from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from simp_vmgen import kernel, qemu
from simp_vmgen.config import Config, KernelConfig, NetworkConfig
from simp_vmgen.distros import load_distro
from simp_vmgen.errors import ProjectError
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


def test_cloudimg_networks_tcg_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.default("golden")
    config.networks = [
        NetworkConfig(
            "lan",
            address="192.168.77.10/24",
            gateway="192.168.77.1",
            mcast="239.192.77.1:4789",
        ),
        NetworkConfig("back", dhcp=True, mcast="239.192.77.2:4789"),
    ]
    config.validate()
    _assert_snapshot(
        "cloudimg-networks-tcg.args",
        _argv(monkeypatch, config, kvm=False, ssh_port=2222),
    )


def test_cross_arch_accel(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.default("golden")
    config.distro.arch = "aarch64"
    monkeypatch.setattr(qemu, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(qemu, "kvm_available", lambda: True)
    assert qemu.resolve_accel(config) == ("tcg", "max")
    config.vm.accel = "kvm"
    with pytest.raises(ProjectError, match="KVM cannot run"):
        qemu.resolve_accel(config)


def test_cloudimg_aarch64_tcg_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.default("golden")
    config.distro.arch = "aarch64"
    config.vm.accel = "tcg"
    config.validate()
    monkeypatch.setattr(qemu, "firmware_path", lambda *args: Path("/firmware/QEMU_EFI.fd"))
    _assert_snapshot(
        "cloudimg-aarch64-tcg.args",
        _argv(monkeypatch, config, kvm=False, ssh_port=2222),
    )


def test_kernel_artifact_aarch64_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.default("golden")
    config.distro.arch = "aarch64"
    config.vm.accel = "tcg"
    config.kernel = KernelConfig(
        enabled=True,
        mode="artifact",
        artifact="/images/Image",
        artifact_type="Image",
        initrd="/images/initrd",
    )
    config.validate()
    _assert_snapshot(
        "kernel-artifact-aarch64-tcg.args",
        _argv(monkeypatch, config, kvm=False, ssh_port=2222),
    )


def test_cloudimg_bridge_tcg_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.default("golden")
    config.networks = [
        NetworkConfig("lan", address="192.168.77.10/24", bridge="br0"),
    ]
    config.validate()
    _assert_snapshot(
        "cloudimg-bridge-tcg.args",
        _argv(monkeypatch, config, kvm=False, ssh_port=2222),
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
