"""Fetch and extract rootfs source archives."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cache import download, rootfs_sources_dir
from .config import LOCK_FILENAME, Config
from .errors import ProjectError
from .layout import ProjectLayout
from .lock import Lock, RootfsLock
from .rootfs import RootfsSpec, load_rootfs


@dataclass(frozen=True, slots=True)
class RootfsSourceInfo:
    spec: RootfsSpec
    archive: Path
    root_dir: Path
    checksum: str


def rootfs_dir(layout: ProjectLayout, spec: RootfsSpec) -> Path:
    return layout.kernel_dir / "rootfs" / spec.name


def _extract(archive: Path, destination: Path) -> None:
    tar = shutil.which("tar")
    if tar is None:
        raise ProjectError("tar not found")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [tar, "-xf", str(archive), "-C", str(destination)],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"cannot extract rootfs source: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"cannot extract rootfs source: {detail}")


def fetch_rootfs(
    config: Config,
    root: Path,
    layout: ProjectLayout,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> RootfsSourceInfo:
    if config.kernel.rootfs == "none":
        raise ProjectError("kernel.rootfs is 'none'; provide kernel.initrd instead")
    spec = load_rootfs(config.kernel.rootfs)
    if spec.distro and spec.distro != config.distro.name:
        raise ProjectError(
            f"rootfs {spec.name!r} expects distro {spec.distro!r}, "
            f"but vmgen.toml uses {config.distro.name!r}"
        )
    if not spec.source_url:
        raise ProjectError(f"rootfs {spec.name!r} has no source_url")
    destination = rootfs_dir(layout, spec)
    if dry_run:
        return RootfsSourceInfo(
            spec=spec,
            archive=rootfs_sources_dir() / spec.source_name(),
            root_dir=destination,
            checksum=spec.source_checksum,
        )
    if force:
        shutil.rmtree(destination, ignore_errors=True)
    if not destination.is_dir():
        archive, checksum = download(
            spec.source_url,
            spec.source_name(),
            checksum=spec.source_checksum,
            checksum_url=spec.source_checksum_url,
            directory=rootfs_sources_dir(),
        )
        try:
            _extract(archive, destination)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    else:
        archive = rootfs_sources_dir() / spec.source_name()
        checksum = spec.source_checksum
    return RootfsSourceInfo(
        spec=spec,
        archive=archive,
        root_dir=destination,
        checksum=checksum,
    )


def apply_to_lock(lock: Lock, info: RootfsSourceInfo) -> None:
    lock.rootfs = RootfsLock(
        source=info.spec.name,
        version=info.spec.release,
        kind=info.spec.kind,
        url=info.spec.source_url,
        checksum=info.checksum,
    )


def update_lock(root: Path, info: RootfsSourceInfo) -> None:
    path = root / LOCK_FILENAME
    lock = Lock.load(path) if path.is_file() else Lock()
    apply_to_lock(lock, info)
    lock.save(path)
