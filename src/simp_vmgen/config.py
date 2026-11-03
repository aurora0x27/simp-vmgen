"""Project configuration model and TOML persistence."""

from __future__ import annotations

import os
import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from .errors import ConfigError

SCHEMA_VERSION = 1
CONFIG_FILENAME = "vmgen.toml"
LOCK_FILENAME = "vmgen.lock"
DEFAULT_SSH_KEY = ".simp-vmgen/ssh/id_ed25519"
DEFAULT_ROOT_PASSWORD = "114514"
DEFAULT_KERNEL_REPO = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_USER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SIZE_RE = re.compile(r"^([1-9][0-9]*)([KMGT])?$", re.IGNORECASE)
_CONFIG_OPTION_RE = re.compile(r"^CONFIG_[A-Za-z0-9_]+$")
_KERNEL_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
_KERNEL_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

_VM_ACCELS = {"auto", "kvm", "tcg"}
_VM_DISPLAYS = {"none", "gtk", "sdl", "vnc"}
_KERNEL_MODES = {"build", "artifact"}
_KERNEL_ARTIFACT_TYPES = {"bzImage", "vmlinuz", "vmlinux", "Image"}


def _table(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    return value


def _unknown_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigError(f"{where}: unknown key(s): {names}")


def _string(
    table: dict[str, Any],
    key: str,
    where: str,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key}: expected a string")
    if required and not value.strip():
        raise ConfigError(f"{where}.{key}: must not be empty")
    return value


def _bool(table: dict[str, Any], key: str, where: str, *, default: bool = False) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: expected a boolean")
    return value


def _int(
    table: dict[str, Any],
    key: str,
    where: str,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key}: expected an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}.{key}: must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{where}.{key}: must be <= {maximum}")
    return value


def _string_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{where}.{key}: expected an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{where}.{key}[{index}]: expected a non-empty string")
        result.append(item)
    return result


def _config_options(table: dict[str, Any], key: str, where: str) -> dict[str, str]:
    value = table.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.{key}: expected a table")
    result: dict[str, str] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or _CONFIG_OPTION_RE.match(name) is None:
            raise ConfigError(f"{where}.{key}: invalid Kconfig option {name!r}")
        if isinstance(raw, bool):
            option_value = "y" if raw else "n"
        elif isinstance(raw, int):
            option_value = str(raw)
        elif isinstance(raw, str):
            option_value = raw
        else:
            raise ConfigError(f"{where}.{key}.{name}: expected a string, integer or boolean")
        result[name] = option_value
    return result


def _port(value: Any, where: str) -> int | str:
    if isinstance(value, str):
        if value.lower() == "auto":
            return "auto"
        if value.isdigit():
            value = int(value)
        else:
            raise ConfigError(f'{where}: expected a port number or "auto"')
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f'{where}: expected a port number or "auto"')
    if not 1 <= value <= 65535:
        raise ConfigError(f"{where}: port must be between 1 and 65535")
    return value


def _size(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a size such as 4G")
    match = _SIZE_RE.match(value)
    if match is None:
        raise ConfigError(f"{where}: invalid size {value!r} (use e.g. 512M, 4G, 30G)")
    suffix = (match.group(2) or "").upper()
    return f"{match.group(1)}{suffix}"


def _absolute_target(value: str, where: str) -> str:
    if not value.startswith("/"):
        raise ConfigError(f"{where}: mount target must be an absolute path")
    target = posixpath.normpath(value)
    if target == "/":
        raise ConfigError(f"{where}: refusing to mount over /")
    return target


@dataclass(slots=True)
class ProjectConfig:
    name: str
    description: str = ""

    @property
    def hostname(self) -> str:
        value = re.sub(r"[^a-z0-9-]+", "-", self.name.lower()).strip("-")
        return value[:63].rstrip("-") or "lab"


@dataclass(slots=True)
class DistroConfig:
    name: str = "ubuntu"
    release: str = "24.04"
    arch: str = "x86_64"
    image: str = ""
    image_checksum: str = ""


@dataclass(slots=True)
class VmConfig:
    mem: str = "4G"
    cpus: int = 4
    disk: str = "30G"
    accel: str = "auto"
    display: str = "none"
    machine: str = ""
    snapshot: bool = False


@dataclass(slots=True)
class SshConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int | str = "auto"
    user: str = "root"
    key: str = DEFAULT_SSH_KEY
    permit_root_login: bool = True
    wait_timeout: int = 900


@dataclass(slots=True)
class GdbConfig:
    enabled: bool = False
    port: int | str = "auto"
    wait: bool = False


@dataclass(slots=True)
class MountConfig:
    source: str
    target: str
    readonly: bool = False
    tag: str = ""

    @classmethod
    def parse(cls, spec: str) -> MountConfig:
        parts = spec.split(":")
        if len(parts) not in (2, 3):
            raise ConfigError(f"invalid volume {spec!r}: expected SRC:DST[:ro]")
        source, target = parts[0], parts[1]
        mode = parts[2].lower() if len(parts) == 3 else ""
        if not source:
            raise ConfigError(f"invalid volume {spec!r}: source must not be empty")
        if mode not in ("", "ro", "rw"):
            raise ConfigError(f"invalid volume {spec!r}: mode must be 'ro' or 'rw'")
        return cls(
            source=source,
            target=_absolute_target(target, f"volume {spec!r}"),
            readonly=mode == "ro",
        )


@dataclass(slots=True)
class UserConfig:
    name: str
    password: str = ""
    shell: str = ""
    sudo: bool = False
    groups: list[str] = field(default_factory=list)
    ssh_keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProvisionConfig:
    timezone: str = "Asia/Shanghai"
    package_sets: list[str] = field(default_factory=lambda: ["base"])
    extra_packages: list[str] = field(default_factory=list)
    extra_user_data: str = ""


@dataclass(slots=True)
class KernelConfig:
    enabled: bool = False
    mode: str = "build"
    version: str = ""
    commit: str = ""
    repo: str = DEFAULT_KERNEL_REPO
    source_url: str = ""
    source_checksum: str = ""
    config: str = "defconfig"
    fragments: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)
    targets: list[str] = field(default_factory=lambda: ["bzImage"])
    make_args: list[str] = field(default_factory=list)
    jobs: int = 0
    rootfs: str = "busybox"
    artifact: str = ""
    artifact_type: str = "bzImage"
    initrd: str = ""
    append: str = ""


@dataclass(slots=True)
class Config:
    schema: int = SCHEMA_VERSION
    project: ProjectConfig = field(default_factory=lambda: ProjectConfig("lab"))
    distro: DistroConfig = field(default_factory=DistroConfig)
    vm: VmConfig = field(default_factory=VmConfig)
    ssh: SshConfig = field(default_factory=SshConfig)
    gdb: GdbConfig = field(default_factory=GdbConfig)
    mounts: list[MountConfig] = field(default_factory=lambda: [MountConfig("work", "/work")])
    users: list[UserConfig] = field(default_factory=lambda: [UserConfig("root", password="114514")])
    provision: ProvisionConfig = field(default_factory=ProvisionConfig)
    kernel: KernelConfig = field(default_factory=KernelConfig)

    @classmethod
    def default(cls, name: str = "lab") -> Config:
        return cls(project=ProjectConfig(name))

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        return cls.loads(text, source=str(path))

    @classmethod
    def loads(cls, text: str, *, source: str = "vmgen.toml") -> Config:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{source}: invalid TOML: {exc}") from exc
        return cls.from_dict(data, source=source)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "vmgen.toml") -> Config:
        _unknown_keys(
            data,
            {
                "schema",
                "project",
                "distro",
                "vm",
                "ssh",
                "gdb",
                "mount",
                "users",
                "provision",
                "kernel",
            },
            source,
        )

        schema = data.get("schema", SCHEMA_VERSION)
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise ConfigError(f"{source}.schema: expected an integer")
        if schema != SCHEMA_VERSION:
            raise ConfigError(
                f"{source}.schema: unsupported schema {schema} (expected {SCHEMA_VERSION})"
            )

        project_table = _table(data.get("project"), f"{source}.project")
        _unknown_keys(project_table, {"name", "description"}, f"{source}.project")
        name = _string(project_table, "name", f"{source}.project", required=True)
        if _PROJECT_NAME_RE.match(name) is None:
            raise ConfigError(
                f"{source}.project.name: invalid name {name!r} "
                "(use letters, digits, '.', '_' or '-')"
            )
        project = ProjectConfig(
            name=name,
            description=_string(project_table, "description", f"{source}.project"),
        )

        distro_table = _table(data.get("distro"), f"{source}.distro")
        _unknown_keys(
            distro_table,
            {"name", "release", "arch", "image", "image_checksum"},
            f"{source}.distro",
        )
        distro = DistroConfig(
            name=_string(distro_table, "name", f"{source}.distro", required=True),
            release=_string(distro_table, "release", f"{source}.distro", required=True),
            arch=_string(distro_table, "arch", f"{source}.distro", default="x86_64"),
            image=_string(distro_table, "image", f"{source}.distro"),
            image_checksum=_string(distro_table, "image_checksum", f"{source}.distro"),
        )

        vm_table = _table(data.get("vm"), f"{source}.vm")
        _unknown_keys(
            vm_table,
            {"mem", "cpus", "disk", "accel", "display", "machine", "snapshot"},
            f"{source}.vm",
        )
        vm = VmConfig(
            mem=_size(vm_table.get("mem", "4G"), f"{source}.vm.mem"),
            cpus=_int(vm_table, "cpus", f"{source}.vm", default=4, minimum=1),
            disk=_size(vm_table.get("disk", "30G"), f"{source}.vm.disk"),
            accel=_string(vm_table, "accel", f"{source}.vm", default="auto"),
            display=_string(vm_table, "display", f"{source}.vm", default="none"),
            machine=_string(vm_table, "machine", f"{source}.vm"),
            snapshot=_bool(vm_table, "snapshot", f"{source}.vm"),
        )
        if vm.accel not in _VM_ACCELS:
            raise ConfigError(f"{source}.vm.accel: expected one of {', '.join(sorted(_VM_ACCELS))}")
        if vm.display not in _VM_DISPLAYS:
            raise ConfigError(
                f"{source}.vm.display: expected one of {', '.join(sorted(_VM_DISPLAYS))}"
            )

        ssh_table = _table(data.get("ssh"), f"{source}.ssh")
        _unknown_keys(
            ssh_table,
            {"enabled", "host", "port", "user", "key", "permit_root_login", "wait_timeout"},
            f"{source}.ssh",
        )
        ssh = SshConfig(
            enabled=_bool(ssh_table, "enabled", f"{source}.ssh", default=True),
            host=_string(ssh_table, "host", f"{source}.ssh", default="127.0.0.1"),
            port=_port(ssh_table.get("port", "auto"), f"{source}.ssh.port"),
            user=_string(ssh_table, "user", f"{source}.ssh", default="root"),
            key=_string(ssh_table, "key", f"{source}.ssh", default=DEFAULT_SSH_KEY),
            permit_root_login=_bool(ssh_table, "permit_root_login", f"{source}.ssh", default=True),
            wait_timeout=_int(ssh_table, "wait_timeout", f"{source}.ssh", default=900, minimum=1),
        )

        gdb_table = _table(data.get("gdb"), f"{source}.gdb")
        _unknown_keys(gdb_table, {"enabled", "port", "wait"}, f"{source}.gdb")
        gdb = GdbConfig(
            enabled=_bool(gdb_table, "enabled", f"{source}.gdb"),
            port=_port(gdb_table.get("port", "auto"), f"{source}.gdb.port"),
            wait=_bool(gdb_table, "wait", f"{source}.gdb"),
        )

        mounts = _parse_mounts(data.get("mount"), f"{source}.mount")
        users = _parse_users(data.get("users"), f"{source}.users")

        provision_table = _table(data.get("provision"), f"{source}.provision")
        _unknown_keys(
            provision_table,
            {"timezone", "package_sets", "extra_packages", "extra_user_data"},
            f"{source}.provision",
        )
        provision = ProvisionConfig(
            timezone=_string(
                provision_table, "timezone", f"{source}.provision", default="Asia/Shanghai"
            ),
            package_sets=(
                _string_list(provision_table, "package_sets", f"{source}.provision")
                if "package_sets" in provision_table
                else ["base"]
            ),
            extra_packages=_string_list(provision_table, "extra_packages", f"{source}.provision"),
            extra_user_data=_string(provision_table, "extra_user_data", f"{source}.provision"),
        )

        kernel_table = _table(data.get("kernel"), f"{source}.kernel")
        _unknown_keys(
            kernel_table,
            {
                "enabled",
                "mode",
                "version",
                "commit",
                "repo",
                "source_url",
                "source_checksum",
                "config",
                "fragments",
                "options",
                "targets",
                "make_args",
                "jobs",
                "rootfs",
                "artifact",
                "artifact_type",
                "initrd",
                "append",
            },
            f"{source}.kernel",
        )
        kernel = KernelConfig(
            enabled=_bool(kernel_table, "enabled", f"{source}.kernel"),
            mode=_string(kernel_table, "mode", f"{source}.kernel", default="build"),
            version=_string(kernel_table, "version", f"{source}.kernel"),
            commit=_string(kernel_table, "commit", f"{source}.kernel"),
            repo=_string(
                kernel_table,
                "repo",
                f"{source}.kernel",
                default=DEFAULT_KERNEL_REPO,
            ),
            source_url=_string(kernel_table, "source_url", f"{source}.kernel"),
            source_checksum=_string(kernel_table, "source_checksum", f"{source}.kernel"),
            config=_string(kernel_table, "config", f"{source}.kernel", default="defconfig"),
            fragments=_string_list(kernel_table, "fragments", f"{source}.kernel"),
            options=_config_options(kernel_table, "options", f"{source}.kernel"),
            targets=_string_list(kernel_table, "targets", f"{source}.kernel") or ["bzImage"],
            make_args=_string_list(kernel_table, "make_args", f"{source}.kernel"),
            jobs=_int(kernel_table, "jobs", f"{source}.kernel", default=0, minimum=0),
            rootfs=_string(kernel_table, "rootfs", f"{source}.kernel", default="busybox"),
            artifact=_string(kernel_table, "artifact", f"{source}.kernel"),
            artifact_type=_string(
                kernel_table,
                "artifact_type",
                f"{source}.kernel",
                default="bzImage",
            ),
            initrd=_string(kernel_table, "initrd", f"{source}.kernel"),
            append=_string(kernel_table, "append", f"{source}.kernel"),
        )

        config = cls(
            schema=schema,
            project=project,
            distro=distro,
            vm=vm,
            ssh=ssh,
            gdb=gdb,
            mounts=mounts,
            users=users,
            provision=provision,
            kernel=kernel,
        )
        config.validate(source=source)
        return config

    def validate(self, *, source: str = "vmgen.toml") -> None:
        if self.schema != SCHEMA_VERSION:
            raise ConfigError(
                f"{source}.schema: unsupported schema {self.schema} (expected {SCHEMA_VERSION})"
            )
        if _PROJECT_NAME_RE.match(self.project.name) is None:
            raise ConfigError(f"{source}.project.name: invalid name {self.project.name!r}")
        if not self.distro.name.strip():
            raise ConfigError(f"{source}.distro.name: must not be empty")
        if not self.distro.release.strip():
            raise ConfigError(f"{source}.distro.release: must not be empty")
        if not self.distro.arch.strip():
            raise ConfigError(f"{source}.distro.arch: must not be empty")

        self.vm.mem = _size(self.vm.mem, f"{source}.vm.mem")
        self.vm.disk = _size(self.vm.disk, f"{source}.vm.disk")
        if self.vm.cpus < 1:
            raise ConfigError(f"{source}.vm.cpus: must be >= 1")
        if self.vm.accel not in _VM_ACCELS:
            raise ConfigError(f"{source}.vm.accel: invalid value {self.vm.accel!r}")
        if self.vm.display not in _VM_DISPLAYS:
            raise ConfigError(f"{source}.vm.display: invalid value {self.vm.display!r}")

        self.ssh.port = _port(self.ssh.port, f"{source}.ssh.port")
        self.gdb.port = _port(self.gdb.port, f"{source}.gdb.port")
        if not self.ssh.host.strip():
            raise ConfigError(f"{source}.ssh.host: must not be empty")
        if self.ssh.wait_timeout < 1:
            raise ConfigError(f"{source}.ssh.wait_timeout: must be >= 1")

        if not self.users:
            raise ConfigError(f"{source}.users: at least one user is required")
        seen_users: set[str] = set()
        for index, user in enumerate(self.users):
            where = f"{source}.users[{index}]"
            if _USER_NAME_RE.match(user.name) is None:
                raise ConfigError(
                    f"{where}.name: invalid user name {user.name!r} "
                    "(lowercase letters, digits, '_' or '-')"
                )
            if user.name in seen_users:
                raise ConfigError(f"{where}.name: duplicate user {user.name!r}")
            seen_users.add(user.name)
            for group in user.groups:
                if not group.strip():
                    raise ConfigError(f"{where}.groups: group names must not be empty")
            for key in user.ssh_keys:
                if not key.strip():
                    raise ConfigError(f"{where}.ssh_keys: keys must not be empty")

        login_user = self.get_user(self.ssh.user)
        if self.ssh.user == "root" and not self.ssh.permit_root_login:
            raise ConfigError(f"{source}.ssh: user is root but permit_root_login is false")
        if (
            self.ssh.enabled
            and not login_user.password
            and not login_user.ssh_keys
            and not self.ssh.key
        ):
            raise ConfigError(
                f"{source}.ssh: user {login_user.name!r} needs a password, "
                "an ssh key, or ssh.key must be set"
            )

        seen_targets: set[str] = set()
        seen_tags: set[str] = set()
        for index, mount in enumerate(self.mounts):
            where = f"{source}.mount[{index}]"
            if not mount.source.strip():
                raise ConfigError(f"{where}.source: must not be empty")
            if "," in mount.source:
                raise ConfigError(f"{where}.source: commas are not supported in 9p source paths")
            mount.target = _absolute_target(mount.target, where)
            if mount.target in seen_targets:
                raise ConfigError(f"{where}.target: duplicate target {mount.target!r}")
            seen_targets.add(mount.target)
            if mount.tag:
                if _TAG_RE.match(mount.tag) is None:
                    raise ConfigError(f"{where}.tag: invalid tag {mount.tag!r}")
                if mount.tag in seen_tags:
                    raise ConfigError(f"{where}.tag: duplicate tag {mount.tag!r}")
                seen_tags.add(mount.tag)

        if not self.provision.timezone.strip():
            raise ConfigError(f"{source}.provision.timezone: must not be empty")
        for index, package in enumerate(self.provision.extra_packages):
            if not package.strip():
                raise ConfigError(f"{source}.provision.extra_packages[{index}]: must not be empty")

        if self.gdb.enabled and self.ssh.port != "auto" and self.gdb.port == self.ssh.port:
            raise ConfigError(f"{source}.gdb.port: must differ from ssh.port when both are fixed")

        if self.kernel.enabled:
            kernel = self.kernel
            normalized_options: dict[str, str] = {}
            for name, raw in kernel.options.items():
                if _CONFIG_OPTION_RE.match(name) is None:
                    raise ConfigError(f"{source}.kernel.options: invalid Kconfig option {name!r}")
                if isinstance(raw, bool):
                    normalized_options[name] = "y" if raw else "n"
                elif isinstance(raw, int):
                    normalized_options[name] = str(raw)
                elif isinstance(raw, str):
                    normalized_options[name] = raw
                else:
                    raise ConfigError(f"{source}.kernel.options.{name}: invalid value {raw!r}")
            kernel.options = normalized_options
            if kernel.mode not in _KERNEL_MODES:
                raise ConfigError(
                    f"{source}.kernel.mode: expected one of {', '.join(sorted(_KERNEL_MODES))}"
                )
            if kernel.artifact:
                kernel.mode = "artifact"
            if kernel.mode == "artifact":
                if not kernel.artifact:
                    raise ConfigError(f"{source}.kernel.artifact: required in artifact mode")
                if kernel.version or kernel.commit:
                    raise ConfigError(
                        f"{source}.kernel: artifact mode cannot use version or commit"
                    )
                if kernel.artifact_type not in _KERNEL_ARTIFACT_TYPES:
                    raise ConfigError(
                        f"{source}.kernel.artifact_type: expected one of "
                        f"{', '.join(sorted(_KERNEL_ARTIFACT_TYPES))}"
                    )
            else:
                if kernel.version and kernel.commit:
                    raise ConfigError(f"{source}.kernel: version and commit are mutually exclusive")
                if not kernel.version and not kernel.commit:
                    raise ConfigError(
                        f"{source}.kernel: version or commit is required in build mode"
                    )
                if kernel.version and _KERNEL_VERSION_RE.match(kernel.version) is None:
                    raise ConfigError(
                        f"{source}.kernel.version: invalid version {kernel.version!r}"
                    )
                if kernel.commit and _KERNEL_COMMIT_RE.match(kernel.commit) is None:
                    raise ConfigError(
                        f"{source}.kernel.commit: expected a 7-40 character hex commit"
                    )
                if kernel.commit and not kernel.repo:
                    raise ConfigError(f"{source}.kernel.repo: must not be empty")
                if kernel.source_url and not kernel.source_url.startswith(("http://", "https://")):
                    raise ConfigError(f"{source}.kernel.source_url: expected an http(s) URL")
                if not kernel.config.strip():
                    raise ConfigError(f"{source}.kernel.config: must not be empty")
                if not kernel.targets:
                    raise ConfigError(f"{source}.kernel.targets: at least one target is required")
            if not kernel.rootfs.strip():
                raise ConfigError(f"{source}.kernel.rootfs: must not be empty")

    def get_user(self, name: str) -> UserConfig:
        for user in self.users:
            if user.name == name:
                return user
        raise ConfigError(f"ssh.user: user {name!r} is not defined in users")

    @property
    def login_user(self) -> UserConfig:
        return self.get_user(self.ssh.user)

    def mount_tags(self) -> list[str]:
        used = {mount.tag for mount in self.mounts if mount.tag}
        tags: list[str] = []
        index = 0
        for mount in self.mounts:
            if mount.tag:
                tags.append(mount.tag)
                continue
            while f"host{index}" in used:
                index += 1
            tag = f"host{index}"
            used.add(tag)
            tags.append(tag)
            index += 1
        return tags

    def to_dict(self) -> dict[str, Any]:
        project: dict[str, Any] = {"name": self.project.name}
        if self.project.description:
            project["description"] = self.project.description

        distro: dict[str, Any] = {
            "name": self.distro.name,
            "release": self.distro.release,
            "arch": self.distro.arch,
        }
        if self.distro.image:
            distro["image"] = self.distro.image
        if self.distro.image_checksum:
            distro["image_checksum"] = self.distro.image_checksum

        vm: dict[str, Any] = {
            "mem": self.vm.mem,
            "cpus": self.vm.cpus,
            "disk": self.vm.disk,
            "accel": self.vm.accel,
            "display": self.vm.display,
        }
        if self.vm.machine:
            vm["machine"] = self.vm.machine
        if self.vm.snapshot:
            vm["snapshot"] = True

        ssh: dict[str, Any] = {
            "enabled": self.ssh.enabled,
            "host": self.ssh.host,
            "port": self.ssh.port,
            "user": self.ssh.user,
            "permit_root_login": self.ssh.permit_root_login,
            "wait_timeout": self.ssh.wait_timeout,
        }
        if self.ssh.key:
            ssh["key"] = self.ssh.key

        gdb: dict[str, Any] = {
            "enabled": self.gdb.enabled,
            "port": self.gdb.port,
            "wait": self.gdb.wait,
        }

        mounts: list[dict[str, Any]] = []
        for mount in self.mounts:
            item: dict[str, Any] = {
                "source": mount.source,
                "target": mount.target,
            }
            if mount.readonly:
                item["readonly"] = True
            if mount.tag:
                item["tag"] = mount.tag
            mounts.append(item)

        users: list[dict[str, Any]] = []
        for user in self.users:
            item = {"name": user.name}
            if user.password:
                item["password"] = user.password
            if user.shell:
                item["shell"] = user.shell
            if user.sudo:
                item["sudo"] = True
            if user.groups:
                item["groups"] = list(user.groups)
            if user.ssh_keys:
                item["ssh_keys"] = list(user.ssh_keys)
            users.append(item)

        provision: dict[str, Any] = {
            "timezone": self.provision.timezone,
            "package_sets": list(self.provision.package_sets),
        }
        if self.provision.extra_packages:
            provision["extra_packages"] = list(self.provision.extra_packages)
        if self.provision.extra_user_data:
            provision["extra_user_data"] = self.provision.extra_user_data

        kernel: dict[str, Any] = {
            "enabled": self.kernel.enabled,
            "mode": self.kernel.mode,
            "config": self.kernel.config,
            "rootfs": self.kernel.rootfs,
        }
        if self.kernel.mode == "artifact":
            if self.kernel.artifact:
                kernel["artifact"] = self.kernel.artifact
            if self.kernel.artifact_type != "bzImage":
                kernel["artifact_type"] = self.kernel.artifact_type
        else:
            if self.kernel.version:
                kernel["version"] = self.kernel.version
            if self.kernel.commit:
                kernel["commit"] = self.kernel.commit
            if self.kernel.repo != DEFAULT_KERNEL_REPO:
                kernel["repo"] = self.kernel.repo
            if self.kernel.source_url:
                kernel["source_url"] = self.kernel.source_url
            if self.kernel.source_checksum:
                kernel["source_checksum"] = self.kernel.source_checksum
            if self.kernel.fragments:
                kernel["fragments"] = list(self.kernel.fragments)
            if self.kernel.options:
                kernel["options"] = dict(self.kernel.options)
            if self.kernel.targets != ["bzImage"]:
                kernel["targets"] = list(self.kernel.targets)
            if self.kernel.make_args:
                kernel["make_args"] = list(self.kernel.make_args)
            if self.kernel.jobs:
                kernel["jobs"] = self.kernel.jobs
        if self.kernel.initrd:
            kernel["initrd"] = self.kernel.initrd
        if self.kernel.append:
            kernel["append"] = self.kernel.append

        data: dict[str, Any] = {
            "schema": self.schema,
            "project": project,
            "distro": distro,
            "vm": vm,
            "ssh": ssh,
            "gdb": gdb,
            "users": users,
            "provision": provision,
            "kernel": kernel,
            "mount": mounts,
        }
        return data

    def dumps(self) -> str:
        data = self.to_dict()
        mount = data.pop("mount")
        users = data.pop("users")

        chunks: list[str] = [f"schema = {self.schema}\n"]
        if not mount:
            chunks.append("mount = []\n")
        for key in ("project", "distro", "vm", "ssh", "gdb"):
            chunks.append(_dump_named_table(key, data[key]))
        for mount_item in mount:
            chunks.append(_dump_array_table("mount", mount_item))
        for user in users:
            chunks.append(_dump_array_table("users", user))
        for key in ("provision", "kernel"):
            chunks.append(_dump_named_table(key, data[key]))

        return "\n\n".join(chunk.rstrip("\n") for chunk in chunks) + "\n"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        try:
            tmp.write_text(self.dumps(), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise ConfigError(f"cannot write {path}: {exc}") from exc


def _dump_named_table(name: str, data: dict[str, Any]) -> str:
    return tomli_w.dumps({name: data})


def _dump_array_table(name: str, data: dict[str, Any]) -> str:
    return f"[[{name}]]\n{tomli_w.dumps(data)}"


def _parse_mounts(value: Any, where: str) -> list[MountConfig]:
    if value is None:
        return [MountConfig("work", "/work")]
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected an array of tables")
    mounts: list[MountConfig] = []
    for index, item in enumerate(value):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{item_where}: expected a table")
        _unknown_keys(item, {"source", "target", "readonly", "tag"}, item_where)
        mounts.append(
            MountConfig(
                source=_string(item, "source", item_where, required=True),
                target=_absolute_target(
                    _string(item, "target", item_where, required=True), item_where
                ),
                readonly=_bool(item, "readonly", item_where),
                tag=_string(item, "tag", item_where),
            )
        )
    return mounts


def _parse_users(value: Any, where: str) -> list[UserConfig]:
    if value is None:
        return [UserConfig("root", password="114514")]
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected an array of tables")
    users: list[UserConfig] = []
    for index, item in enumerate(value):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{item_where}: expected a table")
        _unknown_keys(item, {"name", "password", "shell", "sudo", "groups", "ssh_keys"}, item_where)
        users.append(
            UserConfig(
                name=_string(item, "name", item_where, required=True),
                password=_string(item, "password", item_where),
                shell=_string(item, "shell", item_where),
                sudo=_bool(item, "sudo", item_where),
                groups=_string_list(item, "groups", item_where),
                ssh_keys=_string_list(item, "ssh_keys", item_where),
            )
        )
    return users
