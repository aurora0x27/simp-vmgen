"""Kernel source, configuration and build commands."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import stamp
from .alpine import prepare_alpine_rootfs
from .config import LOCK_FILENAME, Config
from .distros import load_distro
from .errors import ProjectError
from .initramfs import build_initramfs, kernel_initramfs
from .kernel_source import apply_to_lock, artifact_path, resolve_source
from .layout import ProjectLayout
from .lock import Lock, create_lock
from .paths import load_project
from .rootfs import load_rootfs
from .rootfs_source import fetch_rootfs
from .rootfs_source import update_lock as update_rootfs_lock
from .runner import run_streaming
from .sshkeys import ensure_ssh_key


def _project(args: Any) -> tuple[Path, Config, ProjectLayout]:
    root, config = load_project(getattr(args, "directory", None))
    return root, config, ProjectLayout(root, config)


def _load_lock(root: Path, config: Config) -> Lock:
    path = root / LOCK_FILENAME
    if path.is_file():
        return Lock.load(path)
    distro = load_distro(config.distro.name)
    return create_lock(config, distro, distro.release(config.distro.release))


DEFAULT_VM_KCONFIG = {
    "CONFIG_9P_FS": "y",
    "CONFIG_NET_9P": "y",
    "CONFIG_NET_9P_VIRTIO": "y",
    "CONFIG_VIRTIO": "y",
    "CONFIG_VIRTIO_PCI": "y",
    "CONFIG_VIRTIO_BLK": "y",
    "CONFIG_VIRTIO_NET": "y",
    "CONFIG_DEVTMPFS": "y",
    "CONFIG_DEVTMPFS_MOUNT": "y",
    "CONFIG_SERIAL_8250": "y",
    "CONFIG_SERIAL_8250_CONSOLE": "y",
    "CONFIG_BLK_DEV_INITRD": "y",
    "CONFIG_RD_GZIP": "y",
    "CONFIG_TMPFS": "y",
    "CONFIG_PROC_FS": "y",
    "CONFIG_SYSFS": "y",
}


def kernel_build_dir(layout: ProjectLayout) -> Path:
    return layout.kernel_dir / "build"


def _run_make(
    source: Path,
    build: Path,
    config: Config,
    target: str,
    *,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
    log_path: Path | None = None,
) -> None:
    make = shutil.which("make")
    if make is None:
        raise ProjectError("make not found")
    jobs = config.kernel.jobs or (os.cpu_count() or 1)
    argv = [
        make,
        "-C",
        str(source),
        f"O={build}",
        *config.kernel.make_args,
        *(extra_args or []),
    ]
    if target in ("bzImage", "vmlinux", "modules", "all"):
        argv.append(f"-j{jobs}")
    argv.append(target)
    if dry_run:
        print("+", " ".join(argv))
        return
    result = run_streaming(argv, log_path=log_path, what=f"make {target}")
    if result.returncode != 0:
        suffix = f" (log: {log_path})" if log_path is not None else ""
        detail = "\n".join(result.tail)
        raise ProjectError(f"make {target} failed{suffix}:\n{detail}")


def _kernel_config_file(config: Config, root: Path) -> tuple[Path | None, str]:
    value = config.kernel.config
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    if path.is_file():
        return path, ""
    if "/" in value or value.endswith(".config"):
        raise ProjectError(f"kernel config file not found: {path}")
    if value == "menuconfig":
        raise ProjectError(
            "interactive menuconfig is not supported; use a defconfig plus fragments"
        )
    return None, value


def configure_kernel(
    config: Config,
    root: Path,
    source: Path,
    layout: ProjectLayout,
    *,
    dry_run: bool = False,
) -> Path:
    build = kernel_build_dir(layout)
    build.mkdir(parents=True, exist_ok=True)
    config_file = build / ".config"
    base_path, target = _kernel_config_file(config, root)
    if dry_run:
        if base_path is not None:
            print(f"+ cp {base_path} {config_file}")
        else:
            _run_make(source, build, config, target, dry_run=True)
        _run_make(source, build, config, "olddefconfig", dry_run=True)
        return config_file

    if base_path is not None:
        shutil.copyfile(base_path, config_file)
    else:
        _run_make(
            source,
            build,
            config,
            target,
            log_path=layout.logs_dir / "kernel-config.log",
        )

    with config_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# simp-vmgen VM defaults\n")
        for name, value in sorted(DEFAULT_VM_KCONFIG.items()):
            handle.write(f"{name}={value}\n")
        handle.write("\n# simp-vmgen fragments\n")
        for fragment in config.kernel.fragments:
            path = Path(fragment).expanduser()
            if not path.is_absolute():
                path = root / path
            try:
                handle.write(path.read_text(encoding="utf-8").rstrip() + "\n")
            except OSError as exc:
                raise ProjectError(f"cannot read Kconfig fragment {path}: {exc}") from exc
        for name, value in sorted(config.kernel.options.items()):
            handle.write(f"{name}={value}\n")

    _run_make(
        source,
        build,
        config,
        "olddefconfig",
        log_path=layout.logs_dir / "kernel-config.log",
    )
    return config_file


def kernel_config(args: Any) -> int:
    root, config, layout = _project(args)
    if not config.kernel.enabled:
        raise ProjectError("kernel support is not enabled in vmgen.toml")
    if config.kernel.mode == "artifact":
        raise ProjectError("kernel config is not available in artifact mode")
    info = resolve_source(config, root, layout, dry_run=getattr(args, "dry_run", False))
    path = configure_kernel(
        config, root, info.path, layout, dry_run=getattr(args, "dry_run", False)
    )
    if getattr(args, "dry_run", False):
        print(f"would generate kernel config: {path}")
    else:
        print(f"kernel config: {path}")
    return 0


def built_image_path(config: Config, layout: ProjectLayout) -> Path | None:
    build = kernel_build_dir(layout)
    if "bzImage" in config.kernel.targets:
        return build / "arch" / "x86" / "boot" / "bzImage"
    if "vmlinux" in config.kernel.targets:
        return build / "vmlinux"
    if "Image" in config.kernel.targets:
        return build / "arch" / "arm64" / "boot" / "Image"
    return None


@dataclass(frozen=True, slots=True)
class KernelArtifacts:
    image: Path
    initrd: Path


def _update_kernel_lock(root: Path, config: Config, info: Any) -> None:
    lock = _load_lock(root, config)
    apply_to_lock(lock, info)
    lock.save(root / LOCK_FILENAME)


def build_kernel(
    config: Config,
    root: Path,
    layout: ProjectLayout,
    *,
    dry_run: bool = False,
) -> Path | None:
    if config.kernel.mode == "artifact":
        raise ProjectError("kernel build is not available in artifact mode")
    info = resolve_source(config, root, layout, dry_run=dry_run)
    if not dry_run:
        _update_kernel_lock(root, config, info)
    build = kernel_build_dir(layout)
    if not (build / ".config").is_file():
        configure_kernel(config, root, info.path, layout, dry_run=dry_run)
    for target in config.kernel.targets:
        _run_make(
            info.path,
            build,
            config,
            target,
            dry_run=dry_run,
            log_path=layout.logs_dir / "kernel-build.log",
        )
    if "modules" in config.kernel.targets and not dry_run:
        modules_dir = layout.kernel_dir / "modules"
        modules_dir.mkdir(parents=True, exist_ok=True)
        _run_make(
            info.path,
            build,
            config,
            "modules_install",
            extra_args=[f"INSTALL_MOD_PATH={modules_dir}"],
            log_path=layout.logs_dir / "kernel-build.log",
        )
    image = built_image_path(config, layout)
    if not dry_run and image is not None and image.is_file():
        stamp.record_kernel_image(config, layout, image)
    return image


def kernel_image_path(config: Config, layout: ProjectLayout) -> Path:
    if config.kernel.mode == "artifact":
        return artifact_path(config, layout.root)
    image = built_image_path(config, layout)
    if image is None or not image.is_file():
        raise ProjectError("kernel image is missing; run `simp-vmgen kernel build`")
    return image


def kernel_initrd_path(config: Config, layout: ProjectLayout) -> Path:
    if config.kernel.initrd:
        path = Path(config.kernel.initrd).expanduser()
        if not path.is_absolute():
            path = layout.root / path
        path = path.resolve()
        if not path.is_file():
            raise ProjectError(f"kernel initrd not found: {path}")
        return path
    path = layout.kernel_dir / "initramfs.cpio.gz"
    if not path.is_file():
        raise ProjectError("kernel initramfs is missing; run `simp-vmgen kernel initramfs`")
    return path


def ensure_kernel_artifacts(
    config: Config, root: Path, layout: ProjectLayout, *, dry_run: bool = False
) -> KernelArtifacts:
    if not config.kernel.enabled:
        raise ProjectError("kernel support is not enabled in vmgen.toml")
    previous = stamp.load(layout)
    kernel_stale = not (
        previous is not None
        and previous.get("mode") == "kernel"
        and previous.get("config_hash") == stamp.config_digest(config)
    )
    image: Path | None
    if config.kernel.mode == "artifact":
        info = resolve_source(config, root, layout, dry_run=dry_run)
        image = info.path
        if not dry_run:
            _update_kernel_lock(root, config, info)
    else:
        image = built_image_path(config, layout)
        if kernel_stale or image is None or not image.is_file():
            image = build_kernel(config, root, layout, dry_run=dry_run) or image
        if image is None:
            if dry_run:
                image = layout.kernel_dir / "build" / "vmlinux"
            else:
                raise ProjectError("kernel build did not produce a bootable image")
    if config.kernel.initrd:
        initrd = kernel_initrd_path(config, layout)
    else:
        initrd = build_initramfs(config, layout, dry_run=dry_run)
    if not dry_run and image is not None:
        stamp.record_kernel_image(config, layout, image)
        stamp.record_kernel_initrd(config, layout, initrd)
    return KernelArtifacts(image=image, initrd=initrd)


def kernel_build(args: Any) -> int:
    root, config, layout = _project(args)
    if not config.kernel.enabled:
        raise ProjectError("kernel support is not enabled in vmgen.toml")
    if config.kernel.mode == "artifact":
        raise ProjectError("kernel build is not available in artifact mode")
    dry_run = getattr(args, "dry_run", False)
    image = build_kernel(config, root, layout, dry_run=dry_run)
    if dry_run:
        print(f"would build kernel in {kernel_build_dir(layout)}")
    elif image is not None and image.is_file():
        print(f"kernel image: {image}")
    else:
        print(f"kernel build finished in {kernel_build_dir(layout)}")
    return 0


def kernel_clean(args: Any) -> int:
    _root, _config, layout = _project(args)
    shutil.rmtree(layout.kernel_dir / "build", ignore_errors=True)
    shutil.rmtree(layout.kernel_dir / "initramfs", ignore_errors=True)
    shutil.rmtree(layout.kernel_dir / "rootfs", ignore_errors=True)
    (layout.kernel_dir / "initramfs.cpio.gz").unlink(missing_ok=True)
    print(f"removed {layout.kernel_dir / 'build'}, rootfs and initramfs")
    return 0


def kernel_fetch(args: Any) -> int:
    root, config, layout = _project(args)
    layout.ensure()
    info = resolve_source(config, root, layout, dry_run=getattr(args, "dry_run", False))
    if getattr(args, "dry_run", False):
        print(f"would fetch kernel source: {info.path}")
        return 0

    lock = _load_lock(root, config)
    apply_to_lock(lock, info)
    lock.save(root / LOCK_FILENAME)
    print(f"mode     : {info.mode}")
    print(f"source   : {info.path}")
    if info.version:
        print(f"version  : {info.version}")
    if info.url:
        print(f"url      : {info.url}")
    if info.checksum:
        print(f"checksum : {info.checksum}")
    if info.repo:
        print(f"repo     : {info.repo}")
    if info.commit:
        print(f"commit   : {info.commit}")
    return 0


def kernel_rootfs(args: Any) -> int:
    root, config, layout = _project(args)
    if not config.kernel.enabled:
        raise ProjectError("kernel support is not enabled in vmgen.toml")
    spec = load_rootfs(config.kernel.rootfs)
    dry_run = getattr(args, "dry_run", False)
    if spec.name == "alpine":
        key = None if dry_run else ensure_ssh_key(config, layout.ssh_key)
        info = prepare_alpine_rootfs(
            config,
            root,
            layout,
            generated_public_key=key.public_key if key is not None else "",
            force=getattr(args, "force", False),
            dry_run=dry_run,
        )
    else:
        info = fetch_rootfs(
            config,
            root,
            layout,
            force=getattr(args, "force", False),
            dry_run=dry_run,
        )
    if dry_run:
        print(f"would prepare rootfs: {info.root_dir}")
        return 0
    update_rootfs_lock(root, info)
    print(f"rootfs : {info.spec.name} {info.spec.release} ({info.spec.kind})")
    print(f"source : {info.root_dir}")
    print(f"archive: {info.archive}")
    if info.checksum:
        print(f"sha256 : {info.checksum}")
    return 0


def kernel_status(args: Any) -> int:
    root, config, layout = _project(args)
    json_output = getattr(args, "json", False)
    if not config.kernel.enabled:
        if json_output:
            print(json.dumps({"enabled": False}, indent=2))
        else:
            print("kernel: disabled")
        return 0

    source: dict[str, Any]
    if config.kernel.mode == "artifact":
        source = {
            "kind": "artifact",
            "artifact": config.kernel.artifact,
            "artifact_type": config.kernel.artifact_type,
        }
    elif config.kernel.commit:
        source = {
            "kind": "git",
            "repo": config.kernel.repo,
            "commit": config.kernel.commit,
            "tree": str(layout.kernel_dir / "src" / "linux-git"),
        }
    else:
        source = {
            "kind": "tarball",
            "version": config.kernel.version,
            "tree": str(layout.kernel_dir / "src" / f"linux-{config.kernel.version}"),
        }

    rootfs: dict[str, Any] | None = None
    if config.kernel.rootfs != "none":
        spec = load_rootfs(config.kernel.rootfs)
        rootfs_path = layout.kernel_dir / "rootfs" / spec.name
        rootfs = {
            "name": spec.name,
            "release": spec.release,
            "kind": spec.kind,
            "path": str(rootfs_path),
            "present": rootfs_path.is_dir(),
        }

    image: Path | None
    if config.kernel.mode == "artifact":
        image = Path(config.kernel.artifact).expanduser()
    else:
        image = built_image_path(config, layout)
    initrd = (
        Path(config.kernel.initrd)
        if config.kernel.initrd
        else layout.kernel_dir / "initramfs.cpio.gz"
    )
    lock_path = root / LOCK_FILENAME
    lock_data = asdict(Lock.load(lock_path).kernel) if lock_path.is_file() else None

    data: dict[str, Any] = {
        "enabled": True,
        "mode": config.kernel.mode,
        "source": source,
        "build": str(kernel_build_dir(layout)),
        "rootfs": rootfs,
        "image": ({"path": str(image), "present": image.is_file()} if image is not None else None),
        "initrd": {"path": str(initrd), "present": initrd.is_file()},
        "lock": lock_data,
    }
    if json_output:
        print(json.dumps(data, indent=2))
        return 0

    print(f"mode    : {config.kernel.mode}")
    if source["kind"] == "artifact":
        print(f"artifact: {source['artifact']} ({source['artifact_type']})")
    elif source["kind"] == "git":
        print(f"source  : git {source['repo']} @ {source['commit']}")
        print(f"tree    : {source['tree']}")
    else:
        print(f"source  : tarball {source['version']}")
        print(f"tree    : {source['tree']}")
    print(f"build   : {data['build']}")
    if rootfs is not None:
        print(f"rootfs  : {rootfs['name']} {rootfs['release']} ({rootfs['kind']})")
        print(f"          {rootfs['path']} ({'present' if rootfs['present'] else 'missing'})")
    if data["image"] is not None:
        print(
            f"image   : {data['image']['path']} "
            f"({'present' if data['image']['present'] else 'missing'})"
        )
    print(
        f"initrd  : {data['initrd']['path']} "
        f"({'present' if data['initrd']['present'] else 'missing'})"
    )
    if lock_data is not None:
        source_kind = lock_data.get("source")
        if source_kind == "tarball":
            detail = " ".join(
                item
                for item in (
                    "tarball",
                    str(lock_data.get("version") or ""),
                    str(lock_data.get("checksum") or ""),
                )
                if item
            )
        elif source_kind == "git":
            detail = f"git {lock_data.get('repo') or ''} @ {lock_data.get('commit') or ''}"
        elif source_kind == "artifact":
            detail = f"artifact {lock_data.get('artifact') or ''}"
        else:
            detail = str(source_kind or "(empty)")
        print(f"lock    : {detail}")
    return 0


def kernel_dispatch(args: Any) -> int:
    command = getattr(args, "kernel_command", None)
    if command == "fetch":
        return kernel_fetch(args)
    if command == "config":
        return kernel_config(args)
    if command == "build":
        return kernel_build(args)
    if command == "initramfs":
        return kernel_initramfs(args)
    if command == "rootfs":
        return kernel_rootfs(args)
    if command == "status":
        return kernel_status(args)
    if command == "clean":
        return kernel_clean(args)
    raise ProjectError(f"kernel {command or ''} is not implemented yet")
