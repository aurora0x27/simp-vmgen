"""Project runtime layout under .simp-vmgen/."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config

RUNTIME_DIRNAME = ".simp-vmgen"


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    root: Path
    config: Config

    @property
    def runtime_dir(self) -> Path:
        return self.root / RUNTIME_DIRNAME

    @property
    def run_dir(self) -> Path:
        return self.runtime_dir / "run"

    @property
    def ssh_dir(self) -> Path:
        return self.runtime_dir / "ssh"

    @property
    def kernel_dir(self) -> Path:
        return self.runtime_dir / "kernel"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def overlay(self) -> Path:
        return self.run_dir / "overlay.qcow2"

    @property
    def seed_iso(self) -> Path:
        return self.run_dir / "seed.iso"

    @property
    def cloud_init_dir(self) -> Path:
        return self.run_dir / "cloud-init"

    @property
    def pid_file(self) -> Path:
        return self.run_dir / "qemu.pid"

    @property
    def state_file(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def socket_dir(self) -> Path:
        candidate = self.run_dir / "serial.sock"
        if len(str(candidate)) < 100:
            return self.run_dir
        digest = hashlib.sha1(str(self.root).encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"simp-vmgen-{digest}"

    @property
    def serial_sock(self) -> Path:
        return self.socket_dir / "serial.sock"

    @property
    def monitor_sock(self) -> Path:
        return self.socket_dir / "monitor.sock"

    @property
    def serial_log(self) -> Path:
        return self.run_dir / "serial.log"

    @property
    def qemu_log(self) -> Path:
        return self.run_dir / "qemu.log"

    @property
    def ssh_key(self) -> Path:
        key = Path(self.config.ssh.key).expanduser()
        return key if key.is_absolute() else self.root / key

    def ensure(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.socket_dir.mkdir(parents=True, exist_ok=True)
