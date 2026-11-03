"""Kernel source, configuration and build commands."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LOCK_FILENAME, Config
from .distros import load_distro
from .errors import ProjectError
from .initramfs import build_initramfs, kernel_initramfs
from .kernel_source import apply_to_lock, artifact_path, resolve_source
from .layout import ProjectLayout
from .lock import Lock, create_lock
from .paths import load_project


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
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=None
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"make {target} failed: {exc}") from exc
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip().splitlines()
        detail = "\n".join(output[-30:])
        raise ProjectError(f"make {target} failed:\n{detail}")


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
        _run_make(source, build, config, target)

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

    _run_make(source, build, config, "olddefconfig")
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
    build = kernel_build_dir(layout)
    if not dry_run and not (build / ".config").is_file():
        configure_kernel(config, root, info.path, layout)
    for target in config.kernel.targets:
        _run_make(info.path, build, config, target, dry_run=dry_run)
    if "modules" in config.kernel.targets and not dry_run:
        modules_dir = layout.kernel_dir / "modules"
        modules_dir.mkdir(parents=True, exist_ok=True)
        _run_make(
            info.path,
            build,
            config,
            "modules_install",
            extra_args=[f"INSTALL_MOD_PATH={modules_dir}"],
        )
    return built_image_path(config, layout)


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
    image: Path | None
    if config.kernel.mode == "artifact":
        image = artifact_path(config, root)
    else:
        image = built_image_path(config, layout)
        if image is None or not image.is_file():
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
    (layout.kernel_dir / "initramfs.cpio.gz").unlink(missing_ok=True)
    print(f"removed {layout.kernel_dir / 'build'} and initramfs")
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


def kernel_status(args: Any) -> int:
    root, config, layout = _project(args)
    if not config.kernel.enabled:
        print("kernel: disabled")
        return 0
    print(f"mode    : {config.kernel.mode}")
    if config.kernel.mode == "artifact":
        print(f"artifact: {config.kernel.artifact} ({config.kernel.artifact_type})")
    elif config.kernel.commit:
        print(f"source  : git {config.kernel.repo} @ {config.kernel.commit}")
        print(f"tree    : {layout.kernel_dir / 'src' / 'linux-git'}")
    else:
        print(f"source  : tarball {config.kernel.version}")
        print(f"tree    : {layout.kernel_dir / 'src' / f'linux-{config.kernel.version}'}")
    print(f"build   : {kernel_build_dir(layout)}")
    image = built_image_path(config, layout)
    if image is not None:
        print(f"image   : {image} ({'present' if image.is_file() else 'missing'})")
    initrd = (
        Path(config.kernel.initrd)
        if config.kernel.initrd
        else layout.kernel_dir / "initramfs.cpio.gz"
    )
    print(f"initrd  : {initrd} ({'present' if initrd.is_file() else 'missing'})")
    lock_path = root / LOCK_FILENAME
    if lock_path.is_file():
        lock = Lock.load(lock_path)
        print(f"lock    : {lock.kernel.source or '(empty)'}")
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
    if command == "status":
        return kernel_status(args)
    if command == "clean":
        return kernel_clean(args)
    raise ProjectError(f"kernel {command or ''} is not implemented yet")
