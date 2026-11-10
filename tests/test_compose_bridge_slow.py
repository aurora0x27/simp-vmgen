from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

import pytest

from simp_vmgen.cli import main
from simp_vmgen.compose_network import bridge_helper
from simp_vmgen.config import Config

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("SIMP_VMGEN_PRIVILEGED") != "1",
        reason="set SIMP_VMGEN_PRIVILEGED=1 to run privileged bridge tests",
    ),
    pytest.mark.skipif(
        not hasattr(os, "geteuid") or os.geteuid() != 0,
        reason="privileged bridge test requires root",
    ),
]


def test_bridge_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("qemu-system-x86_64") is None or shutil.which("ip") is None:
        pytest.skip("QEMU or iproute2 is missing")
    if bridge_helper() is None or not Path("/dev/net/tun").exists():
        pytest.skip("qemu-bridge-helper or /dev/net/tun is missing")
    if not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")):
        pytest.skip("no cloud-init seed tool")

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "bridge-lab"
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
name = "bridge-lab"

[networks.lan]
driver = "bridge"
subnet = "192.168.96.0/24"

[services.a]
project = "./a"

[services.a.networks.lan]
address = "192.168.96.10/24"

[services.b]
project = "./b"

[services.b.networks.lan]
address = "192.168.96.11/24"
""",
        encoding="utf-8",
    )
    try:
        assert main(["-C", str(root), "compose", "network", "prepare"]) == 0
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
                    "192.168.96.11",
                ]
            )
            == 0
        )
        assert main(["-C", str(root), "compose", "network", "status", "--json"]) == 0
    finally:
        with contextlib.suppress(Exception):
            main(["-C", str(root), "compose", "down"])
        with contextlib.suppress(Exception):
            main(["-C", str(root), "compose", "network", "cleanup"])
