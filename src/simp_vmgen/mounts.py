"""Resolve configured bind mounts for QEMU and cloud-init."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import ProjectError


@dataclass(frozen=True, slots=True)
class ResolvedMount:
    source: Path
    target: str
    readonly: bool
    tag: str


def resolve_mounts(config: Config, root: Path) -> list[ResolvedMount]:
    resolved: list[ResolvedMount] = []
    for mount, tag in zip(config.mounts, config.mount_tags(), strict=True):
        source = Path(mount.source).expanduser()
        if not source.is_absolute():
            source = root / source
        source = source.resolve()
        if not source.is_dir():
            raise ProjectError(f"mount source for {mount.target!r} is not a directory: {source}")
        resolved.append(
            ResolvedMount(
                source=source,
                target=mount.target,
                readonly=mount.readonly,
                tag=tag,
            )
        )
    return resolved
