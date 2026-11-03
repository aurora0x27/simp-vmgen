from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.errors import ProjectError
from simp_vmgen.rootfs import available_rootfs, load_rootfs, load_rootfs_from_path


def test_available_rootfs() -> None:
    assert available_rootfs() == ["busybox"]


def test_busybox_metadata() -> None:
    spec = load_rootfs("busybox")
    assert spec.name == "busybox"
    assert spec.kind == "initramfs"
    assert spec.source_name() == "busybox-1.35.0-x86_64-linux-musl"
    assert spec.source_checksum.startswith("sha256:")


def test_load_rootfs_from_path(tmp_path: Path) -> None:
    path = tmp_path / "custom"
    path.mkdir()
    (path / "rootfs.toml").write_text(
        """
name = "custom"
display_name = "Custom rootfs"
kind = "initramfs"
templates = "templates"
init = "/init"
source_url = "https://example.invalid/rootfs.tar.gz"
""",
        encoding="utf-8",
    )
    spec = load_rootfs_from_path(path)
    assert spec.name == "custom"
    assert spec.source_name() == "rootfs.tar.gz"


def test_invalid_rootfs_metadata(tmp_path: Path) -> None:
    path = tmp_path / "bad"
    path.mkdir()
    (path / "rootfs.toml").write_text(
        'name = "bad"\nkind = "bad"\n', encoding="utf-8"
    )
    with pytest.raises(ProjectError, match="kind"):
        load_rootfs_from_path(path)
