from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import diagnose as diag
from simp_vmgen.config import Config


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


def test_build_report_flags_unknown_distro(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    config = Config.default("lab")
    config.distro.name = "debian"
    config.save(root / "vmgen.toml")
    report = diag.build_report(directory=str(root))
    assert not report.ok
    assert any(check.name == "distro" and check.status == "missing" for check in report.checks)


def test_build_report_flags_missing_mount_source(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    Config.default("lab").save(root / "vmgen.toml")
    report = diag.build_report(directory=str(root))
    assert not report.ok
    assert any(
        check.name == "mount sources" and check.status == "missing" for check in report.checks
    )


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


def test_kernel_artifact_profile_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from simp_vmgen.config import Config, KernelConfig

    artifact = tmp_path / "bzImage"
    artifact.write_bytes(b"kernel")
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, mode="artifact", artifact=str(artifact))
    config.validate()
    monkeypatch.setattr(diag, "_load_optional_project", lambda directory: (tmp_path, config))
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    report = diag.build_report(directory=str(tmp_path))
    names = [check.name for check in report.checks]
    assert report.profile == "kernel"
    assert "kernel artifact" in names
    assert "flex" not in names
    assert "bc" not in names


def test_kernel_alpine_rootfs_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from simp_vmgen.config import Config, KernelConfig

    artifact = tmp_path / "bzImage"
    artifact.write_bytes(b"kernel")
    config = Config.default("lab")
    config.distro.name = "alpine"
    config.distro.release = "3.22"
    config.kernel = KernelConfig(
        enabled=True,
        mode="artifact",
        artifact=str(artifact),
        rootfs="alpine",
    )
    config.validate()
    monkeypatch.setattr(diag, "_load_optional_project", lambda directory: (tmp_path, config))
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    report = diag.build_report(directory=str(tmp_path))
    names = [check.name for check in report.checks]
    assert "kernel rootfs" in names
    assert "openssl" in names


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
