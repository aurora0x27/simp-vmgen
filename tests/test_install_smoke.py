from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.install,
    pytest.mark.skipif(
        os.environ.get("SIMP_VMGEN_INSTALL_TEST") != "1",
        reason="set SIMP_VMGEN_INSTALL_TEST=1 to run the install smoke test",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_uv_tool_install(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")

    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("*.whl"))
    tools = tmp_path / "tools"
    bins = tmp_path / "bin"
    env = os.environ.copy()
    env["UV_TOOL_DIR"] = str(tools)
    env["UV_TOOL_BIN_DIR"] = str(bins)

    subprocess.run(
        ["uv", "tool", "install", "--force", str(wheel)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    binary = bins / "simp-vmgen"
    assert binary.is_file()
    version = subprocess.run(
        [str(binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "simp-vmgen" in version.stdout

    help_output = subprocess.run(
        [str(binary), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Generate reproducible QEMU lab environments." in help_output.stdout
