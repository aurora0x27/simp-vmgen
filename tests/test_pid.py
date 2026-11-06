from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from simp_vmgen import runtime
from simp_vmgen.cli import main
from simp_vmgen.config import Config


def test_pid_matches_qemu_accepts_qemu_shaped_process(tmp_path: Path) -> None:
    sleep = shutil.which("sleep")
    assert sleep is not None
    qemu = tmp_path / "qemu-system-x86_64"
    shutil.copy2(sleep, qemu)
    process = subprocess.Popen([str(qemu), "30"])
    try:
        cmdline = b""
        for _ in range(50):
            cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes()
            if cmdline:
                break
            time.sleep(0.01)
        assert runtime._pid_matches_qemu(process.pid), cmdline
    finally:
        process.kill()
        process.wait()


def test_running_pid_ignores_unrelated_process(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    layout = runtime.ProjectLayout(root, Config.load(root / "vmgen.toml"))
    layout.ensure()
    process = subprocess.Popen(["sleep", "30"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        assert not runtime._pid_matches_qemu(process.pid)
        assert runtime.running_pid(layout) is None

        assert runtime.stop(SimpleNamespace(directory=str(root))) == 0
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
