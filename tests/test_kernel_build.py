from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import kernel
from simp_vmgen.config import Config, KernelConfig
from simp_vmgen.kernel_source import KernelSourceInfo
from simp_vmgen.layout import ProjectLayout


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "linux"
    source.mkdir()
    (source / "Makefile").write_text(
        "O ?= .\n"
        "defconfig:\n"
        "\tmkdir -p $(O)\n"
        "\tprintf 'CONFIG_BASE=y\\n' > $(O)/.config\n"
        "olddefconfig:\n"
        "\ttrue\n"
        "bzImage:\n"
        "\tmkdir -p $(O)/arch/x86/boot\n"
        "\tprintf 'bzImage\\n' > $(O)/arch/x86/boot/bzImage\n"
        "vmlinux:\n"
        "\tprintf 'vmlinux\\n' > $(O)/vmlinux\n"
        "modules:\n"
        "\tprintf 'modules\\n' > $(O)/modules.builtin\n"
        "modules_install:\n"
        "\tmkdir -p $(INSTALL_MOD_PATH)\n"
        "\tprintf 'installed\\n' > $(INSTALL_MOD_PATH)/marker\n"
    )
    return source


def _config(tmp_path: Path, targets: list[str]) -> Config:
    config = Config.default("lab")
    config.kernel = KernelConfig(
        enabled=True,
        version="6.1.0",
        targets=targets,
    )
    config.validate()
    config.save(tmp_path / "vmgen.toml")
    return config


def test_kernel_build_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path)
    config = _config(tmp_path, ["bzImage", "modules"])
    layout = ProjectLayout(tmp_path, config)
    monkeypatch.setattr(
        kernel,
        "resolve_source",
        lambda *args, **kwargs: KernelSourceInfo(
            mode="tarball", path=source, version="6.1.0"
        ),
    )
    assert kernel.kernel_build(SimpleNamespace(directory=str(tmp_path), dry_run=False)) == 0
    assert (layout.kernel_dir / "build" / "arch/x86/boot/bzImage").is_file()
    assert (layout.kernel_dir / "modules" / "marker").is_file()


def test_kernel_build_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    config = _config(tmp_path, ["bzImage"])
    layout = ProjectLayout(tmp_path, config)
    monkeypatch.setattr(
        kernel,
        "resolve_source",
        lambda *args, **kwargs: KernelSourceInfo(
            mode="tarball", path=source, version="6.1.0"
        ),
    )
    assert kernel.kernel_build(SimpleNamespace(directory=str(tmp_path), dry_run=True)) == 0
    assert not (layout.kernel_dir / "build" / "arch/x86/boot/bzImage").exists()
    assert "would build kernel" in capsys.readouterr().out


def test_kernel_clean(tmp_path: Path) -> None:
    config = _config(tmp_path, ["bzImage"])
    layout = ProjectLayout(tmp_path, config)
    (layout.kernel_dir / "build").mkdir(parents=True)
    (layout.kernel_dir / "build" / "marker").write_text("x")
    assert kernel.kernel_clean(SimpleNamespace(directory=str(tmp_path))) == 0
    assert not (layout.kernel_dir / "build").exists()
