from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import runtime
from simp_vmgen.cli import main
from simp_vmgen.config import Config, UserConfig
from simp_vmgen.errors import ProjectError
from simp_vmgen.layout import ProjectLayout
from simp_vmgen.sshkeys import askpass_script, ensure_ssh_key
from simp_vmgen.state import RunState


@pytest.fixture(autouse=True)
def _treat_test_process_as_qemu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_pid_matches_qemu", lambda pid: True)


def _layout(tmp_path: Path) -> ProjectLayout:
    config = Config.default("lab")
    return ProjectLayout(tmp_path, config)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen missing")
def test_ensure_ssh_key(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    key = ensure_ssh_key(layout.config, layout.ssh_key)
    assert key is not None
    assert key.private_key == layout.ssh_key
    assert layout.ssh_key.is_file()
    assert layout.ssh_key.with_name("id_ed25519.pub").is_file()
    assert key.public_key.startswith("ssh-ed25519 ")
    assert (layout.ssh_key.stat().st_mode & 0o777) == 0o600


def test_askpass_script(tmp_path: Path) -> None:
    config = Config.default("lab")
    config.login_user.password = "p'a ss"
    path = askpass_script(config, tmp_path / "askpass.sh")
    assert path is not None
    assert path.read_text() == "#!/bin/sh\nprintf '%s\\n' 'p'\"'\"'a ss'\n"
    assert (path.stat().st_mode & 0o777) == 0o700


def test_ssh_argv(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.ssh_key.parent.mkdir(parents=True)
    layout.ssh_key.write_text("private", encoding="utf-8")
    state = RunState(ssh_port=2222, gdb_port=None, pid=1, started_at="now")
    argv = runtime._ssh_argv(
        layout.config,
        layout,
        state,
        ["echo", "hi"],
        tty=True,
        batch=True,
    )
    assert argv[0] == "ssh"
    assert "-p" in argv and "2222" in argv
    assert "-i" in argv and str(layout.ssh_key) in argv
    assert "-t" in argv
    assert "BatchMode=yes" in argv
    assert "root@127.0.0.1" in argv
    assert argv[-1] == "echo hi"


def test_remote_shell_command_quotes_argv() -> None:
    assert runtime._remote_shell_command(["echo", "hello world"]) == "echo 'hello world'"
    assert runtime._remote_shell_command(["sh", "-c", "exit 7"]) == "sh -c 'exit 7'"
    assert runtime._remote_shell_command(['sh -c "exit 7"']) == 'sh -c "exit 7"'
    assert runtime._remote_shell_command([]) == ""


def test_ssh_argv_workdir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    state = RunState(ssh_port=2222, gdb_port=None, pid=1, started_at="now")
    argv = runtime._ssh_argv(
        layout.config,
        layout,
        state,
        ["echo", "hi"],
        tty=False,
        batch=False,
        use_key=False,
        workdir="/work dir",
    )
    assert argv[-1] == "cd '/work dir' && echo hi"


def test_ssh_user_override_and_askpass(tmp_path: Path) -> None:
    config = Config.default("lab")
    config.users.append(UserConfig("alice", password="alice-secret"))
    config.validate()
    layout = ProjectLayout(tmp_path, config)
    state = RunState(ssh_port=2222, gdb_port=None, pid=1, started_at="now")

    argv = runtime._ssh_argv(
        config,
        layout,
        state,
        [],
        tty=False,
        batch=False,
        user="alice",
        use_key=False,
    )
    assert "alice@127.0.0.1" in argv
    assert "-i" not in argv

    env = runtime._askpass_env(config, layout, user_name="alice")
    assert env is not None
    assert "alice-secret" in Path(env["SSH_ASKPASS"]).read_text()


def test_remote_command_strips_separator() -> None:
    assert runtime._remote_command(["--", "uname", "-a"]) == ["uname", "-a"]
    assert runtime._remote_command(None) == []


def test_wait_uses_serial_marker(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    layout = ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.ensure()
    process = subprocess.Popen(["sleep", "30"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        RunState(ssh_port=2222, gdb_port=None, pid=process.pid, started_at="now").save(
            layout.state_file
        )
        layout.serial_log.write_text("booting\nSIMP_VMGEN_READY name=lab\n")
        assert runtime.wait(SimpleNamespace(directory=str(root), timeout=5)) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_ready_marker_seen(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.run_dir.mkdir(parents=True)
    layout.serial_log.write_text("x\nSIMP_VMGEN_READY name=lab\n")
    assert runtime._ready_marker_seen(layout)


def test_run_ssh_requires_running_vm(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    with pytest.raises(ProjectError, match="not running"):
        runtime.guest(SimpleNamespace(directory=str(root), guest_command=["true"]))


def test_guest_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    layout = ProjectLayout(root, Config.load(root / "vmgen.toml"))
    layout.ensure()
    process = subprocess.Popen(["sleep", "30"])
    try:
        layout.pid_file.write_text(str(process.pid), encoding="utf-8")
        RunState(ssh_port=2222, gdb_port=None, pid=process.pid, started_at="now").save(
            layout.state_file
        )

        def fake_run(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired("ssh", 1)

        monkeypatch.setattr(runtime.subprocess, "run", fake_run)
        with pytest.raises(ProjectError, match="timed out"):
            runtime.guest(
                SimpleNamespace(
                    directory=str(root),
                    guest_command=["true"],
                    timeout=1,
                )
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
