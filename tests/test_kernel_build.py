from __future__ import annotations

import json
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
        lambda *args, **kwargs: KernelSourceInfo(mode="tarball", path=source, version="6.1.0"),
    )
    assert kernel.kernel_build(SimpleNamespace(directory=str(tmp_path), dry_run=False)) == 0
    assert (layout.kernel_dir / "build" / "arch/x86/boot/bzImage").is_file()
    assert (layout.kernel_dir / "modules" / "marker").is_file()
    from simp_vmgen import stamp

    recorded = stamp.load(layout)
    assert recorded is not None
    assert recorded["mode"] == "kernel"
    assert recorded["kernel"]["image"]["path"].endswith("arch/x86/boot/bzImage")


def test_kernel_build_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    config = _config(tmp_path, ["bzImage"])
    layout = ProjectLayout(tmp_path, config)
    monkeypatch.setattr(
        kernel,
        "resolve_source",
        lambda *args, **kwargs: KernelSourceInfo(mode="tarball", path=source, version="6.1.0"),
    )
    assert kernel.kernel_build(SimpleNamespace(directory=str(tmp_path), dry_run=True)) == 0
    assert not (layout.kernel_dir / "build" / "arch/x86/boot/bzImage").exists()
    assert "would build kernel" in capsys.readouterr().out


def test_kernel_status(tmp_path: Path, capsys) -> None:
    _config(tmp_path, ["bzImage"])
    assert kernel.kernel_status(SimpleNamespace(directory=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "mode    : build" in out
    assert "source  : tarball 6.1.0" in out
    assert "image   :" in out
    assert "initrd  :" in out


def test_kernel_status_json(tmp_path: Path, capsys) -> None:
    _config(tmp_path, ["bzImage"])
    assert kernel.kernel_status(SimpleNamespace(directory=str(tmp_path), json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["enabled"] is True
    assert data["mode"] == "build"
    assert data["source"]["kind"] == "tarball"
    assert data["source"]["version"] == "6.1.0"
    assert data["rootfs"]["name"] == "busybox"
    assert data["image"]["path"].endswith("arch/x86/boot/bzImage")
    assert data["initrd"]["path"].endswith("initramfs.cpio.gz")
    assert data["lock"] is None


def test_kernel_clean(tmp_path: Path) -> None:
    config = _config(tmp_path, ["bzImage"])
    layout = ProjectLayout(tmp_path, config)
    (layout.kernel_dir / "build").mkdir(parents=True)
    (layout.kernel_dir / "build" / "marker").write_text("x")
    assert kernel.kernel_clean(SimpleNamespace(directory=str(tmp_path))) == 0
    assert not (layout.kernel_dir / "build").exists()


def test_ensure_kernel_artifacts_rebuilds_without_fresh_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, ["bzImage"])
    layout = ProjectLayout(tmp_path, config)
    image = layout.kernel_dir / "build" / "arch/x86/boot/bzImage"
    initrd = layout.kernel_dir / "initramfs.cpio.gz"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"old")
    initrd.parent.mkdir(parents=True, exist_ok=True)
    initrd.write_bytes(b"initrd")
    calls: list[str] = []

    def fake_build_kernel(*args: object, **kwargs: object) -> Path:
        calls.append("image")
        image.write_bytes(b"new")
        return image

    monkeypatch.setattr(kernel, "build_kernel", fake_build_kernel)
    monkeypatch.setattr(kernel, "build_initramfs", lambda *args, **kwargs: initrd)

    artifacts = kernel.ensure_kernel_artifacts(config, tmp_path, layout)
    assert artifacts.image == image
    assert calls == ["image"]

    artifacts = kernel.ensure_kernel_artifacts(config, tmp_path, layout)
    assert artifacts.image == image
    assert calls == ["image"]


def test_kernel_build_failure_reports_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        "\t@echo boom\n"
        "\t@exit 1\n"
    )
    config = _config(tmp_path, ["bzImage"])
    layout = ProjectLayout(tmp_path, config)
    monkeypatch.setattr(
        kernel,
        "resolve_source",
        lambda *args, **kwargs: KernelSourceInfo(mode="tarball", path=source, version="6.1.0"),
    )
    with pytest.raises(ProjectError, match="kernel-build.log"):
        kernel.kernel_build(SimpleNamespace(directory=str(tmp_path), dry_run=False))
    log = layout.logs_dir / "kernel-build.log"
    assert log.is_file()
    assert "boom" in log.read_text(encoding="utf-8")
