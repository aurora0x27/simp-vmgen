"""Turn configured users and SSH keys into cloud-init provisioning data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .distros import Distro
from .errors import ProjectError

_KEY_PREFIXES = (
    "ssh-",
    "ecdsa-",
    "sk-ssh-",
    "sk-ecdsa-",
)


@dataclass(frozen=True, slots=True)
class ProvisionUser:
    name: str
    password: str
    shell: str
    sudo: bool
    groups: tuple[str, ...]
    ssh_keys: tuple[str, ...]


def _looks_like_key(value: str) -> bool:
    return value.strip().startswith(_KEY_PREFIXES)


def _read_key_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read SSH key file {path}: {exc}") from exc
    keys = [line.strip() for line in text.splitlines() if _looks_like_key(line)]
    if not keys:
        raise ProjectError(f"no SSH public key found in {path}")
    return keys


def resolve_ssh_keys(specs: list[str], root: Path) -> list[str]:
    keys: list[str] = []
    for spec in specs:
        if _looks_like_key(spec):
            candidates = [spec.strip()]
        else:
            path = Path(spec).expanduser()
            if not path.is_absolute():
                path = root / path
            candidates = _read_key_file(path)
        for key in candidates:
            if key not in keys:
                keys.append(key)
    return keys


def provision_users(
    config: Config,
    distro: Distro,
    root: Path,
    *,
    generated_public_key: str = "",
) -> list[ProvisionUser]:
    users: list[ProvisionUser] = []
    for user in config.users:
        keys = resolve_ssh_keys(user.ssh_keys, root)
        if (
            generated_public_key
            and user.name == config.ssh.user
            and generated_public_key not in keys
        ):
            keys.append(generated_public_key)
        users.append(
            ProvisionUser(
                name=user.name,
                password=user.password,
                shell=distro.shell_path(user.shell or distro.default_shell),
                sudo=user.sudo and user.name != "root",
                groups=tuple(user.groups),
                ssh_keys=tuple(keys),
            )
        )
    return users
