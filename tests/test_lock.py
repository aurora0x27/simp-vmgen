from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.config import Config
from simp_vmgen.distros import load_distro
from simp_vmgen.errors import LockError
from simp_vmgen.lock import Lock, create_lock


def test_create_lock_for_ubuntu() -> None:
    config = Config.default("lab")
    distro = load_distro("ubuntu")
    lock = create_lock(config, distro, distro.release())

    assert lock.image_url.endswith("noble-server-cloudimg-amd64.img")
    assert lock.image_filename == "noble-server-cloudimg-amd64.img"
    assert lock.image_checksum == ""
    assert lock.image_checksum_url.endswith("/SHA256SUMS")


def test_lock_roundtrip(tmp_path: Path) -> None:
    lock = Lock(
        image_url="https://example.invalid/image.qcow2",
        image_filename="image.qcow2",
        image_checksum="sha256:" + "a" * 64,
        image_checksum_url="https://example.invalid/SHA256SUMS",
    )
    path = tmp_path / "vmgen.lock"
    lock.save(path)
    assert Lock.load(path) == lock
    assert Lock.loads(lock.dumps()) == lock


def test_lock_validation() -> None:
    with pytest.raises(LockError, match="unsupported schema"):
        Lock.loads("schema = 2\n[image]\nurl = \"x\"\n")
    with pytest.raises(LockError, match="unknown key"):
        Lock.loads("schema = 1\n[image]\nextra = 1\n")
    with pytest.raises(LockError, match="invalid TOML"):
        Lock.loads("schema = 1\n[image\n")


def test_new_writes_lock(tmp_path: Path) -> None:
    from simp_vmgen.cli import main

    target = tmp_path / "lab"
    assert main(["new", str(target)]) == 0
    lock = Lock.load(target / "vmgen.lock")
    assert lock.image_url.endswith("noble-server-cloudimg-amd64.img")
    assert lock.image_filename == "noble-server-cloudimg-amd64.img"
