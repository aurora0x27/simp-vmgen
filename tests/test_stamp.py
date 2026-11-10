from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import runtime, stamp
from simp_vmgen.cli import main
from simp_vmgen.config import Config, KernelConfig, NetworkConfig
from simp_vmgen.errors import ProjectError


def _args(root: Path, **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(directory=str(root), **kwargs)


def _base_image(tmp_path: Path) -> Path:
    base = tmp_path / "base.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(base), "8M"],
        check=True,
        capture_output=True,
        text=True,
    )
    return base


def _cloud_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    base = _base_image(tmp_path)
    monkeypatch.setattr(
        runtime,
        "download",
        lambda *args, **kwargs: (base, f"sha256:{'a' * 64}"),
    )
    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    return root, base


def _qemu_virtual_size(path: Path) -> int:
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(json.loads(result.stdout)["virtual-size"])


@pytest.mark.skipif(
    shutil.which("qemu-img") is None
    or not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="qemu-img or seed tool missing",
)
def test_run_rejects_config_changes_since_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _base = _cloud_project(tmp_path, monkeypatch)
    config = Config.load(root / "vmgen.toml")
    config.users[0].password = "changed"
    config.save(root / "vmgen.toml")

    layout = runtime.ProjectLayout(root, config)
    with pytest.raises(ProjectError, match="configuration changed"):
        stamp.require_cloud_artifacts(config, layout)

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    stamp.require_cloud_artifacts(config, layout)
    assert runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False)) == 0


@pytest.mark.skipif(
    shutil.which("qemu-img") is None
    or not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="qemu-img or seed tool missing",
)
def test_run_rejects_network_changes_since_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _base = _cloud_project(tmp_path, monkeypatch)
    config = Config.load(root / "vmgen.toml")
    config.networks = [NetworkConfig("lan", address="192.168.77.10/24", mcast="239.192.77.1:4789")]
    config.validate()
    config.save(root / "vmgen.toml")

    layout = runtime.ProjectLayout(root, config)
    with pytest.raises(ProjectError, match="configuration changed"):
        stamp.require_cloud_artifacts(config, layout)

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    stamp.require_cloud_artifacts(config, layout)


@pytest.mark.skipif(
    shutil.which("qemu-img") is None
    or not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="qemu-img or seed tool missing",
)
def test_build_requires_force_for_disk_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _base = _cloud_project(tmp_path, monkeypatch)
    config = Config.load(root / "vmgen.toml")
    config.vm.disk = "40G"
    config.save(root / "vmgen.toml")

    layout = runtime.ProjectLayout(root, config)
    assert _qemu_virtual_size(layout.overlay) == 30 * 1024**3
    with pytest.raises(ProjectError, match="does not match"):
        runtime.build(_args(root, dry_run=False, force=False))

    assert runtime.build(_args(root, dry_run=False, force=True)) == 0
    assert _qemu_virtual_size(layout.overlay) == 40 * 1024**3


@pytest.mark.skipif(
    shutil.which("qemu-img") is None
    or not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="qemu-img or seed tool missing",
)
def test_build_recovers_when_stamp_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _base = _cloud_project(tmp_path, monkeypatch)
    (root / ".simp-vmgen" / stamp.STAMP_FILENAME).unlink()
    config = Config.load(root / "vmgen.toml")
    layout = runtime.ProjectLayout(root, config)

    with pytest.raises(ProjectError, match="no build stamp"):
        stamp.require_cloud_artifacts(config, layout)

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    stamp.require_cloud_artifacts(config, layout)
    assert runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False)) == 0


def test_kernel_artifact_run_rejects_config_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "lab"
    root.mkdir()
    (root / "work").mkdir()
    artifact = tmp_path / "bzImage"
    initrd = tmp_path / "initrd"
    artifact.write_bytes(b"kernel")
    initrd.write_bytes(b"initrd")
    config = Config.default("lab")
    config.kernel = KernelConfig(
        enabled=True,
        mode="artifact",
        artifact=str(artifact),
        initrd=str(initrd),
    )
    config.validate()
    config.save(root / "vmgen.toml")

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    assert runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False)) == 0

    config.users[0].password = "changed"
    config.save(root / "vmgen.toml")
    layout = runtime.ProjectLayout(root, config)
    with pytest.raises(ProjectError, match="stale"):
        stamp.require_kernel_artifacts(config, layout, artifact, initrd)

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    stamp.require_kernel_artifacts(config, layout, artifact, initrd)
    assert runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False)) == 0


@pytest.mark.skipif(
    shutil.which("qemu-system-x86_64") is None
    or not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="qemu or seed tool missing",
)
def test_run_json_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _base = _cloud_project(tmp_path, monkeypatch)
    capsys.readouterr()
    assert (
        runtime.run(
            _args(
                root,
                dry_run=True,
                foreground=False,
                snapshot=False,
                json=True,
            )
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "dry-run"
    assert data["qemu_argv"][0] == "qemu-system-x86_64"
    assert data["foreground"] is False
    assert data["snapshot"] is False


@pytest.mark.skipif(
    shutil.which("qemu-system-x86_64") is None
    or not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="qemu or seed tool missing",
)
def test_run_wait_json_combines_summary_and_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _base = _cloud_project(tmp_path, monkeypatch)
    layout = runtime.ProjectLayout(root, Config.load(root / "vmgen.toml"))
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(runtime, "_wait_for_pid", lambda *args, **kwargs: process.pid)
        monkeypatch.setattr(
            runtime,
            "_wait_for_ready",
            lambda *args, **kwargs: {
                "ready": True,
                "elapsed_seconds": 1.0,
                "timeout": 5,
                "serial_log": str(layout.serial_log),
            },
        )
        monkeypatch.setattr(
            runtime.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        capsys.readouterr()
        assert (
            runtime.run(
                _args(
                    root,
                    dry_run=False,
                    foreground=False,
                    snapshot=False,
                    json=True,
                    wait=True,
                    wait_timeout=5,
                )
            )
            == 0
        )
        data = json.loads(capsys.readouterr().out)
        assert data["state"] == "running"
        assert data["pid"] == process.pid
        assert data["ready"] is True
        assert data["timeout"] == 5
        assert isinstance(data["ssh"]["port"], int)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
