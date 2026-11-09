"""Build a minimal busybox initramfs for kernel experiments."""

from __future__ import annotations

import gzip
import os
import shlex
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from . import __version__, stamp
from .alpine import prepare_alpine_rootfs
from .cache import cache_dir, download
from .config import Config
from .errors import ProjectError
from .layout import ProjectLayout
from .mounts import resolve_mounts
from .network import resolve_networks
from .paths import load_project
from .rootfs import RootfsSpec, load_rootfs
from .rootfs_source import update_lock as update_rootfs_lock
from .seed import READY_MARKER
from .sshkeys import ensure_ssh_key


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("simp_vmgen", "rootfs"),
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    env.filters["shquote"] = shlex.quote
    return env


def _write(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def ensure_busybox(spec: RootfsSpec) -> Path:
    if not spec.source_url:
        raise ProjectError("busybox rootfs source_url is missing")
    path, _checksum = download(
        spec.source_url,
        spec.source_name(),
        checksum=spec.source_checksum,
        checksum_url=spec.source_checksum_url,
        directory=cache_dir() / "busybox",
    )
    os.chmod(path, 0o755)
    return path


def _install_applets(busybox: Path, rootfs: Path) -> None:
    try:
        result = subprocess.run(
            [str(busybox), "--list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"cannot list busybox applets: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"cannot list busybox applets: {detail}")
    bin_dir = rootfs / "bin"
    for applet in result.stdout.splitlines():
        applet = applet.strip()
        if not applet or applet == "busybox":
            continue
        target = bin_dir / applet
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to("busybox")


def _archive(rootfs: Path, output: Path, *, owner: str = "0:0") -> None:
    cpio = shutil.which("cpio")
    if cpio is None:
        raise ProjectError("cpio not found")
    output.parent.mkdir(parents=True, exist_ok=True)
    paths = ["."]
    for directory, dirnames, filenames in os.walk(rootfs):
        dirnames.sort()
        filenames.sort()
        base = Path(directory)
        for name in dirnames:
            paths.append((base / name).relative_to(rootfs).as_posix())
        for name in filenames:
            paths.append((base / name).relative_to(rootfs).as_posix())
    data = ("\n".join(paths) + "\n").encode()
    try:
        result = subprocess.run(
            [cpio, "-o", "-H", "newc", "--quiet", "-R", owner],
            cwd=rootfs,
            input=data,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"cannot create initramfs archive: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ProjectError(f"cannot create initramfs archive: {detail}")
    tmp = output.with_name(f"{output.name}.tmp")
    with gzip.open(tmp, "wb", compresslevel=9) as handle:
        handle.write(result.stdout)
    os.replace(tmp, output)


def _build_busybox(
    config: Config,
    layout: ProjectLayout,
    spec: RootfsSpec,
    output: Path,
    *,
    busybox_path: Path | None,
    install_applets: bool,
) -> Path:
    rootfs = layout.kernel_dir / "initramfs"
    shutil.rmtree(rootfs, ignore_errors=True)
    for directory in ("bin", "dev", "etc", "lib", "proc", "run", "sys", "tmp", "root"):
        (rootfs / directory).mkdir(parents=True, exist_ok=True)

    modules = layout.kernel_dir / "modules" / "lib"
    if modules.is_dir():
        shutil.copytree(modules, rootfs / "lib", dirs_exist_ok=True)

    busybox = busybox_path or ensure_busybox(spec)
    shutil.copy2(busybox, rootfs / "bin" / "busybox")
    os.chmod(rootfs / "bin" / "busybox", 0o755)
    if install_applets:
        _install_applets(rootfs / "bin" / "busybox", rootfs)

    mounts = resolve_mounts(config, layout.root)
    init = (
        _environment()
        .get_template(f"{spec.name}/{spec.templates}/init.j2")
        .render(
            config=config,
            tool_version=__version__,
            hostname=config.project.hostname,
            mounts=mounts,
            networks=resolve_networks(config),
            ready_marker=f"{READY_MARKER} name={config.project.hostname}",
        )
    )
    _write(rootfs / "init", init, mode=0o755)
    _write(rootfs / "etc" / "hostname", f"{config.project.hostname}\n")
    _write(rootfs / "etc" / "passwd", "root:x:0:0:root:/root:/bin/sh\n")
    _write(rootfs / "etc" / "group", "root:x:0:\n")

    _archive(rootfs, output)
    return output


def build_initramfs(
    config: Config,
    layout: ProjectLayout,
    *,
    busybox_path: Path | None = None,
    install_applets: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    if config.kernel.rootfs == "none":
        raise ProjectError("kernel.rootfs is 'none'; provide kernel.initrd instead")
    spec = load_rootfs(config.kernel.rootfs)
    output = layout.kernel_dir / "initramfs.cpio.gz"
    if dry_run:
        print(f"would build initramfs: {output}")
        return output

    if spec.name == "busybox":
        return _build_busybox(
            config,
            layout,
            spec,
            output,
            busybox_path=busybox_path,
            install_applets=install_applets,
        )
    if spec.name == "alpine":
        key = ensure_ssh_key(config, layout.ssh_key)
        info = prepare_alpine_rootfs(
            config,
            layout.root,
            layout,
            generated_public_key=key.public_key if key is not None else "",
            force=force,
        )
        update_rootfs_lock(layout.root, info)
        _archive(info.root_dir, output)
        return output
    raise ProjectError(f"rootfs plugin {spec.name!r} cannot build an initramfs")


def kernel_initramfs(args: Any) -> int:
    root, config = load_project(getattr(args, "directory", None))
    if not config.kernel.enabled:
        raise ProjectError("kernel support is not enabled in vmgen.toml")
    layout = ProjectLayout(root, config)
    output = build_initramfs(
        config,
        layout,
        force=getattr(args, "force", False),
        dry_run=getattr(args, "dry_run", False),
    )
    if getattr(args, "dry_run", False):
        print(f"would create {output}")
    else:
        stamp.record_kernel_initrd(config, layout, output)
        print(f"initramfs: {output}")
    return 0
