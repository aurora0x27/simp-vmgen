from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from simp_vmgen import __version__
from simp_vmgen.cli import EXIT_OK, EXIT_USAGE, build_parser, main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Generate reproducible QEMU lab environments." in out
    assert "new" in out


def test_subcommands_are_registered() -> None:
    parser = build_parser()
    for name in (
        "new",
        "regen",
        "diagnose",
        "show",
        "build",
        "run",
        "stop",
        "status",
        "wait",
        "ssh",
        "tty",
        "serial",
        "console",
        "log",
        "guest",
        "clean",
        "kernel",
        "gdb",
    ):
        assert parser.parse_args([name]).command == name
    assert parser.parse_args(["compose", "validate"]).command == "compose"


def test_kernel_subcommands_are_registered() -> None:
    parser = build_parser()
    for name in ("fetch", "config", "build", "initramfs", "clean"):
        args = parser.parse_args(["kernel", name])
        assert args.command == "kernel"
        assert args.kernel_command == name


def test_ssh_user_option() -> None:
    parser = build_parser()
    assert parser.parse_args(["ssh", "--user", "alice"]).user == "alice"
    assert parser.parse_args(["tty", "-u", "alice"]).user == "alice"
    assert parser.parse_args(["guest", "--user", "alice", "--", "true"]).user == "alice"
    assert parser.parse_args(["guest", "--workdir", "/work", "--", "true"]).workdir == "/work"
    assert parser.parse_args(["guest", "--timeout", "5", "--", "true"]).timeout == 5.0


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        ("bash", "complete"),
        ("zsh", "#compdef simp-vmgen"),
        ("fish", "complete -c simp-vmgen"),
    ],
)
def test_completion_scripts(shell: str, marker: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["completion", shell]) == EXIT_OK
    output = capsys.readouterr().out
    assert marker in output
    assert "simp-vmgen" in output


def test_new_volume_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["new", "lab", "-v", "work:/work", "-v", "/src:/src:ro"])
    assert args.directory == "lab"
    assert args.volume == ["work:/work", "/src:/src:ro"]


@pytest.mark.parametrize(
    "argv",
    [
        ["-n", "stop"],
        ["-n", "clean"],
        ["-n", "status"],
        ["-n", "ssh"],
        ["-n", "regen"],
        ["-n", "kernel", "status"],
        ["-n", "kernel", "clean"],
    ],
)
def test_dry_run_rejects_unsupported_commands(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == EXIT_USAGE
    assert "--dry-run is not supported" in capsys.readouterr().err


def test_dry_run_supported_build(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    assert main(["-C", str(root), "-n", "build"]) == 0


def test_directory_option_after_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "lab"
    assert main(["new", str(root)]) == 0
    capsys.readouterr()
    assert main(["show", "-C", str(root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["root"] == str(root)
    assert main(["kernel", "status", "-C", str(root)]) == 0


def test_log_options() -> None:
    args = build_parser().parse_args(["log", "-f", "--lines", "10", "--no-color"])
    assert args.follow is True
    assert args.lines == 10
    assert args.no_color is True
