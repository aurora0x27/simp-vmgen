from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.config import Config, MountConfig
from simp_vmgen.distros import load_distro
from simp_vmgen.errors import ProjectError
from simp_vmgen.layout import ProjectLayout
from simp_vmgen.qemu import build_argv, resolve_accel


def _layout(tmp_path: Path) -> ProjectLayout:
    (tmp_path / "work").mkdir()
    (tmp_path / "src").mkdir()
    config = Config.default("lab")
    config.mounts.append(MountConfig("src", "/src", readonly=True))
    return ProjectLayout(tmp_path, config)


def test_default_qemu_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simp_vmgen.qemu.kvm_available", lambda: True)
    layout = _layout(tmp_path)
    distro = load_distro("ubuntu")
    argv = build_argv(
        layout.config,
        distro,
        distro.release(),
        layout,
        ssh_port=2222,
        gdb_port=None,
    )
    assert argv[0] == "qemu-system-x86_64"
    assert "-machine" in argv
    assert "q35,accel=kvm" in argv
    assert "-cpu" in argv and "host" in argv
    assert any(f"file={layout.overlay}" in item for item in argv)
    assert any(f"file={layout.seed_iso}" in item for item in argv)
    assert "user,id=n0,hostfwd=tcp:127.0.0.1:2222-:22" in argv
    assert "-display" in argv and "none" in argv
    assert "-daemonize" in argv
    assert "-virtfs" in argv
    assert any("mount_tag=host0" in item for item in argv)
    assert any("mount_tag=host1" in item and "readonly=on" in item for item in argv)
    assert "-gdb" not in argv


def test_gdb_foreground_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("simp_vmgen.qemu.kvm_available", lambda: False)
    layout = _layout(tmp_path)
    layout.config.gdb.enabled = True
    layout.config.gdb.wait = True
    distro = load_distro("ubuntu")
    argv = build_argv(
        layout.config,
        distro,
        distro.release(),
        layout,
        ssh_port=2222,
        gdb_port=1234,
        foreground=True,
        snapshot=True,
    )
    assert "q35,accel=tcg" in argv
    assert "-gdb" in argv and "tcp:127.0.0.1:1234" in argv
    assert "-S" in argv
    assert "-snapshot" in argv
    assert "-daemonize" not in argv


def test_forced_kvm_fails_without_kvm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("simp_vmgen.qemu.kvm_available", lambda: False)
    layout = _layout(tmp_path)
    layout.config.vm.accel = "kvm"
    with pytest.raises(ProjectError, match="KVM"):
        resolve_accel(layout.config)


def test_alpine_machine_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simp_vmgen.qemu.kvm_available", lambda: False)
    layout = _layout(tmp_path)
    layout.config.distro.name = "alpine"
    layout.config.distro.release = "3.22"
    distro = load_distro("alpine")
    argv = build_argv(
        layout.config,
        distro,
        distro.release(),
        layout,
        ssh_port=2222,
        gdb_port=None,
    )
    assert "pc,accel=tcg" in argv
