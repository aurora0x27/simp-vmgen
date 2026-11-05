from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.errors import ProjectError
from simp_vmgen.runner import run_streaming, set_verbose


def test_run_streaming_echoes_and_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "build.log"
    result = run_streaming(
        ["sh", "-c", "echo hello; echo error >&2; exit 3"],
        log_path=log,
    )
    captured = capsys.readouterr()
    assert result.returncode == 3
    assert "hello" in captured.out
    assert "error" in captured.out
    assert "hello" in result.tail
    text = log.read_text(encoding="utf-8")
    assert "hello" in text
    assert "error" in text
    assert "[exit 3]" in text


def test_run_streaming_can_capture_without_echoing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_streaming(
        ["sh", "-c", "printf 'abc\\n'"],
        log_path=tmp_path / "capture.log",
        capture=True,
        echo=False,
    )
    assert result.returncode == 0
    assert result.stdout == "abc"
    assert capsys.readouterr().out == ""


def test_run_streaming_timeout(tmp_path: Path) -> None:
    log = tmp_path / "timeout.log"
    with pytest.raises(ProjectError, match="timed out"):
        run_streaming(["sh", "-c", "sleep 2"], log_path=log, timeout=0.1)
    assert log.is_file()
    assert "[timeout" in log.read_text(encoding="utf-8")


def test_verbose_prints_command(capsys: pytest.CaptureFixture[str]) -> None:
    set_verbose(True)
    try:
        run_streaming(["sh", "-c", "true"], echo=False)
    finally:
        set_verbose(False)
    assert "+ " in capsys.readouterr().out
