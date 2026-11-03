from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import runtime
from simp_vmgen.cli import main
from simp_vmgen.errors import ProjectError
from simp_vmgen.net import resolve_port
from simp_vmgen.state import RunState


def test_port_allocation() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        busy = sock.getsockname()[1]
        with pytest.raises(ProjectError, match="in use"):
            resolve_port("127.0.0.1", busy)
        chosen = resolve_port("127.0.0.1", "auto", start=busy + 1)
        assert chosen != busy


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = RunState(ssh_port=2222, gdb_port=1234, pid=42, started_at="now")
    state.save(path)
    assert RunState.load(path) == state
    assert RunState.load(tmp_path / "missing.json") is None


def _project(tmp_path: Path) -> Path:
    target = tmp_path / "lab"
    assert main(["new", str(target)]) == 0
    return target


@pytest.mark.skipif(
    shutil.which("qemu-img") is None
    or not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="qemu-img or seed tool missing",
)
def test_build_and_run_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    base = tmp_path / "base.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(base), "8M"],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setattr(
        runtime, "download", lambda *args, **kwargs: (base, f"sha256:{'a' * 64}")
    )

    assert runtime.build(SimpleNamespace(directory=str(root), force=True, dry_run=False)) == 0
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    assert layout.overlay.is_file()
    assert layout.seed_iso.is_file()

    assert (
        runtime.run(
            SimpleNamespace(
                directory=str(root), dry_run=True, foreground=False, snapshot=False
            )
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "qemu-system-x86_64" in out
    assert "-drive" in out


def test_status_and_stop(tmp_path: Path) -> None:
    root = _project(tmp_path)
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen(["sleep", "30"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        RunState(
            ssh_port=2222,
            gdb_port=None,
            pid=process.pid,
            started_at="now",
        ).save(layout.state_file)

        assert runtime.status(SimpleNamespace(directory=str(root))) == 0
        assert runtime.stop(SimpleNamespace(directory=str(root))) == 0
        process.wait(timeout=5)
        assert process.poll() is not None
        assert not layout.pid_file.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
