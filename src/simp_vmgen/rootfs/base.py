"""Rootfs plugin metadata for kernel experiments."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..errors import ProjectError

ROOTFS_METADATA = "rootfs.toml"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_KINDS = {"initramfs", "disk", "9p"}


def _string(table: dict[str, Any], key: str, where: str, default: str = "") -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ProjectError(f"{where}.{key}: expected a string")
    return value


@dataclass(frozen=True, slots=True)
class RootfsSpec:
    name: str
    display_name: str
    kind: str
    templates: str
    init: str
    distro: str = ""
    release: str = ""
    source_url: str = ""
    source_filename: str = ""
    source_checksum: str = ""
    source_checksum_url: str = ""
    packages: tuple[str, ...] = ()
    boot_args: str = ""
    apk_tools_url: str = ""
    apk_tools_filename: str = ""
    apk_tools_checksum: str = ""

    def source_name(self) -> str:
        return self.source_filename or Path(urlparse(self.source_url).path).name


def _parse(text: str, *, source: str) -> RootfsSpec:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{source}: invalid TOML: {exc}") from exc
    allowed = {
        "name",
        "display_name",
        "kind",
        "templates",
        "init",
        "distro",
        "release",
        "source_url",
        "source_filename",
        "source_checksum",
        "source_checksum_url",
        "packages",
        "boot_args",
        "apk_tools_url",
        "apk_tools_filename",
        "apk_tools_checksum",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProjectError(f"{source}: unknown key(s): {', '.join(unknown)}")
    name = _string(data, "name", source)
    if _NAME_RE.match(name) is None:
        raise ProjectError(f"{source}.name: invalid rootfs name {name!r}")
    kind = _string(data, "kind", source, "initramfs")
    if kind not in _KINDS:
        raise ProjectError(f"{source}.kind: expected one of {', '.join(sorted(_KINDS))}")
    templates = _string(data, "templates", source, "templates")
    init = _string(data, "init", source, "/init")
    if not init.startswith("/"):
        raise ProjectError(f"{source}.init: expected an absolute path")
    packages_value = data.get("packages", [])
    if not isinstance(packages_value, list):
        raise ProjectError(f"{source}.packages: expected an array of strings")
    packages: list[str] = []
    for index, package in enumerate(packages_value):
        if not isinstance(package, str) or not package.strip():
            raise ProjectError(f"{source}.packages[{index}]: expected a non-empty string")
        packages.append(package)
    return RootfsSpec(
        name=name,
        display_name=_string(data, "display_name", source, name),
        kind=kind,
        templates=templates,
        init=init,
        distro=_string(data, "distro", source),
        release=_string(data, "release", source),
        source_url=_string(data, "source_url", source),
        source_filename=_string(data, "source_filename", source),
        source_checksum=_string(data, "source_checksum", source),
        source_checksum_url=_string(data, "source_checksum_url", source),
        packages=tuple(packages),
        boot_args=_string(data, "boot_args", source),
        apk_tools_url=_string(data, "apk_tools_url", source),
        apk_tools_filename=_string(data, "apk_tools_filename", source),
        apk_tools_checksum=_string(data, "apk_tools_checksum", source),
    )


def _rootfs_root():
    return files("simp_vmgen.rootfs")


def available_rootfs() -> list[str]:
    names: list[str] = []
    for entry in _rootfs_root().iterdir():
        if entry.is_dir() and (entry / ROOTFS_METADATA).is_file():
            names.append(entry.name)
    return sorted(names)


def load_rootfs(name: str) -> RootfsSpec:
    if _NAME_RE.match(name) is None:
        raise ProjectError(f"invalid rootfs name {name!r}")
    metadata = _rootfs_root() / name / ROOTFS_METADATA
    if not metadata.is_file():
        available = ", ".join(available_rootfs())
        raise ProjectError(f"unknown rootfs {name!r} (available: {available})")
    try:
        text = metadata.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read {metadata}: {exc}") from exc
    return _parse(text, source=f"rootfs {name}")


def load_rootfs_from_path(path: Path) -> RootfsSpec:
    metadata = path / ROOTFS_METADATA if path.is_dir() else path
    try:
        text = metadata.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read {metadata}: {exc}") from exc
    return _parse(text, source=str(metadata))
