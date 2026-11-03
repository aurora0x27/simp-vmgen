from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from simp_vmgen import kernel, qemu
from simp_vmgen.config import Config, KernelConfig
from simp_vmgen.distros import load_distro
from simp_vmgen.layout import ProjectLayout
from simp_vmgen.mounts import ResolvedMount

GOLDEN = Path(__file__).parent / "golden" / "qemu"
MOUNTS = [ResolvedMount(Path("/host/work"), "/work", False, "host0")]


def _argv(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    *,
    kvm: bool,
    ssh_port: int,
    gdb_port: int | None = None,
    foreground: bool = False,
) -> list[str]:
    distro = load_distro(config.distro.name)
    layout = ProjectLayout(Path("/project"), config)
    monkeypatch.setattr(qemu, "kvm_available", lambda: kvm)
    monkeypatch.setattr(qemu, "resolve_mounts", lambda *args, **kwargs: MOUNTS)
    monkeypatch.setattr(kernel, "kernel_image_path", lambda *args: Path("/images/bzImage"))
    monkeypatch.setattr(kernel, "kernel_initrd_path", lambda *args: Path("/images/initrd"))
    return qemu.build_argv(
        config,
        distro,
        distro.release(),
        layout,
        ssh_port=ssh_port,
        gdb_port=gdb_port,
        foreground=foreground,
    )


def _assert_snapshot(name: str, argv: list[str]) -> None:
    expected = (GOLDEN / name).read_text(encoding="utf-8").strip()
    assert shlex.join(argv) == expected


def test_cloudimg_tcg_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_snapshot(
        "cloudimg-tcg.args",
        _argv(monkeypatch, Config.default("golden"), kvm=False, ssh_port=2222),
    )


def test_cloudimg_kvm_gdb_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.default("golden")
    config.gdb.enabled = True
    config.gdb.wait = True
    _assert_snapshot(
        "cloudimg-kvm-gdb.args",
        _argv(
            monkeypatch,
            config,
            kvm=True,
            ssh_port=2223,
            gdb_port=1234,
            foreground=True,
        ),
    )


def test_kernel_artifact_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.default("golden")
    config.kernel = KernelConfig(
        enabled=True,
        mode="artifact",
        artifact="/images/bzImage",
        initrd="/images/initrd",
        append="loglevel=7",
    )
    config.validate()
    _assert_snapshot(
        "kernel-artifact-tcg.args",
        _argv(monkeypatch, config, kvm=False, ssh_port=2222),
    )
