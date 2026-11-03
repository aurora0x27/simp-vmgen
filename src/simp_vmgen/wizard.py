"""Interactive project wizard."""

from __future__ import annotations

import argparse
import copy
import re
from collections.abc import Callable
from pathlib import Path

from .config import DEFAULT_ROOT_PASSWORD, DEFAULT_SSH_KEY
from .distros import Distro, load_distro
from .errors import ProjectError

_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SIZE_RE = re.compile(r"^[1-9][0-9]*[KMGT]?$", re.IGNORECASE)

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


class WizardCancelled(ProjectError):
    """The user cancelled the interactive wizard."""


def _ask(
    prompt: str,
    default: str | None,
    input_fn: InputFn,
) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        answer = input_fn(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise WizardCancelled("cancelled") from exc
    if answer.lower() in ("q", "quit", "cancel"):
        raise WizardCancelled("cancelled")
    return answer if answer else (default or "")


def _ask_yes_no(prompt: str, default: bool, input_fn: InputFn, output_fn: OutputFn) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        answer = _ask(f"{prompt} [{default_text}]", "", input_fn).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        output_fn("please answer y or n")


def _ask_choice(
    prompt: str,
    choices: tuple[str, ...],
    default: str,
    input_fn: InputFn,
    output_fn: OutputFn,
) -> str:
    while True:
        answer = _ask(f"{prompt} ({'/'.join(choices)})", default, input_fn)
        if answer in choices:
            return answer
        output_fn(f"choose one of: {', '.join(choices)}")


def _ask_size(prompt: str, default: str, input_fn: InputFn, output_fn: OutputFn) -> str:
    while True:
        answer = _ask(prompt, default, input_fn).upper()
        if _SIZE_RE.match(answer):
            return answer
        output_fn("use a size such as 512M, 2G or 30G")


def _ask_int(prompt: str, default: int, input_fn: InputFn, output_fn: OutputFn) -> int:
    while True:
        answer = _ask(prompt, str(default), input_fn)
        if answer.isdigit() and int(answer) > 0:
            return int(answer)
        output_fn("enter a positive integer")


def _ask_port(prompt: str, default: str, input_fn: InputFn, output_fn: OutputFn) -> str:
    while True:
        answer = _ask(prompt, default, input_fn).lower()
        if answer == "auto":
            return "auto"
        if answer.isdigit() and 1 <= int(answer) <= 65535:
            return answer
        output_fn('enter a port between 1 and 65535, or "auto"')


def _ask_distro(
    args: argparse.Namespace, input_fn: InputFn, output_fn: OutputFn
) -> tuple[str, Distro]:
    while True:
        name = _ask_choice(
            "Distro", ("ubuntu", "alpine"), args.distro or "ubuntu", input_fn, output_fn
        )
        try:
            return name, load_distro(name)
        except ProjectError as exc:
            output_fn(str(exc))


def _default_project_name(target: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", target.name).strip("-._")
    if not name:
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path.cwd().name).strip("-._")
    return name or "lab"


def _ask_users(
    args: argparse.Namespace,
    distro: Distro,
    input_fn: InputFn,
    output_fn: OutputFn,
) -> list[str]:
    names: list[str] = []
    create_root = _ask_yes_no("Create the root account?", True, input_fn, output_fn)
    if create_root:
        args.no_root_user = False
        args.root_password = _ask("Root password", DEFAULT_ROOT_PASSWORD, input_fn)
        names.append("root")
    else:
        args.no_root_user = True
        args.root_password = None

    while True:
        name = _ask("Add user (blank to finish)", "", input_fn)
        if not name:
            break
        if _USER_RE.match(name) is None:
            output_fn("user names must be lowercase letters, digits, '_' or '-'")
            continue
        if name in names:
            output_fn(f"user {name!r} already exists")
            continue
        password = _ask(f"Password for {name}", "", input_fn)
        shell = _ask(f"Shell for {name}", distro.default_shell, input_fn)
        sudo = _ask_yes_no(f"Grant passwordless sudo to {name}?", False, input_fn, output_fn)
        groups = _ask(f"Groups for {name} (comma separated)", "", input_fn)
        keys = _ask(f"SSH public keys for {name} (comma separated)", "", input_fn)
        args.user.append(f"{name}:{password}" if password else name)
        args.user_shell.append(f"{name}={shell}")
        if sudo:
            args.user_sudo.append(name)
        for group in (item.strip() for item in groups.split(",")):
            if group:
                args.user_group.append(f"{name}={group}")
        for key in (item.strip() for item in keys.split(",")):
            if key:
                args.user_key.append(f"{name}={key}")
        names.append(name)

    if not names:
        output_fn("at least one user is required")
        return _ask_users(args, distro, input_fn, output_fn)
    return names


def _ask_mounts(args: argparse.Namespace, input_fn: InputFn, output_fn: OutputFn) -> None:
    answer = _ask("Volumes (SRC:DST[:ro], comma separated)", "work:/work", input_fn)
    args.volume = [item.strip() for item in answer.split(",") if item.strip()]


def _ask_packages(
    args: argparse.Namespace,
    distro: Distro,
    input_fn: InputFn,
    output_fn: OutputFn,
) -> None:
    available = ", ".join(sorted(distro.package_sets))
    while True:
        answer = _ask("Package sets (comma separated)", "base", input_fn)
        sets = [item.strip() for item in answer.split(",") if item.strip()]
        missing = [item for item in sets if item not in distro.package_sets]
        if not missing:
            args.package_set = sets or ["base"]
            break
        output_fn(f"unknown package set(s): {', '.join(missing)}; available: {available}")
    extra = _ask("Extra packages (comma separated)", "", input_fn)
    args.package = [item.strip() for item in extra.split(",") if item.strip()]


def _summary(args: argparse.Namespace, distro: Distro, names: list[str]) -> str:
    user_text = ", ".join(names)
    lines = [
        "",
        "Project summary",
        f"  name      : {args.project_name}",
        f"  distro    : {distro.display_name} {args.release} ({args.arch})",
        f"  resources : {args.mem} RAM, {args.cpus} vCPU, {args.disk} disk",
        f"  accel     : {args.accel}",
        f"  users     : {user_text} (login: {args.login_user})",
        f"  volumes   : {', '.join(args.volume) if args.volume else 'none'}",
        f"  packages  : {', '.join(args.package_set)}"
        + (f" + {', '.join(args.package)}" if args.package else ""),
        f"  ssh port  : {args.ssh_port}",
        f"  kernel    : {'yes (' + args.kernel_version + ')' if args.kernel else 'no'}",
        f"  gdb       : {'yes' if args.gdb else 'no'}",
    ]
    return "\n".join(lines)


def run_wizard(
    args: argparse.Namespace,
    target: Path,
    *,
    input_fn: InputFn | None = None,
    output_fn: OutputFn = print,
) -> argparse.Namespace:
    if input_fn is None:
        input_fn = input
    result = copy.copy(args)
    result.interactive = False
    output_fn("simp-vmgen interactive setup (type q to cancel)")
    output_fn("\n[project]")
    default_name = getattr(args, "project_name", None) or _default_project_name(target)
    result.project_name = _ask("Project name", default_name, input_fn)
    while _PROJECT_RE.match(result.project_name) is None:
        output_fn(
            "project names must start with a letter/digit and use letters, digits, '.', '_' or '-'"
        )
        result.project_name = _ask("Project name", None, input_fn)

    output_fn("\n[distro and resources]")
    distro_name, distro = _ask_distro(result, input_fn, output_fn)
    result.distro = distro_name
    while True:
        release = _ask("Release", distro.default_release, input_fn)
        try:
            distro.release(release)
        except ProjectError as exc:
            output_fn(str(exc))
            continue
        result.release = release
        break

    while True:
        arch = _ask("Architecture", args.arch, input_fn)
        if arch in distro.architectures:
            result.arch = arch
            break
        output_fn(f"supported architectures: {', '.join(distro.architectures)}")

    release_info = distro.release(result.release)
    result.mem = _ask_size("Memory", release_info.default_mem, input_fn, output_fn)
    result.cpus = _ask_int("vCPUs", release_info.default_cpus, input_fn, output_fn)
    result.disk = _ask_size("Disk size", release_info.default_disk, input_fn, output_fn)
    result.accel = _ask_choice(
        "Accelerator", ("auto", "kvm", "tcg"), args.accel, input_fn, output_fn
    )
    result.ssh_port = _ask_port("SSH port", args.ssh_port or "auto", input_fn, output_fn)
    result.ssh_key = _ask("SSH private key path", args.ssh_key or DEFAULT_SSH_KEY, input_fn)

    output_fn("\n[users and login]")
    result.user = []
    result.user_shell = []
    result.user_sudo = []
    result.user_group = []
    result.user_key = []
    names = _ask_users(result, distro, input_fn, output_fn)
    default_login = "root" if "root" in names else names[0]
    while True:
        login = _ask("Default SSH login user", default_login, input_fn)
        if login in names:
            result.login_user = login
            break
        output_fn(f"choose one of: {', '.join(names)}")

    output_fn("\n[volumes]")
    _ask_mounts(result, input_fn, output_fn)
    output_fn("\n[packages]")
    _ask_packages(result, distro, input_fn, output_fn)

    output_fn("\n[advanced]")
    result.kernel = _ask_yes_no("Enable a custom kernel?", False, input_fn, output_fn)
    if result.kernel:
        result.kernel_version = _ask("Kernel version", args.kernel_version or "6.12.5", input_fn)
        result.rootfs = _ask("Kernel rootfs", args.rootfs or "busybox", input_fn)
    else:
        result.kernel_version = args.kernel_version
        result.rootfs = args.rootfs

    result.gdb = _ask_yes_no("Enable the QEMU gdb stub?", False, input_fn, output_fn)
    result.gdb_port = _ask_port("GDB port", args.gdb_port or "auto", input_fn, output_fn)

    output_fn(_summary(result, distro, names))
    if not _ask_yes_no("Create this project?", True, input_fn, output_fn):
        raise WizardCancelled("cancelled")
    return result
