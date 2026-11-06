from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from simp_vmgen import runtime
from simp_vmgen.config import Config, KernelConfig


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
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
    return root, artifact, initrd


def test_build_and_run_artifact(tmp_path: Path, capsys) -> None:
    root, artifact, initrd = _project(tmp_path)
    assert runtime.build(SimpleNamespace(directory=str(root), dry_run=False, force=False)) == 0
    out = capsys.readouterr().out
    assert str(artifact) in out
    assert str(initrd) in out
    assert not (root / ".simp-vmgen" / "run" / "overlay.qcow2").exists()

    assert (
        runtime.run(
            SimpleNamespace(directory=str(root), dry_run=True, foreground=False, snapshot=False)
        )
        == 0
    )
    command = capsys.readouterr().out
    assert "-kernel" in command
    assert str(artifact) in command
    assert "-initrd" in command


def test_run_dry_run_before_build(tmp_path: Path, capsys) -> None:
    root, artifact, initrd = _project(tmp_path)
    capsys.readouterr()
    assert (
        runtime.run(
            SimpleNamespace(directory=str(root), dry_run=True, foreground=False, snapshot=False)
        )
        == 0
    )
    command = capsys.readouterr().out
    assert "-kernel" in command
    assert str(artifact) in command
    assert str(initrd) in command
