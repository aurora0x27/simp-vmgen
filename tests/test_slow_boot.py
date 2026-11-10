from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import runtime
from simp_vmgen.cli import main
from simp_vmgen.config import Config, NetworkConfig

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("SIMP_VMGEN_SLOW") != "1",
        reason="set SIMP_VMGEN_SLOW=1 to run boot integration tests",
    ),
]


def _args(root: Path, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(directory=str(root), **kwargs)


def _stop(root: Path) -> None:
    with contextlib.suppress(Exception):
        runtime.stop(_args(root))


def test_alpine_cloud_image_boot(tmp_path: Path) -> None:
    if shutil.which("qemu-system-x86_64") is None or shutil.which("qemu-img") is None:
        pytest.skip("QEMU is not installed")
    if not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")):
        pytest.skip("no cloud-init seed tool")

    root = tmp_path / "alpine-lab"
    assert (
        main(
            [
                "new",
                str(root),
                "--distro",
                "alpine",
                "--mem",
                "2G",
                "--cpus",
                "2",
            ]
        )
        == 0
    )
    # Keep the boot test focused: package installation is covered separately
    # and can be slow/flaky on a fresh Alpine mirror.
    config = Config.load(root / "vmgen.toml")
    config.provision.package_sets = []
    config.save(root / "vmgen.toml")

    try:
        assert runtime.build(_args(root, dry_run=False, force=False)) == 0
        assert runtime.run(_args(root, dry_run=False, foreground=False, snapshot=False)) == 0
        assert runtime.wait(_args(root, timeout=300)) == 0
        assert runtime.guest(_args(root, guest_command=["sh", "-c", "uname -s"])) == 0
    finally:
        _stop(root)


def test_mixed_cloud_network(tmp_path: Path) -> None:
    if shutil.which("qemu-system-x86_64") is None or shutil.which("qemu-img") is None:
        pytest.skip("QEMU is not installed")
    if not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")):
        pytest.skip("no cloud-init seed tool")

    nodes = {
        "ubuntu": ("192.168.88.10/24", "192.168.88.11"),
        "alpine": ("192.168.88.11/24", "192.168.88.10"),
    }
    roots: dict[str, Path] = {}
    for distro, (address, _peer) in nodes.items():
        root = tmp_path / distro
        roots[distro] = root
        assert (
            main(
                [
                    "new",
                    str(root),
                    "--distro",
                    distro,
                    "--mem",
                    "1G",
                    "--cpus",
                    "1",
                ]
            )
            == 0
        )
        config = Config.load(root / "vmgen.toml")
        config.provision.package_sets = []
        config.networks = [NetworkConfig("lan", address=address, mcast="239.192.88.1:4789")]
        config.validate()
        config.save(root / "vmgen.toml")

    try:
        for root in roots.values():
            assert runtime.build(_args(root, dry_run=False, force=False)) == 0
        for root in roots.values():
            assert runtime.run(_args(root, dry_run=False, foreground=False, snapshot=False)) == 0
        for root in roots.values():
            assert runtime.wait(_args(root, timeout=600)) == 0
        for distro, (_address, peer) in nodes.items():
            assert (
                runtime.guest(
                    _args(
                        roots[distro],
                        guest_command=["ping", "-c", "3", "-W", "2", peer],
                    )
                )
                == 0
            )
    finally:
        for root in roots.values():
            _stop(root)


def test_compose_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("qemu-system-x86_64") is None or shutil.which("qemu-img") is None:
        pytest.skip("QEMU is not installed")
    if not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")):
        pytest.skip("no cloud-init seed tool")

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "compose"
    for name in ("a", "b"):
        service = root / name
        assert (
            main(
                [
                    "new",
                    str(service),
                    "--distro",
                    "alpine",
                    "--mem",
                    "768M",
                    "--cpus",
                    "1",
                ]
            )
            == 0
        )
        config = Config.load(service / "vmgen.toml")
        config.provision.package_sets = []
        config.save(service / "vmgen.toml")
    (root / "vmgen-compose.toml").write_text(
        """schema = 1

[project]
name = "slow-compose"

[networks.lan]
subnet = "192.168.92.0/24"

[services.a]
project = "./a"

[services.a.networks.lan]
address = "192.168.92.10/24"

[services.b]
project = "./b"

[services.b.networks.lan]
address = "192.168.92.11/24"
""",
        encoding="utf-8",
    )
    try:
        assert main(["-C", str(root), "compose", "up", "--timeout", "300"]) == 0
        assert (
            main(
                [
                    "-C",
                    str(root),
                    "compose",
                    "guest",
                    "a",
                    "--",
                    "ping",
                    "-c",
                    "2",
                    "-W",
                    "2",
                    "192.168.92.11",
                ]
            )
            == 0
        )
        assert main(["-C", str(root), "compose", "status", "--json"]) == 0
        assert main(["-C", str(root), "compose", "wait", "--timeout", "60"]) == 0
    finally:
        with contextlib.suppress(Exception):
            main(["-C", str(root), "compose", "down"])


def test_alpine_rootfs_with_kernel_artifact(tmp_path: Path) -> None:
    kernel_image = Path("/boot/vmlinuz-linux")
    if not kernel_image.is_file():
        pytest.skip("no host vmlinuz to boot")
    if shutil.which("qemu-system-x86_64") is None or shutil.which("cpio") is None:
        pytest.skip("QEMU or cpio is not installed")

    root = tmp_path / "alpine-kernel-rootfs"
    assert (
        main(
            [
                "new",
                str(root),
                "--distro",
                "alpine",
                "--kernel-artifact",
                str(kernel_image),
                "--kernel-artifact-type",
                "vmlinuz",
                "--rootfs",
                "alpine",
                "--mem",
                "2G",
                "--cpus",
                "2",
            ]
        )
        == 0
    )
    config = Config.load(root / "vmgen.toml")
    config.provision.package_sets = []
    config.save(root / "vmgen.toml")
    try:
        assert runtime.build(_args(root, dry_run=False, force=False)) == 0
        assert runtime.run(_args(root, dry_run=False, foreground=False, snapshot=False)) == 0
        assert runtime.wait(_args(root, timeout=300)) == 0
    finally:
        _stop(root)


def test_kernel_artifact_boot(tmp_path: Path) -> None:
    kernel_image = Path("/boot/vmlinuz-linux")
    if not kernel_image.is_file():
        pytest.skip("no host vmlinuz to boot")
    if shutil.which("qemu-system-x86_64") is None or shutil.which("cpio") is None:
        pytest.skip("QEMU or cpio is not installed")

    root = tmp_path / "kernel-artifact"
    assert (
        main(
            [
                "new",
                str(root),
                "--kernel-artifact",
                str(kernel_image),
                "--kernel-artifact-type",
                "vmlinuz",
                "--mem",
                "1G",
                "--cpus",
                "1",
            ]
        )
        == 0
    )
    try:
        assert runtime.build(_args(root, dry_run=False, force=False)) == 0
        assert runtime.run(_args(root, dry_run=False, foreground=False, snapshot=False)) == 0
        assert runtime.wait(_args(root, timeout=120)) == 0
    finally:
        _stop(root)
