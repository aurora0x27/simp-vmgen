"""Distro metadata discovery and validation."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..errors import DistroError

METADATA_FILENAME = "distro.toml"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SIZE_RE = re.compile(r"^([1-9][0-9]*)([KMGT])?$", re.IGNORECASE)
_CHECKSUM_RE = re.compile(r"^(sha256|sha512):[0-9a-fA-F]+$")

_DISTRO_KEYS = {
    "name",
    "display_name",
    "architectures",
    "default_release",
    "provisioner",
    "init_system",
    "package_manager",
    "default_shell",
    "ssh_service",
    "templates",
    "image_arches",
    "shell_paths",
    "package_sets",
    "release",
}
_RELEASE_KEYS = {
    "version",
    "image_url",
    "image_checksum",
    "image_checksum_url",
    "image_format",
    "default_mem",
    "default_cpus",
    "default_disk",
    "machine",
}


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DistroError(f"{where}: expected a table")
    return value


def _unknown_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise DistroError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _string(
    table: dict[str, Any],
    key: str,
    where: str,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise DistroError(f"{where}.{key}: expected a string")
    if required and not value.strip():
        raise DistroError(f"{where}.{key}: must not be empty")
    return value


def _int(table: dict[str, Any], key: str, where: str, *, default: int, minimum: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DistroError(f"{where}.{key}: expected an integer")
    if value < minimum:
        raise DistroError(f"{where}.{key}: must be >= {minimum}")
    return value


def _string_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list):
        raise DistroError(f"{where}.{key}: expected an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise DistroError(f"{where}.{key}[{index}]: expected a non-empty string")
        result.append(item)
    return result


def _size(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise DistroError(f"{where}: expected a size such as 4G")
    match = _SIZE_RE.match(value)
    if match is None:
        raise DistroError(f"{where}: invalid size {value!r}")
    return f"{match.group(1)}{(match.group(2) or '').upper()}"


def _http_url(value: str, where: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DistroError(f"{where}: expected an http(s) URL, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class DistroRelease:
    version: str
    image_url: str
    image_checksum: str = ""
    image_checksum_url: str = ""
    image_format: str = "qcow2"
    default_mem: str = "4G"
    default_cpus: int = 4
    default_disk: str = "20G"
    machine: str = ""

    def image_url_for(self, arch: str) -> str:
        try:
            url = self.image_url.format(arch=arch, version=self.version)
        except KeyError as exc:
            raise DistroError(
                f"release {self.version}: unknown image URL placeholder {exc}"
            ) from exc
        return _http_url(url, f"release {self.version}.image_url")

    def checksum_url_for(self, arch: str) -> str:
        if not self.image_checksum_url:
            return ""
        try:
            url = self.image_checksum_url.format(arch=arch, version=self.version)
        except KeyError as exc:
            raise DistroError(
                f"release {self.version}: unknown checksum URL placeholder {exc}"
            ) from exc
        return _http_url(url, f"release {self.version}.image_checksum_url")


@dataclass(frozen=True, slots=True)
class Distro:
    name: str
    display_name: str
    architectures: tuple[str, ...]
    releases: tuple[DistroRelease, ...]
    default_release: str
    provisioner: str
    init_system: str
    package_manager: str
    default_shell: str
    ssh_service: str
    templates: str
    image_arches: dict[str, str]
    shell_paths: dict[str, str]
    package_sets: dict[str, tuple[str, ...]]

    def release(self, version: str | None = None) -> DistroRelease:
        wanted = version or self.default_release
        for release in self.releases:
            if release.version == wanted:
                return release
        available = ", ".join(release.version for release in self.releases)
        raise DistroError(
            f"distro {self.name!r} has no release {wanted!r} (available: {available})"
        )

    def image_arch(self, arch: str) -> str:
        return self.image_arches.get(arch, arch)

    def shell_path(self, shell: str) -> str:
        if shell.startswith("/"):
            return shell
        return self.shell_paths.get(shell, f"/bin/{shell}")

    def image_url(self, release: DistroRelease, arch: str) -> str:
        return release.image_url_for(self.image_arch(arch))

    def checksum_url(self, release: DistroRelease, arch: str) -> str:
        return release.checksum_url_for(self.image_arch(arch))

    def packages(self, set_names: list[str], extra: list[str] | None = None) -> list[str]:
        result: list[str] = []
        for name in set_names:
            try:
                packages = self.package_sets[name]
            except KeyError as exc:
                available = ", ".join(sorted(self.package_sets))
                raise DistroError(
                    f"distro {self.name!r} has no package set {name!r} (available: {available})"
                ) from exc
            for package in packages:
                if package not in result:
                    result.append(package)
        for package in extra or []:
            if package not in result:
                result.append(package)
        return result


def _parse_release(value: Any, where: str) -> DistroRelease:
    table = _table(value, where)
    _unknown_keys(table, _RELEASE_KEYS, where)
    version = _string(table, "version", where, required=True)
    image_url = _string(table, "image_url", where, required=True)
    _http_url(image_url.format(arch="x86_64", version=version), f"{where}.image_url")

    checksum = _string(table, "image_checksum", where)
    if checksum and _CHECKSUM_RE.match(checksum) is None:
        raise DistroError(f"{where}.image_checksum: expected sha256:<hex> or sha512:<hex>")
    checksum_url = _string(table, "image_checksum_url", where)
    if checksum_url:
        _http_url(
            checksum_url.format(arch="x86_64", version=version),
            f"{where}.image_checksum_url",
        )

    image_format = _string(table, "image_format", where, default="qcow2")
    if image_format not in ("qcow2", "raw"):
        raise DistroError(f"{where}.image_format: expected qcow2 or raw")

    return DistroRelease(
        version=version,
        image_url=image_url,
        image_checksum=checksum,
        image_checksum_url=checksum_url,
        image_format=image_format,
        default_mem=_size(table.get("default_mem", "4G"), f"{where}.default_mem"),
        default_cpus=_int(table, "default_cpus", where, default=4, minimum=1),
        default_disk=_size(table.get("default_disk", "20G"), f"{where}.default_disk"),
        machine=_string(table, "machine", where),
    )


def load_distro_from_text(text: str, *, source: str = METADATA_FILENAME) -> Distro:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DistroError(f"{source}: invalid TOML: {exc}") from exc
    return _parse_distro(data, source=source)


def load_distro_from_path(path: Path) -> Distro:
    metadata = path / METADATA_FILENAME if path.is_dir() else path
    try:
        text = metadata.read_text(encoding="utf-8")
    except OSError as exc:
        raise DistroError(f"cannot read {metadata}: {exc}") from exc
    return load_distro_from_text(text, source=str(metadata))


def _parse_distro(data: dict[str, Any], *, source: str) -> Distro:
    _unknown_keys(data, _DISTRO_KEYS, source)

    name = _string(data, "name", source, required=True)
    if _NAME_RE.match(name) is None:
        raise DistroError(f"{source}.name: invalid distro name {name!r}")

    architectures = tuple(_string_list(data, "architectures", source))
    if not architectures:
        raise DistroError(f"{source}.architectures: at least one architecture is required")

    releases_value = data.get("release")
    if not isinstance(releases_value, list) or not releases_value:
        raise DistroError(f"{source}.release: at least one release table is required")
    releases = tuple(
        _parse_release(item, f"{source}.release[{index}]")
        for index, item in enumerate(releases_value)
    )
    versions = [release.version for release in releases]
    if len(set(versions)) != len(versions):
        raise DistroError(f"{source}.release: duplicate release versions")

    default_release = _string(data, "default_release", source, default=releases[0].version)
    if default_release not in versions:
        raise DistroError(f"{source}.default_release: {default_release!r} is not in release")

    image_arches_table = _table(data.get("image_arches", {}), f"{source}.image_arches")
    image_arches: dict[str, str] = {}
    for arch, image_arch in image_arches_table.items():
        if arch not in architectures:
            raise DistroError(f"{source}.image_arches: {arch!r} is not in architectures")
        if not isinstance(image_arch, str) or not image_arch.strip():
            raise DistroError(f"{source}.image_arches.{arch}: expected a non-empty string")
        image_arches[arch] = image_arch

    shell_paths_table = _table(data.get("shell_paths", {}), f"{source}.shell_paths")
    shell_paths: dict[str, str] = {}
    for shell, path in shell_paths_table.items():
        if not isinstance(shell, str) or not shell.strip():
            raise DistroError(f"{source}.shell_paths: invalid shell name {shell!r}")
        if not isinstance(path, str) or not path.startswith("/"):
            raise DistroError(f"{source}.shell_paths.{shell}: expected an absolute path")
        shell_paths[shell] = path

    package_sets_value = data.get("package_sets", {})
    package_sets_table = _table(package_sets_value, f"{source}.package_sets")
    package_sets: dict[str, tuple[str, ...]] = {}
    for set_name, packages in package_sets_table.items():
        if not isinstance(set_name, str) or not set_name.strip():
            raise DistroError(f"{source}.package_sets: invalid set name {set_name!r}")
        if not isinstance(packages, list):
            raise DistroError(f"{source}.package_sets.{set_name}: expected an array of strings")
        parsed_packages: list[str] = []
        for index, package in enumerate(packages):
            if not isinstance(package, str) or not package.strip():
                raise DistroError(
                    f"{source}.package_sets.{set_name}[{index}]: expected a non-empty string"
                )
            parsed_packages.append(package)
        package_sets[set_name] = tuple(parsed_packages)
    if "base" not in package_sets:
        raise DistroError(f"{source}.package_sets: a 'base' set is required")

    provisioner = _string(data, "provisioner", source, default="cloud-init")
    if provisioner != "cloud-init":
        raise DistroError(f"{source}.provisioner: only 'cloud-init' is supported")

    init_system = _string(data, "init_system", source, default="systemd")
    if init_system not in ("systemd", "openrc"):
        raise DistroError(f"{source}.init_system: expected systemd or openrc")

    return Distro(
        name=name,
        display_name=_string(data, "display_name", source, default=name),
        architectures=architectures,
        releases=releases,
        default_release=default_release,
        provisioner=provisioner,
        init_system=init_system,
        package_manager=_string(data, "package_manager", source, required=True),
        default_shell=_string(data, "default_shell", source, default="sh"),
        ssh_service=_string(data, "ssh_service", source, default="ssh"),
        templates=_string(data, "templates", source, default="templates"),
        image_arches=image_arches,
        shell_paths=shell_paths,
        package_sets=package_sets,
    )


def _distros_root():
    return files("simp_vmgen.distros")


def available_distros() -> list[str]:
    names: list[str] = []
    for entry in _distros_root().iterdir():
        if entry.is_dir() and (entry / METADATA_FILENAME).is_file():
            names.append(entry.name)
    return sorted(names)


def load_distro(name: str) -> Distro:
    if _NAME_RE.match(name) is None:
        raise DistroError(f"invalid distro name {name!r}")
    metadata = _distros_root() / name / METADATA_FILENAME
    if not metadata.is_file():
        available = ", ".join(available_distros())
        raise DistroError(f"unknown distro {name!r} (available: {available})")
    try:
        text = metadata.read_text(encoding="utf-8")
    except OSError as exc:
        raise DistroError(f"cannot read {metadata}: {exc}") from exc
    return load_distro_from_text(text, source=f"distro {name}")
