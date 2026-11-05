"""SSH key generation and public-key extraction."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import ProjectError


@dataclass(frozen=True, slots=True)
class SshKey:
    private_key: Path | None
    public_key: str


def _run(argv: list[str], what: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"{what} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"{what} failed: {detail}")
    return result


def ensure_ssh_key(config: Config, key_path: Path) -> SshKey | None:
    if not config.ssh.enabled or not config.ssh.key:
        return None

    key_path = key_path.expanduser()
    if key_path.suffix == ".pub" and key_path.is_file():
        text = key_path.read_text(encoding="utf-8").strip()
        if not text:
            raise ProjectError(f"empty SSH public key: {key_path}")
        return SshKey(None, text)

    if not key_path.is_file():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(key_path.parent, 0o700)
        _run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
            "cannot generate an SSH key",
        )
        os.chmod(key_path, 0o600)

    public_path = key_path.with_name(f"{key_path.name}.pub")
    if public_path.is_file():
        public_key = public_path.read_text(encoding="utf-8").strip()
    else:
        result = _run(
            ["ssh-keygen", "-y", "-f", str(key_path)],
            "cannot derive the SSH public key",
        )
        public_key = result.stdout.strip()
        if public_key:
            public_path.write_text(f"{public_key}\n", encoding="utf-8")
    if not public_key:
        raise ProjectError(f"cannot determine the public key for {key_path}")
    return SshKey(key_path, public_key)


def askpass_script(config: Config, path: Path, *, user_name: str | None = None) -> Path | None:
    password = config.get_user(user_name).password if user_name else config.login_user.password
    if not password:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(password)}\n")
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    except OSError as exc:
        raise ProjectError(f"cannot write askpass helper {path}: {exc}") from exc
    return path
