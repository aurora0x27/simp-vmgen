from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from simp_vmgen import rootfs_source
from simp_vmgen.config import Config, KernelConfig
from simp_vmgen.errors import ProjectError
from simp_vmgen.layout import ProjectLayout
from simp_vmgen.lock import Lock
from simp_vmgen.rootfs import load_rootfs


def _config(tmp_path: Path) -> Config:
    config = Config.default("lab")
    config.distro.name = "alpine"
    config.distro.release = "3.22"
    config.kernel = KernelConfig(enabled=True, version="6.1.0", rootfs="alpine")
    config.validate()
    config.save(tmp_path / "vmgen.toml")
    return config


def _rootfs_tarball(tmp_path: Path) -> Path:
    source = tmp_path / "minirootfs"
    (source / "etc").mkdir(parents=True)
    (source / "etc" / "alpine-release").write_text("3.22.0\n")
    (source / "bin").mkdir()
    (source / "bin" / "busybox").write_text("busybox\n")
    archive = tmp_path / "alpine-minirootfs-3.22.0-x86_64.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for path in sorted(source.rglob("*")):
            handle.add(path, arcname=path.relative_to(source))
    return archive


def test_alpine_rootfs_metadata() -> None:
    spec = load_rootfs("alpine")
    assert spec.distro == "alpine"
    assert spec.kind == "initramfs"
    assert "openssh" in spec.packages
    assert spec.source_name().endswith(".tar.gz")


def test_fetch_rootfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    layout = ProjectLayout(tmp_path, config)
    archive = _rootfs_tarball(tmp_path)
    monkeypatch.setattr(
        rootfs_source,
        "download",
        lambda *args, **kwargs: (archive, f"sha256:{'a' * 64}"),
    )
    info = rootfs_source.fetch_rootfs(config, tmp_path, layout)
    assert (info.root_dir / "etc" / "alpine-release").read_text() == "3.22.0\n"
    assert info.checksum == f"sha256:{'a' * 64}"

    rootfs_source.update_lock(tmp_path, info)
    lock = Lock.load(tmp_path / "vmgen.lock")
    assert lock.rootfs.source == "alpine"
    assert lock.rootfs.kind == "initramfs"
    assert lock.rootfs.checksum == f"sha256:{'a' * 64}"


def test_rootfs_distro_mismatch(tmp_path: Path) -> None:
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.1.0", rootfs="alpine")
    config.validate()
    layout = ProjectLayout(tmp_path, config)
    with pytest.raises(ProjectError, match="expects distro"):
        rootfs_source.fetch_rootfs(config, tmp_path, layout)
