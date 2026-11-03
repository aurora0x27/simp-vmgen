from __future__ import annotations

import pytest

from simp_vmgen import __version__
from simp_vmgen.cli import EXIT_NOT_IMPLEMENTED, EXIT_OK, build_parser, main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


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


def test_kernel_subcommands_are_registered() -> None:
    parser = build_parser()
    for name in ("fetch", "config", "build", "initramfs", "clean"):
        args = parser.parse_args(["kernel", name])
        assert args.command == "kernel"
        assert args.kernel_command == name


def test_placeholder_handler(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gdb"]) == EXIT_NOT_IMPLEMENTED
    assert "not implemented yet" in capsys.readouterr().err


def test_new_volume_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["new", "lab", "-v", "work:/work", "-v", "/src:/src:ro"])
    assert args.directory == "lab"
    assert args.volume == ["work:/work", "/src:/src:ro"]
