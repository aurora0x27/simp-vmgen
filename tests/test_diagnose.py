from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import diagnose as diag


def test_check_tool_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    result = diag._check_tool("make")
    assert result.status == "ok"
    assert result.detail == "/usr/bin/make"


def test_check_tool_missing_and_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    required = diag._check_tool("make")
    optional = diag._check_tool("busybox", required=False)
    assert required.status == "missing"
    assert required.is_failure
    assert required.hint
    assert optional.status == "optional"
    assert not optional.is_failure


def test_seed_tool_alternatives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/xorriso" if name == "xorriso" else None,
    )
    assert diag._check_seed_tool().status == "ok"

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert diag._check_seed_tool().status == "missing"


def test_port_check_detects_busy_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        result = diag._check_port("ssh port", "127.0.0.1", port)
        assert result is not None
        assert result.status == "warn"
        assert not result.is_failure


def test_port_check_skips_auto() -> None:
    assert diag._check_port("ssh port", "127.0.0.1", "auto") is None


def test_cache_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag, "_cache_dir", lambda: tmp_path / "cache")
    cache, disk = diag._check_cache()
    assert cache.status == "ok"
    assert (tmp_path / "cache").is_dir()
    assert disk.status in ("ok", "warn")


def test_render_text_and_json_report() -> None:
    report = diag.DiagnosticReport(
        platform="linux x86_64",
        profile="base",
        checks=[
            diag.CheckResult("python", "ok", detail="3.11.0"),
            diag.CheckResult("qemu", "missing", hint="install qemu"),
            diag.CheckResult("kvm", "warn", detail="tc tcg", required=False),
        ],
    )
    text = diag.render_text(report)
    assert "[ok] python" in text
    assert "[!!] qemu" in text
    assert "hint: install qemu" in text
    assert "1 missing" in text

    data = report.as_dict()
    assert data["ok"] is False
    assert data["checks"][1]["status"] == "missing"


def test_diagnose_json_and_exit_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = diag.DiagnosticReport(
        platform="linux x86_64",
        profile="base",
        checks=[diag.CheckResult("python", "ok")],
    )
    monkeypatch.setattr(diag, "build_report", lambda **kwargs: report)

    code = diag.diagnose(SimpleNamespace(profile="base", directory=None, json=True))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["profile"] == "base"
    assert data["ok"] is True
