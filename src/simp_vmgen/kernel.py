"""Kernel source, configuration and build commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import LOCK_FILENAME, Config
from .distros import load_distro
from .errors import ProjectError
from .kernel_source import apply_to_lock, resolve_source
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


def kernel_dispatch(args: Any) -> int:
    command = getattr(args, "kernel_command", None)
    if command == "fetch":
        return kernel_fetch(args)
    raise ProjectError(f"kernel {command or ''} is not implemented yet")
