from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from simp_vmgen.cli import main


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    script = tmp_path / "fake-vmgen"
    args_file = tmp_path / "args.txt"
    script.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$ARGS_FILE"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return root, script, args_file


@pytest.mark.skipif(shutil.which("make") is None, reason="make missing")
def test_make_guest_preserves_quoted_command(tmp_path: Path) -> None:
    root, script, args_file = _project(tmp_path)
    result = subprocess.run(
        [
            "make",
            "-C",
            str(root),
            "guest",
            'CMD=sh -c "exit 7"',
            f"SIMP_VMGEN={script}",
        ],
        env={"ARGS_FILE": str(args_file), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == [
        "guest",
        "--",
        'sh -c "exit 7"',
    ]


@pytest.mark.skipif(shutil.which("make") is None, reason="make missing")
def test_make_lab_uses_workdir_and_preserves_command(tmp_path: Path) -> None:
    root, script, args_file = _project(tmp_path)
    result = subprocess.run(
        [
            "make",
            "-C",
            str(root),
            "lab",
            "CMD=pwd; ls",
            f"SIMP_VMGEN={script}",
        ],
        env={"ARGS_FILE": str(args_file), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == [
        "guest",
        "--workdir",
        "/work",
        "--",
        "pwd; ls",
    ]


@pytest.mark.skipif(shutil.which("make") is None, reason="make missing")
def test_make_guest_requires_command(tmp_path: Path) -> None:
    root, script, _args_file = _project(tmp_path)
    result = subprocess.run(
        ["make", "-C", str(root), "guest", f"SIMP_VMGEN={script}"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert 'usage: make guest CMD="..."' in result.stderr
