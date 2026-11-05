from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import runtime, stamp
from simp_vmgen.cli import main
from simp_vmgen.config import Config, KernelConfig
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

    with pytest.raises(ProjectError, match="configuration changed"):
        runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False))

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    assert runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False)) == 0


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

    with pytest.raises(ProjectError, match="no build stamp"):
        runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False))

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
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
    with pytest.raises(ProjectError, match="stale"):
        runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False))

    assert runtime.build(_args(root, dry_run=False, force=False)) == 0
    assert runtime.run(_args(root, dry_run=True, foreground=False, snapshot=False)) == 0
