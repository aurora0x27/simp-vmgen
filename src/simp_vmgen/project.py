"""Implementation of project creation and regeneration commands."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .config import (
    CONFIG_FILENAME,
    DEFAULT_ROOT_PASSWORD,
    DEFAULT_SSH_KEY,
    LOCK_FILENAME,
    Config,
    DistroConfig,
    GdbConfig,
    KernelConfig,
    MountConfig,
    ProjectConfig,
    ProvisionConfig,
    SshConfig,
    UserConfig,
    VmConfig,
)
from .distros import load_distro
from .errors import ProjectError
from .lock import create_lock
from .manifest import GeneratedManifest
from .paths import load_project
from .render import render_project, write_rendered
from .wizard import run_wizard

DEFAULT_KERNEL_VERSION = "6.12.5"


def _project_name(target: Path) -> str:
    raw = target.name
    if raw in ("", ".", ".."):
        raw = Path.cwd().name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return name or "lab"


def _user_by_name(users: list[UserConfig], name: str, option: str) -> UserConfig:
    user = next((item for item in users if item.name == name), None)
    if user is None:
        raise ProjectError(f"{option}: user {name!r} is not defined")
    return user


def _parse_users(args: argparse.Namespace) -> list[UserConfig]:
    users: list[UserConfig] = []
    if not args.no_root_user:
        users.append(UserConfig("root", password=DEFAULT_ROOT_PASSWORD))

    for spec in args.user:
        name, separator, password = spec.partition(":")
        if not name:
            raise ProjectError(f"invalid --user {spec!r}: expected NAME[:PASSWORD]")
        existing = next((user for user in users if user.name == name), None)
        if existing is not None:
            if separator:
                existing.password = password
            continue
        users.append(UserConfig(name, password=password if separator else ""))

    if args.root_password is not None:
        root = next((user for user in users if user.name == "root"), None)
        if root is None:
            if args.no_root_user:
                raise ProjectError("--root-password cannot be used with --no-root-user")
            root = UserConfig("root")
            users.insert(0, root)
        root.password = args.root_password

    for spec in args.user_shell:
        name, separator, shell = spec.partition("=")
        if not separator or not name or not shell:
            raise ProjectError(f"invalid --user-shell {spec!r}: expected NAME=SHELL")
        _user_by_name(users, name, "--user-shell").shell = shell

    for name in args.user_sudo:
        _user_by_name(users, name, "--user-sudo").sudo = True

    for spec in args.user_group:
        name, separator, group = spec.partition("=")
        if not separator or not name or not group:
            raise ProjectError(f"invalid --user-group {spec!r}: expected NAME=GROUP")
        user = _user_by_name(users, name, "--user-group")
        if group not in user.groups:
            user.groups.append(group)

    for spec in args.user_key:
        name, separator, key = spec.partition("=")
        if not separator or not name or not key:
            raise ProjectError(f"invalid --user-key {spec!r}: expected NAME=KEY")
        user = _user_by_name(users, name, "--user-key")
        if key not in user.ssh_keys:
            user.ssh_keys.append(key)

    if not users:
        raise ProjectError("at least one user is required")
    return users


def _parse_mounts(args: argparse.Namespace) -> list[MountConfig]:
    mounts = [MountConfig("work", "/work")]
    for spec in args.volume:
        mount = MountConfig.parse(spec)
        existing = next((item for item in mounts if item.target == mount.target), None)
        if existing is not None:
            if existing.source == mount.source and existing.readonly == mount.readonly:
                continue
            raise ProjectError(f"volume {spec!r}: target {mount.target!r} is already mounted")
        mounts.append(mount)
    return mounts


def config_from_args(args: argparse.Namespace, target: Path) -> Config:
    distro = load_distro(args.distro or "ubuntu")
    if args.arch not in distro.architectures:
        raise ProjectError(
            f"distro {distro.name!r} does not support architecture {args.arch!r} "
            f"(supported: {', '.join(distro.architectures)})"
        )
    release = distro.release(args.release)

    kernel_version = args.kernel_version
    if args.kernel and not kernel_version:
        kernel_version = DEFAULT_KERNEL_VERSION

    project_name = getattr(args, "project_name", None) or _project_name(target)
    config = Config(
        project=ProjectConfig(project_name),
        distro=DistroConfig(
            name=distro.name,
            release=release.version,
            arch=args.arch,
            image=distro.image_url(release, args.arch),
            image_checksum=release.image_checksum,
        ),
        vm=VmConfig(
            mem=args.mem or release.default_mem,
            cpus=args.cpus or release.default_cpus,
            disk=args.disk or release.default_disk,
            accel=args.accel,
            machine=release.machine,
        ),
        ssh=SshConfig(
            port=args.ssh_port,
            user=args.login_user,
            key=args.ssh_key if args.ssh_key is not None else DEFAULT_SSH_KEY,
        ),
        gdb=GdbConfig(enabled=args.gdb, port=args.gdb_port),
        mounts=_parse_mounts(args),
        users=_parse_users(args),
        provision=ProvisionConfig(
            package_sets=args.package_set or ["base"],
            extra_packages=args.package,
        ),
        kernel=KernelConfig(
            enabled=args.kernel,
            version=kernel_version or "",
            rootfs=args.rootfs,
        ),
    )
    config.validate()
    return config


def new_project(args: argparse.Namespace) -> int:
    target = Path(args.directory).expanduser()
    if target.exists() and not target.is_dir():
        raise ProjectError(f"target exists and is not a directory: {target}")
    if target.exists() and any(target.iterdir()) and not args.force:
        raise ProjectError(
            f"target directory is not empty: {target} (use --force to overwrite)"
        )

    if args.interactive:
        if not sys.stdin.isatty():
            raise ProjectError("interactive mode requires a TTY")
        args = run_wizard(args, target)

    config = config_from_args(args, target)
    files = render_project(config)

    if args.dry_run:
        print(f"would create {target}")
        print(f"  {target / CONFIG_FILENAME}")
        print(f"  {target / LOCK_FILENAME}")
        for item in files:
            print(f"  {target / item.path}")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    for mount in config.mounts:
        source = Path(mount.source).expanduser()
        if not source.is_absolute():
            (target / source).mkdir(parents=True, exist_ok=True)

    config.save(target / CONFIG_FILENAME)
    distro = load_distro(config.distro.name)
    release = distro.release(config.distro.release)
    create_lock(config, distro, release).save(target / LOCK_FILENAME)
    wrote = write_rendered(target, files)
    manifest = GeneratedManifest()
    for item in files:
        manifest.record(item.path, item.content)
    manifest.save(GeneratedManifest.path(target))
    print(f"created {target}")
    print(f"  config: {target / CONFIG_FILENAME}")
    print(f"  lock:   {target / LOCK_FILENAME}")
    for path in wrote:
        print(f"  file:   {path}")
    print()
    print("next steps:")
    print(f"  cd {target}")
    print("  make diagnose")
    print("  make build")
    print("  make run")
    print("  make tty")
    return 0


def regen_project(args: argparse.Namespace) -> int:
    root, config = load_project(args.directory)
    files = render_project(config)
    manifest_path = GeneratedManifest.path(root)
    manifest = GeneratedManifest.load(manifest_path)

    updates = []
    conflicts = []
    unchanged = []
    for item in files:
        path = root / item.path
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            updates.append(item)
            continue
        except OSError as exc:
            raise ProjectError(f"cannot read {path}: {exc}") from exc
        if current == item.content:
            unchanged.append(item)
        elif manifest.is_unmodified(item.path, current):
            updates.append(item)
        else:
            conflicts.append(item)

    if args.check:
        for item in updates:
            print(f"would update {root / item.path}")
        for item in conflicts:
            print(f"conflict    {root / item.path}")
        if updates or conflicts:
            return 1
        print(f"{root}: generated files are up to date")
        return 0

    to_write = files if args.force else updates
    if to_write:
        write_rendered(root, to_write)

    written = {item.path for item in to_write}
    for item in files:
        if item.path in written or item in unchanged:
            manifest.record(item.path, item.content)
    manifest.save(manifest_path)

    for item in to_write:
        print(f"updated {root / item.path}")
    if conflicts and not args.force:
        for item in conflicts:
            print(
                f"conflict {root / item.path}: locally modified "
                "(use --force to overwrite)"
            )
        return 1
    if not to_write and not conflicts:
        print(f"{root}: generated files are up to date")
    return 0
