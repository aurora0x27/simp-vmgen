"""Command-line entry point for simp-vmgen."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import shtab

from . import __version__
from .diagnose import diagnose
from .errors import SimpVmgenError
from .kernel import kernel_dispatch
from .project import new_project, regen_project, show_project
from .runtime import build as runtime_build
from .runtime import clean as runtime_clean
from .runtime import console as runtime_console
from .runtime import gdb as runtime_gdb
from .runtime import guest as runtime_guest
from .runtime import log as runtime_log
from .runtime import run as runtime_run
from .runtime import serial as runtime_serial
from .runtime import ssh as runtime_ssh
from .runtime import status as runtime_status
from .runtime import stop as runtime_stop
from .runtime import tty as runtime_tty
from .runtime import wait as runtime_wait

PROG = "simp-vmgen"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Generate reproducible QEMU lab environments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  simp-vmgen new lab-ebpf --distro ubuntu --release 24.04 \\\n"
            "      --mem 4G --cpus 4 -v work:/work\n"
            "  simp-vmgen new lab-kernel -i\n"
            "  simp-vmgen -C lab-ebpf build\n"
            "  simp-vmgen diagnose\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-C",
        "--directory",
        metavar="DIR",
        default=None,
        help="project directory (default: search upward for vmgen.toml)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show commands and diagnostic detail",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print what would happen without changing anything",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    _add_new(subparsers)
    _add_regen(subparsers)
    _add_diagnose(subparsers)
    _add_show(subparsers)
    _add_lifecycle(subparsers)
    _add_kernel(subparsers)
    _add_gdb(subparsers)
    _add_completion(subparsers)
    return parser


def _add_new(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "new",
        help="create a new lab project",
        description="Create a new lab project from the selected distro template.",
    )
    p.add_argument("directory", nargs="?", default=".", metavar="DIR")
    p.add_argument("--name", dest="project_name", metavar="NAME", help="project/hostname name")
    p.add_argument("-i", "--interactive", action="store_true", help="interactive wizard")
    p.add_argument("--distro", metavar="NAME", help="distro name (ubuntu, alpine)")
    p.add_argument("--release", metavar="VERSION", help="distro release")
    p.add_argument("--arch", default="x86_64", metavar="ARCH")
    p.add_argument("--mem", default=None, metavar="SIZE")
    p.add_argument("--cpus", default=None, type=int, metavar="N")
    p.add_argument("--disk", default=None, metavar="SIZE")
    p.add_argument(
        "--accel",
        choices=("auto", "kvm", "tcg"),
        default="auto",
        help="QEMU accelerator (default: auto)",
    )
    p.add_argument("--ssh-port", default="auto", metavar="PORT|auto")
    p.add_argument("--gdb", action="store_true", help="enable the QEMU gdb stub")
    p.add_argument("--gdb-port", default="auto", metavar="PORT|auto")
    p.add_argument(
        "-v",
        "--volume",
        action="append",
        default=[],
        metavar="SRC:DST[:ro]",
        help="bind mount (repeatable, Docker-style)",
    )
    p.add_argument(
        "--package-set",
        action="append",
        default=[],
        metavar="NAME",
        help="distro package set (repeatable)",
    )
    p.add_argument(
        "--package",
        action="append",
        default=[],
        metavar="PKG",
        help="extra guest package (repeatable)",
    )
    p.add_argument(
        "--ssh-key",
        metavar="PATH",
        help="existing private key to use (its .pub file is installed in the guest)",
    )
    p.add_argument(
        "--login-user",
        default="root",
        metavar="NAME",
        help="default SSH login user (default: root)",
    )
    p.add_argument(
        "--user",
        action="append",
        default=[],
        metavar="NAME[:PASSWORD]",
        help="add a guest user (repeatable)",
    )
    p.add_argument(
        "--user-shell",
        action="append",
        default=[],
        metavar="NAME=SHELL",
        help="set a user's login shell (repeatable)",
    )
    p.add_argument(
        "--user-sudo",
        action="append",
        default=[],
        metavar="NAME",
        help="grant passwordless sudo to a user (repeatable)",
    )
    p.add_argument(
        "--user-group",
        action="append",
        default=[],
        metavar="NAME=GROUP",
        help="add a user to a group (repeatable)",
    )
    p.add_argument(
        "--user-key",
        action="append",
        default=[],
        metavar="NAME=PATH_OR_KEY",
        help="add an SSH public key to a user (repeatable)",
    )
    p.add_argument(
        "--no-root-user",
        action="store_true",
        help="do not create the default root account",
    )
    p.add_argument("--root-password", metavar="PASSWORD")
    p.add_argument("--kernel", action="store_true", help="enable a custom kernel")
    p.add_argument("--kernel-version", metavar="VERSION")
    p.add_argument("--kernel-commit", metavar="SHA")
    p.add_argument("--kernel-repo", metavar="URL")
    p.add_argument("--kernel-config", metavar="TARGET_OR_FILE")
    p.add_argument(
        "--kernel-fragment",
        action="append",
        default=[],
        metavar="FILE",
        help="Kconfig fragment (repeatable)",
    )
    p.add_argument(
        "--kernel-option",
        action="append",
        default=[],
        metavar="CONFIG_X=Y",
        help="Kconfig option override (repeatable)",
    )
    p.add_argument(
        "--kernel-target",
        action="append",
        default=[],
        metavar="TARGET",
        help="make target such as bzImage or modules (repeatable)",
    )
    p.add_argument(
        "--kernel-make-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra make argument such as LLVM=1 (repeatable)",
    )
    p.add_argument("--kernel-jobs", type=int, default=0, metavar="N")
    p.add_argument("--kernel-artifact", metavar="PATH")
    p.add_argument(
        "--kernel-artifact-type",
        choices=("bzImage", "vmlinuz", "vmlinux", "Image"),
        default="bzImage",
    )
    p.add_argument("--kernel-initrd", metavar="PATH")
    p.add_argument("--kernel-append", metavar="ARGS")
    p.add_argument("--rootfs", default="busybox", metavar="NAME")
    p.add_argument("--force", action="store_true", help="overwrite a non-empty directory")
    _add_local_dry_run(p)


def _add_local_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print what would happen without changing anything",
    )


def _add_regen(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "regen",
        help="re-render generated project files",
        description="Re-render generated files from vmgen.toml.",
    )
    p.add_argument("--check", action="store_true", help="fail if files would change")
    p.add_argument("--force", action="store_true", help="overwrite modified files")


def _add_diagnose(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "diagnose",
        help="check host dependencies",
        description="Check the host tools needed by the configured profile.",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--profile",
        choices=("base", "cloudimg", "kernel"),
        default=None,
        help="limit checks to a profile",
    )


def _add_show(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "show",
        help="show the resolved project configuration",
        description="Show the resolved configuration and project paths.",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _add_lifecycle(subparsers: argparse._SubParsersAction) -> None:
    for name, help_text in (
        ("build", "prepare images, seed and other build artifacts"),
        ("run", "start the VM"),
        ("stop", "stop the VM"),
        ("status", "show VM status"),
        ("wait", "wait until the guest is ready"),
        ("ssh", "open an SSH session in the guest"),
        ("tty", "open an interactive SSH session in the guest"),
        ("serial", "attach to the guest serial console"),
        ("console", "attach to the QEMU monitor"),
        ("log", "show or follow the serial log"),
        ("guest", "run one command in the guest"),
        ("clean", "stop the VM and remove runtime artifacts"),
    ):
        p = subparsers.add_parser(name, help=help_text)

        if name == "build":
            p.add_argument("--force", action="store_true", help="rebuild artifacts")
            _add_local_dry_run(p)
        elif name == "run":
            p.add_argument("--foreground", action="store_true", help="do not daemonize")
            p.add_argument("--snapshot", action="store_true", help="discard writes on exit")
            _add_local_dry_run(p)
        elif name == "wait":
            p.add_argument("--timeout", type=int, default=None, metavar="SECONDS")
        elif name in ("ssh", "guest"):
            p.add_argument(
                "guest_command",
                nargs=argparse.REMAINDER,
                metavar="CMD",
                help="command to run (use -- before CMD)",
            )
        elif name == "log":
            p.add_argument("-f", "--follow", action="store_true", help="follow the log")
        elif name == "clean":
            p.add_argument("--all", action="store_true", help="remove .simp-vmgen too")


def _add_kernel(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "kernel",
        help="manage the custom kernel build",
        description="Fetch, configure and build a custom Linux kernel.",
    )
    kernel_sub = p.add_subparsers(dest="kernel_command", metavar="COMMAND")
    for name, help_text in (
        ("fetch", "download and verify the kernel source"),
        ("config", "generate the kernel configuration"),
        ("build", "build bzImage, vmlinux and modules"),
        ("initramfs", "build the busybox initramfs"),
        ("status", "show kernel source and artifact status"),
        ("clean", "remove kernel build artifacts"),
    ):
        kernel_parser = kernel_sub.add_parser(name, help=help_text)
        if name in ("fetch", "config", "build", "initramfs"):
            _add_local_dry_run(kernel_parser)


def _add_gdb(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "gdb",
        help="connect GDB to the running VM",
        description="Connect GDB to the QEMU gdb stub.",
    )
    p.add_argument("--connect", action="store_true", help="start gdb automatically")


def _add_completion(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "completion",
        help="print a shell completion script",
        description="Print completion code for bash, zsh or fish.",
    )
    p.add_argument("shell", choices=("bash", "zsh", "fish"))


def print_completion(args: argparse.Namespace) -> int:
    print(shtab.complete(build_parser(), args.shell), end="")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    handlers = {
        "new": new_project,
        "regen": regen_project,
        "diagnose": diagnose,
        "show": show_project,
        "build": runtime_build,
        "run": runtime_run,
        "stop": runtime_stop,
        "status": runtime_status,
        "wait": runtime_wait,
        "ssh": runtime_ssh,
        "tty": runtime_tty,
        "serial": runtime_serial,
        "console": runtime_console,
        "log": runtime_log,
        "guest": runtime_guest,
        "clean": runtime_clean,
        "kernel": kernel_dispatch,
        "gdb": runtime_gdb,
        "completion": print_completion,
    }

    try:
        return handlers[args.command](args)
    except SimpVmgenError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
