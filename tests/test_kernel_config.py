from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import kernel
from simp_vmgen.config import Config, KernelConfig
from simp_vmgen.errors import ProjectError
from simp_vmgen.kernel_source import KernelSourceInfo
from simp_vmgen.layout import ProjectLayout


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "linux"
    source.mkdir()
    (source / "Makefile").write_text(
        "O ?= .\n"
        "defconfig:\n"
        "\tmkdir -p $(O)\n"
        "\tprintf 'CONFIG_BASE=n\\n' > $(O)/.config\n"
        "olddefconfig:\n"
        "\tprintf '# resolved\\n' >> $(O)/.config\n"
    )
    return source


def _config(tmp_path: Path) -> Config:
    config = Config.default("lab")
    config.kernel = KernelConfig(
        enabled=True,
        version="6.1.0",
        fragments=["fragment.config"],
        options={"CONFIG_DEBUG_INFO": True, "CONFIG_KASAN": "y"},
        make_args=["LLVM=0"],
    )
    config.validate()
    (tmp_path / "fragment.config").write_text("CONFIG_FRAGMENT=y\n")
    config.save(tmp_path / "vmgen.toml")
    return config


def test_configure_kernel(tmp_path: Path) -> None:
    source = _source(tmp_path)
    config = _config(tmp_path)
    layout = ProjectLayout(tmp_path, config)
    config_file = kernel.configure_kernel(config, tmp_path, source, layout)

    text = config_file.read_text()
    assert "CONFIG_BASE=n" in text
    assert "CONFIG_9P_FS=y" in text
    assert "CONFIG_VIRTIO_PCI=y" in text
    assert "CONFIG_DEBUG_INFO=y" in text
    assert "CONFIG_KASAN=y" in text
    assert "CONFIG_FRAGMENT=y" in text
    assert "# resolved" in text


def test_kernel_config_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    config = _config(tmp_path)
    layout = ProjectLayout(tmp_path, config)
    monkeypatch.setattr(
        kernel,
        "resolve_source",
        lambda *args, **kwargs: KernelSourceInfo(mode="tarball", path=source, version="6.1.0"),
    )
    args = SimpleNamespace(directory=str(tmp_path), dry_run=True)
    assert kernel.kernel_config(args) == 0
    assert not (kernel.kernel_build_dir(layout) / ".config").exists()
    assert "would generate kernel config" in capsys.readouterr().out


def test_kernel_config_rejects_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "bzImage"
    artifact.write_bytes(b"x")
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, mode="artifact", artifact=str(artifact))
    config.validate()
    config.save(tmp_path / "vmgen.toml")
    with pytest.raises(ProjectError, match="artifact mode"):
        kernel.kernel_config(SimpleNamespace(directory=str(tmp_path), dry_run=False))


def test_kernel_config_rejects_menuconfig(tmp_path: Path) -> None:
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.1.0", config="menuconfig")
    config.validate()
    with pytest.raises(ProjectError, match="menuconfig"):
        kernel._kernel_config_file(config, tmp_path)
