"""The vmgen.lock file: pinned remote artifacts for reproducible builds."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomli_w

from . import __version__
from .config import LOCK_FILENAME, Config
from .distros import Distro, DistroRelease
from .errors import LockError

LOCK_SCHEMA_VERSION = 1


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LockError(f"{where}: expected a table")
    return value


def _string(table: dict[str, Any], key: str, where: str, default: str = "") -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise LockError(f"{where}.{key}: expected a string")
    return value


@dataclass(slots=True)
class KernelLock:
    source: str = ""
    version: str = ""
    url: str = ""
    checksum: str = ""
    repo: str = ""
    commit: str = ""
    artifact: str = ""
    artifact_checksum: str = ""


@dataclass(slots=True)
class Lock:
    schema: int = LOCK_SCHEMA_VERSION
    image_url: str = ""
    image_filename: str = ""
    image_checksum: str = ""
    image_checksum_url: str = ""
    kernel: KernelLock = field(default_factory=KernelLock)
    tool_version: str = __version__

    @classmethod
    def load(cls, path: Path) -> Lock:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LockError(f"cannot read {path}: {exc}") from exc
        return cls.loads(text, source=str(path))

    @classmethod
    def loads(cls, text: str, *, source: str = LOCK_FILENAME) -> Lock:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise LockError(f"{source}: invalid TOML: {exc}") from exc
        unknown = sorted(set(data) - {"schema", "image", "kernel", "tool"})
        if unknown:
            raise LockError(f"{source}: unknown key(s): {', '.join(unknown)}")
        schema = data.get("schema", LOCK_SCHEMA_VERSION)
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise LockError(f"{source}.schema: expected an integer")
        if schema != LOCK_SCHEMA_VERSION:
            raise LockError(
                f"{source}.schema: unsupported schema {schema} (expected {LOCK_SCHEMA_VERSION})"
            )

        image = _table(data.get("image"), f"{source}.image")
        unknown = sorted(set(image) - {"url", "filename", "checksum", "checksum_url"})
        if unknown:
            raise LockError(f"{source}.image: unknown key(s): {', '.join(unknown)}")
        kernel = _table(data.get("kernel", {}), f"{source}.kernel")
        kernel_keys = {
            "source",
            "version",
            "url",
            "checksum",
            "repo",
            "commit",
            "artifact",
            "artifact_checksum",
        }
        unknown = sorted(set(kernel) - kernel_keys)
        if unknown:
            raise LockError(f"{source}.kernel: unknown key(s): {', '.join(unknown)}")
        tool = _table(data.get("tool", {}), f"{source}.tool")
        unknown = sorted(set(tool) - {"version"})
        if unknown:
            raise LockError(f"{source}.tool: unknown key(s): {', '.join(unknown)}")
        return cls(
            schema=schema,
            image_url=_string(image, "url", f"{source}.image"),
            image_filename=_string(image, "filename", f"{source}.image"),
            image_checksum=_string(image, "checksum", f"{source}.image"),
            image_checksum_url=_string(image, "checksum_url", f"{source}.image"),
            kernel=KernelLock(
                source=_string(kernel, "source", f"{source}.kernel"),
                version=_string(kernel, "version", f"{source}.kernel"),
                url=_string(kernel, "url", f"{source}.kernel"),
                checksum=_string(kernel, "checksum", f"{source}.kernel"),
                repo=_string(kernel, "repo", f"{source}.kernel"),
                commit=_string(kernel, "commit", f"{source}.kernel"),
                artifact=_string(kernel, "artifact", f"{source}.kernel"),
                artifact_checksum=_string(
                    kernel, "artifact_checksum", f"{source}.kernel"
                ),
            ),
            tool_version=_string(tool, "version", f"{source}.tool", __version__),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "image": {
                "url": self.image_url,
                "filename": self.image_filename,
                "checksum": self.image_checksum,
                "checksum_url": self.image_checksum_url,
            },
            "kernel": {
                "source": self.kernel.source,
                "version": self.kernel.version,
                "url": self.kernel.url,
                "checksum": self.kernel.checksum,
                "repo": self.kernel.repo,
                "commit": self.kernel.commit,
                "artifact": self.kernel.artifact,
                "artifact_checksum": self.kernel.artifact_checksum,
            },
            "tool": {"version": self.tool_version},
        }

    def dumps(self) -> str:
        text = tomli_w.dumps(self.to_dict())
        return text if text.endswith("\n") else f"{text}\n"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        try:
            tmp.write_text(self.dumps(), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise LockError(f"cannot write {path}: {exc}") from exc


def create_lock(config: Config, distro: Distro, release: DistroRelease) -> Lock:
    image_url = config.distro.image or distro.image_url(release, config.distro.arch)
    filename = Path(urlparse(image_url).path).name
    if not filename:
        raise LockError(f"cannot determine image filename from {image_url!r}")
    lock = Lock(
        image_url=image_url,
        image_filename=filename,
        image_checksum=config.distro.image_checksum or release.image_checksum,
        image_checksum_url=distro.checksum_url(release, config.distro.arch),
    )
    if config.kernel.enabled:
        kernel = config.kernel
        if kernel.artifact:
            lock.kernel = KernelLock(
                source="artifact",
                artifact=kernel.artifact,
            )
        elif kernel.commit:
            lock.kernel = KernelLock(
                source="git",
                repo=kernel.repo,
                commit=kernel.commit,
            )
        else:
            lock.kernel = KernelLock(
                source="tarball",
                version=kernel.version,
                checksum=kernel.source_checksum,
            )
    return lock
