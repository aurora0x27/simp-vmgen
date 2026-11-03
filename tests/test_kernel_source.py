from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from simp_vmgen import kernel_source
from simp_vmgen.config import Config, KernelConfig
from simp_vmgen.errors import ConfigError, ProjectError
from simp_vmgen.layout import ProjectLayout
from simp_vmgen.lock import KernelLock, Lock


def _kernel_config(tmp_path: Path, **kwargs) -> Config:
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, **kwargs)
    config.validate()
    return config


def test_kernel_urls() -> None:
    assert kernel_source.kernel_url("6.12.5") == (
        "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.5.tar.xz"
    )
    assert kernel_source.kernel_checksum_url("6.12.5").endswith("v6.x/sha256sums.asc")


def test_artifact_mode(tmp_path: Path) -> None:
    artifact = tmp_path / "bzImage"
    artifact.write_bytes(b"kernel")
    config = _kernel_config(tmp_path, mode="artifact", artifact=str(artifact))
    layout = ProjectLayout(tmp_path, config)
    info = kernel_source.resolve_source(config, tmp_path, layout)
    assert info.mode == "artifact"
    assert info.path == artifact
    assert info.checksum.startswith("sha256:")

    lock = Lock()
    kernel_source.apply_to_lock(lock, info)
    assert lock.kernel == KernelLock(
        source="artifact",
        artifact=str(artifact),
        artifact_checksum=info.checksum,
    )


def test_missing_artifact(tmp_path: Path) -> None:
    config = _kernel_config(
        tmp_path, mode="artifact", artifact=str(tmp_path / "missing")
    )
    layout = ProjectLayout(tmp_path, config)
    with pytest.raises(ProjectError, match="artifact not found"):
        kernel_source.resolve_source(config, tmp_path, layout)


def test_tarball_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "fake-linux"
    (source_root / "linux-6.1.0").mkdir(parents=True)
    (source_root / "linux-6.1.0" / "Makefile").write_text("all:\n")
    tarball = tmp_path / "linux-6.1.0.tar.xz"
    with tarfile.open(tarball, "w:xz") as archive:
        archive.add(source_root / "linux-6.1.0", arcname="linux-6.1.0")

    config = _kernel_config(tmp_path, version="6.1.0")
    layout = ProjectLayout(tmp_path, config)
    monkeypatch.setattr(
        kernel_source,
        "download",
        lambda *args, **kwargs: (tarball, f"sha256:{'a' * 64}"),
    )
    info = kernel_source.resolve_source(config, tmp_path, layout)
    assert info.mode == "tarball"
    assert info.path == layout.kernel_dir / "src" / "linux-6.1.0"
    assert (info.path / "Makefile").is_file()

    lock = Lock()
    kernel_source.apply_to_lock(lock, info)
    assert lock.kernel.source == "tarball"
    assert lock.kernel.version == "6.1.0"
    assert lock.kernel.checksum == f"sha256:{'a' * 64}"


def test_kernel_validation() -> None:
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.12.5", commit="a" * 40)
    with pytest.raises(ConfigError, match="mutually exclusive"):
        config.validate()

    config.kernel = KernelConfig(enabled=True, mode="artifact")
    with pytest.raises(ConfigError, match="artifact"):
        config.validate()

    config.kernel = KernelConfig(
        enabled=True, mode="artifact", artifact="/tmp/kernel", artifact_type="bad"
    )
    with pytest.raises(ConfigError, match="artifact_type"):
        config.validate()

    config.kernel = KernelConfig(
        enabled=True, version="6.12.5", options={"CONFIG_DEBUG_INFO": True}
    )
    config.validate()
    assert config.kernel.options == {"CONFIG_DEBUG_INFO": "y"}
