"""Build stamps used to detect stale runtime artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ProjectError
from .layout import ProjectLayout

STAMP_SCHEMA_VERSION = 1
STAMP_FILENAME = "build.json"

_SIZE_RE = re.compile(r"^([1-9][0-9]*)([KMGT])?$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
}


def config_digest(config: Config) -> str:
    return hashlib.sha256(config.dumps().encode("utf-8")).hexdigest()


def _size_bytes(value: str) -> int | None:
    match = _SIZE_RE.match(value)
    if match is None:
        return None
    unit = (match.group(2) or "").upper()
    return int(match.group(1)) * _SIZE_MULTIPLIERS[unit]


def _stamp_path(layout: ProjectLayout) -> Path:
    return layout.runtime_dir / STAMP_FILENAME


def load(layout: ProjectLayout) -> dict[str, Any] | None:
    path = _stamp_path(layout)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"cannot read build stamp {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"invalid build stamp {path}")
    schema = data.get("schema")
    if schema != STAMP_SCHEMA_VERSION:
        raise ProjectError(
            f"{path}: unsupported build stamp schema {schema!r} (expected {STAMP_SCHEMA_VERSION})"
        )
    return data


def _save(layout: ProjectLayout, data: dict[str, Any]) -> None:
    path = _stamp_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise ProjectError(f"cannot write build stamp {path}: {exc}") from exc


def _new_stamp(config: Config, mode: str) -> dict[str, Any]:
    return {
        "schema": STAMP_SCHEMA_VERSION,
        "mode": mode,
        "config_hash": config_digest(config),
    }


def _artifact_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def record_kernel_image(config: Config, layout: ProjectLayout, image: Path) -> None:
    data = load(layout) or _new_stamp(config, "kernel")
    data["mode"] = "kernel"
    data["config_hash"] = config_digest(config)
    kernel = data.setdefault("kernel", {})
    kernel["image"] = _artifact_info(image) | {"config_hash": config_digest(config)}
    _save(layout, data)


def record_kernel_initrd(config: Config, layout: ProjectLayout, initrd: Path) -> None:
    data = load(layout) or _new_stamp(config, "kernel")
    data["mode"] = "kernel"
    data["config_hash"] = config_digest(config)
    kernel = data.setdefault("kernel", {})
    kernel["initrd"] = _artifact_info(initrd) | {"config_hash": config_digest(config)}
    _save(layout, data)


def save_cloud(
    config: Config,
    layout: ProjectLayout,
    *,
    base: Path,
    image_url: str,
    image_filename: str,
    image_checksum: str,
    seed_instance_id: str,
) -> None:
    data = _new_stamp(config, "cloud")
    data["cloud"] = {
        "image_url": image_url,
        "image_filename": image_filename,
        "image_checksum": image_checksum,
        "disk": config.vm.disk,
        "overlay": str(layout.overlay.resolve()),
        "overlay_backing": str(base.resolve()),
        "seed": str(layout.seed_iso.resolve()),
        "seed_instance_id": seed_instance_id,
    }
    _save(layout, data)


def _inspect_overlay(path: Path) -> tuple[Path | None, int | None]:
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        raise ProjectError("qemu-img not found; cannot verify the existing overlay")
    try:
        result = subprocess.run(
            [qemu_img, "info", "--output=json", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"cannot inspect overlay {path}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"cannot inspect overlay {path}: {detail}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProjectError(f"cannot parse qemu-img output for {path}: {exc}") from exc
    backing = data.get("backing-filename")
    size = data.get("virtual-size")
    return (
        Path(backing).resolve() if isinstance(backing, str) and backing else None,
        int(size) if isinstance(size, int) else None,
    )


def validate_cloud_build(
    config: Config,
    layout: ProjectLayout,
    *,
    base: Path,
    image_url: str,
    image_filename: str,
    image_checksum: str,
    force: bool,
) -> None:
    """Refuse to reuse an overlay that does not match the current build."""
    if force or not layout.overlay.is_file():
        return

    previous = load(layout)
    if previous is not None and previous.get("mode") == "cloud":
        cloud = previous.get("cloud")
        if not isinstance(cloud, dict):
            raise ProjectError("build stamp is missing cloud artifact data; run build --force")
        differences = []
        if cloud.get("disk") != config.vm.disk:
            differences.append(f"disk {cloud.get('disk')!r} -> {config.vm.disk!r}")
        if cloud.get("image_filename") != image_filename:
            differences.append(f"image {cloud.get('image_filename')!r} -> {image_filename!r}")
        if cloud.get("image_url") != image_url:
            differences.append(f"image URL {cloud.get('image_url')!r} -> {image_url!r}")
        if (
            image_checksum
            and cloud.get("image_checksum")
            and cloud["image_checksum"] != image_checksum
        ):
            differences.append("base image checksum changed")
        if differences:
            detail = "; ".join(differences)
            raise ProjectError(
                "existing overlay does not match the current configuration "
                f"({detail}); run `simp-vmgen build --force` to recreate it "
                "(guest data will be lost)"
            )
        return

    backing, size = _inspect_overlay(layout.overlay)
    expected_size = _size_bytes(config.vm.disk)
    if (
        backing is None
        or (backing != base.resolve())
        or (expected_size is not None and size is not None and size != expected_size)
    ):
        raise ProjectError(
            "existing overlay has no matching build stamp and does not match the "
            "current base image/disk size; run `simp-vmgen build --force` to recreate it "
            "(guest data will be lost)"
        )


def require_cloud_artifacts(config: Config, layout: ProjectLayout) -> None:
    stamp = load(layout)
    if stamp is None or stamp.get("mode") != "cloud":
        raise ProjectError("runtime artifacts have no build stamp; run `simp-vmgen build` first")
    if stamp.get("config_hash") != config_digest(config):
        raise ProjectError("configuration changed since the last build; run `simp-vmgen build`")
    cloud = stamp.get("cloud")
    if not isinstance(cloud, dict):
        raise ProjectError("build stamp is missing cloud artifact data; run `simp-vmgen build`")
    expected = {
        "overlay": layout.overlay.resolve(),
        "seed": layout.seed_iso.resolve(),
    }
    for name, path in expected.items():
        recorded = cloud.get(name)
        if not isinstance(recorded, str) or Path(recorded).resolve() != path:
            raise ProjectError(
                f"runtime artifact {name!r} does not match the build stamp; run `simp-vmgen build`"
            )
        if not path.is_file():
            raise ProjectError(f"runtime artifact is missing: {path}; run `simp-vmgen build`")


def _require_kernel_component(
    config: Config,
    stamp: dict[str, Any],
    name: str,
    expected: Path,
) -> None:
    kernel = stamp.get("kernel")
    if not isinstance(kernel, dict):
        raise ProjectError("kernel artifacts have no build stamp; run `simp-vmgen build`")
    entry = kernel.get(name)
    if not isinstance(entry, dict):
        raise ProjectError(f"kernel {name} has no build stamp; run `simp-vmgen build`")
    if entry.get("config_hash") != config_digest(config):
        raise ProjectError(f"kernel {name} is stale; rebuild it with `simp-vmgen build`")
    recorded = entry.get("path")
    if not isinstance(recorded, str) or Path(recorded).resolve() != expected.resolve():
        raise ProjectError(
            f"kernel {name} path does not match the build stamp; run `simp-vmgen build`"
        )
    try:
        stat = expected.stat()
    except OSError as exc:
        raise ProjectError(f"kernel {name} is missing: {expected}") from exc
    if entry.get("mtime_ns") != stat.st_mtime_ns or entry.get("size") != stat.st_size:
        raise ProjectError(f"kernel {name} changed since the last build; run `simp-vmgen build`")


def require_kernel_artifacts(
    config: Config, layout: ProjectLayout, image: Path, initrd: Path
) -> None:
    stamp = load(layout)
    if stamp is None or stamp.get("mode") != "kernel":
        raise ProjectError("kernel artifacts have no build stamp; run `simp-vmgen build` first")
    _require_kernel_component(config, stamp, "image", image)
    _require_kernel_component(config, stamp, "initrd", initrd)
