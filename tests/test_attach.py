from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import runtime
from simp_vmgen.attach import attach_socket, show_log
from simp_vmgen.cli import main
from simp_vmgen.errors import ProjectError
from simp_vmgen.layout import ProjectLayout


def test_show_log_tail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "serial.log"
    path.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n")
    assert show_log(path, follow=False, lines=5) == 0
    out = capsys.readouterr().out
    assert "line-99" in out
    assert "line-94" not in out


def test_show_log_missing(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="log not found"):
        show_log(tmp_path / "missing.log", follow=False)


def test_show_log_strips_ansi_outside_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "serial.log"
    path.write_text("\x1b[32mOK\x1b[0m done\n", encoding="utf-8")
    assert show_log(path, follow=False) == 0
    assert capsys.readouterr().out == "OK done\n"


def test_attach_missing_socket(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="socket not found"):
        attach_socket(tmp_path / "missing.sock")


def test_runtime_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    layout = ProjectLayout(root, runtime.load_project(str(root))[1])
    layout.run_dir.mkdir(parents=True, exist_ok=True)
    layout.serial_log.write_text("hello guest\n")
    assert runtime.log(SimpleNamespace(directory=str(root), follow=False)) == 0
    assert "hello guest" in capsys.readouterr().out


def test_serial_requires_running_vm(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    with pytest.raises(ProjectError, match="not running"):
        runtime.serial(SimpleNamespace(directory=str(root)))


def test_clean_removes_run_and_all(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    layout = ProjectLayout(root, runtime.load_project(str(root))[1])
    (layout.run_dir / "cloud-init").mkdir(parents=True)
    (layout.run_dir / "cloud-init" / "user-data").write_text("x")
    layout.ssh_dir.mkdir(parents=True, exist_ok=True)
    (layout.ssh_dir / "id_ed25519").write_text("key")

    assert runtime.clean(SimpleNamespace(directory=str(root), all=False)) == 0
    assert not layout.run_dir.exists()
    assert layout.ssh_dir.exists()

    (layout.run_dir / "cloud-init").mkdir(parents=True)
    assert runtime.clean(SimpleNamespace(directory=str(root), all=True)) == 0
    assert not layout.runtime_dir.exists()


def test_clean_all_warns_about_destructive_removal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    capsys.readouterr()
    assert runtime.clean(SimpleNamespace(directory=str(root), all=True)) == 0
    assert "SSH keys" in capsys.readouterr().err
