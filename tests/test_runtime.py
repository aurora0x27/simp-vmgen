from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import runtime
from simp_vmgen.cli import main
from simp_vmgen.errors import ProjectError
from simp_vmgen.net import resolve_port
from simp_vmgen.state import RunState


@pytest.fixture(autouse=True)
def _treat_test_process_as_qemu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_pid_matches_qemu", lambda pid: True)


def test_port_allocation() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        busy = sock.getsockname()[1]
        with pytest.raises(ProjectError, match="in use"):
            resolve_port("127.0.0.1", busy)
        chosen = resolve_port("127.0.0.1", "auto", start=busy + 1)
        assert chosen != busy


def test_long_project_path_uses_short_socket_dir(tmp_path: Path) -> None:
    from simp_vmgen.config import Config

    root = tmp_path / ("long-project-" + "x" * 70)
    layout = runtime.ProjectLayout(root, Config.default("long"))
    assert len(str(layout.serial_sock)) < 108
    assert layout.socket_dir != layout.run_dir


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
    monkeypatch.setattr(runtime, "download", lambda *args, **kwargs: (base, f"sha256:{'a' * 64}"))

    assert runtime.build(SimpleNamespace(directory=str(root), force=True, dry_run=False)) == 0
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    assert layout.overlay.is_file()
    assert layout.seed_iso.is_file()

    assert (
        runtime.run(
            SimpleNamespace(directory=str(root), dry_run=True, foreground=False, snapshot=False)
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "qemu-system-x86_64" in out
    assert "-drive" in out


def test_run_dry_run_before_build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    capsys.readouterr()
    assert (
        runtime.run(
            SimpleNamespace(
                directory=str(root),
                dry_run=True,
                foreground=False,
                snapshot=False,
            )
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "qemu-system-x86_64" in out
    assert "-drive" in out


def test_gdb_reports_connection(tmp_path: Path, capsys) -> None:
    root = tmp_path / "gdb"
    assert main(["new", str(root), "--gdb"]) == 0
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        RunState(
            ssh_port=2222,
            gdb_port=1234,
            pid=process.pid,
            started_at="now",
        ).save(layout.state_file)
        assert runtime.gdb(SimpleNamespace(directory=str(root), connect=False)) == 0
        out = capsys.readouterr().out
        assert "127.0.0.1:1234" in out
        assert "target remote" in out
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_status_and_stop(tmp_path: Path) -> None:
    root = _project(tmp_path)
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
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


def test_status_json_and_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    capsys.readouterr()
    assert (
        runtime.status(SimpleNamespace(directory=str(root), json=True, check=True))
        == runtime.EXIT_NOT_RUNNING
    )
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "stopped"
    assert data["pid"] is None
    assert data["ready"] is False
    assert data["ssh"] is None
    assert data["qemu_log"].endswith("qemu.log")

    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        RunState(
            ssh_port=2222,
            gdb_port=1234,
            pid=process.pid,
            started_at="now",
        ).save(layout.state_file)
        layout.serial_log.write_text("SIMP_VMGEN_READY name=lab\n", encoding="utf-8")

        assert runtime.status(SimpleNamespace(directory=str(root), json=True, check=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["state"] == "running"
        assert data["pid"] == process.pid
        assert data["ssh"] == {"user": "root", "host": "127.0.0.1", "port": 2222}
        assert data["gdb"] == {"host": "127.0.0.1", "port": 1234}
        assert data["ready"] is True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_wait_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    capsys.readouterr()
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        RunState(
            ssh_port=2222,
            gdb_port=None,
            pid=process.pid,
            started_at="now",
        ).save(layout.state_file)
        layout.serial_log.write_text("booting\nSIMP_VMGEN_READY name=lab\n")
        assert runtime.wait(SimpleNamespace(directory=str(root), timeout=5, json=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ready"] is True
        assert data["timeout"] == 5
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_wait_detects_qemu_exit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen(["sh", "-c", "sleep 0.2"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        RunState(
            ssh_port=2222,
            gdb_port=None,
            pid=process.pid,
            started_at="now",
        ).save(layout.state_file)
        with pytest.raises(ProjectError, match="exited before"):
            runtime.wait(SimpleNamespace(directory=str(root), timeout=5, json=True))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_wait_reports_progress_and_serial_tail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    capsys.readouterr()
    layout = runtime.ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        layout.serial_log.write_text("kernel panic\n", encoding="utf-8")
        config = runtime.load_project(str(root))[1]
        with pytest.raises(ProjectError, match="timed out") as exc:
            runtime._wait_for_ready(
                layout,
                config,
                timeout=0.7,
                progress=True,
                report_interval=0.01,
            )
        assert "still waiting" in capsys.readouterr().err
        assert "kernel panic" in str(exc.value)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_up_builds_and_runs_with_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        runtime,
        "build",
        lambda args: calls.append("build") or 0,
    )
    monkeypatch.setattr(
        runtime,
        "run",
        lambda args: (
            calls.append(("run", getattr(args, "wait", False), getattr(args, "wait_timeout", None)))
            or 0
        ),
    )

    args = SimpleNamespace(directory=str(root), no_build=False, timeout=42)
    assert runtime.up(args) == 0
    assert calls == ["build", ("run", True, 42)]


def test_up_waits_when_already_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(runtime, "running_pid", lambda layout: 123)
    monkeypatch.setattr(runtime, "build", lambda args: calls.append("build") or 0)
    monkeypatch.setattr(runtime, "run", lambda args: calls.append("run") or 0)
    monkeypatch.setattr(runtime, "wait", lambda args: calls.append("wait") or 0)

    args = SimpleNamespace(directory=str(root), no_build=False, timeout=42)
    assert runtime.up(args) == 0
    assert calls == ["wait"]


def test_restart_stops_and_runs_with_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(runtime, "stop", lambda args: calls.append("stop") or 0)
    monkeypatch.setattr(
        runtime,
        "run",
        lambda args: (
            calls.append(("run", getattr(args, "wait", False), getattr(args, "wait_timeout", None)))
            or 0
        ),
    )

    args = SimpleNamespace(directory=str(root), timeout=7)
    assert runtime.restart(args) == 0
    assert calls == ["stop", ("run", True, 7)]
