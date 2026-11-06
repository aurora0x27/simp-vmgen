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


@pytest.mark.skipif(shutil.which("make") is None, reason="make missing")
@pytest.mark.parametrize(
    ("target", "expected"),
    [("log", ["log"]), ("log-follow", ["log", "-f"])],
)
def test_make_log_targets(tmp_path: Path, target: str, expected: list[str]) -> None:
    root, script, args_file = _project(tmp_path)
    result = subprocess.run(
        ["make", "-C", str(root), target, f"SIMP_VMGEN={script}"],
        env={"ARGS_FILE": str(args_file), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == expected


@pytest.mark.skipif(shutil.which("make") is None, reason="make missing")
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("kernel-rootfs", ["kernel", "rootfs"]),
        ("kernel-clean", ["kernel", "clean"]),
    ],
)
def test_make_kernel_targets(tmp_path: Path, target: str, expected: list[str]) -> None:
    root, script, args_file = _project(tmp_path)
    result = subprocess.run(
        ["make", "-C", str(root), target, f"SIMP_VMGEN={script}"],
        env={"ARGS_FILE": str(args_file), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == expected


@pytest.mark.skipif(shutil.which("make") is None, reason="make missing")
def test_make_help_is_project_specific(tmp_path: Path) -> None:
    root, _script, _args_file = _project(tmp_path)
    result = subprocess.run(
        ["make", "-C", str(root), "help"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "lab targets:" in result.stdout
    assert "build/up/restart" in result.stdout
    assert "kernel-fetch" not in result.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="make missing")
def test_make_help_lists_kernel_targets(tmp_path: Path) -> None:
    root = tmp_path / "kernel-lab"
    assert main(["new", str(root), "--kernel-version", "6.12.5"]) == 0
    result = subprocess.run(
        ["make", "-C", str(root), "help"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "kernel-fetch" in result.stdout
    assert "kernel-clean" in result.stdout
