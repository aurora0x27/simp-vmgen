"""Target architecture profiles for QEMU and guest configuration."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from .errors import ProjectError


@dataclass(frozen=True, slots=True)
class ArchProfile:
    qemu: str
    machine: str
    console: str
    cpu_kvm: str = "host"
    cpu_tcg: str = "max"
    firmware_paths: tuple[str, ...] = ()

    @property
    def binary(self) -> str:
        return f"qemu-system-{self.qemu}"


ARCH_PROFILES = {
    "x86_64": ArchProfile(
        qemu="x86_64",
        machine="q35",
        console="ttyS0",
    ),
    "aarch64": ArchProfile(
        qemu="aarch64",
        machine="virt",
        console="ttyAMA0",
        firmware_paths=(
            "/usr/share/edk2/aarch64/QEMU_EFI.fd",
            "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
            "/usr/share/AAVMF/AAVMF_CODE.fd",
        ),
    ),
}


def host_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def arch_profile(arch: str) -> ArchProfile:
    try:
        return ARCH_PROFILES[arch]
    except KeyError as exc:
        supported = ", ".join(sorted(ARCH_PROFILES))
        raise ProjectError(f"unsupported architecture {arch!r} (supported: {supported})") from exc


def firmware_path(config_firmware: str, arch: str, root: Path) -> Path | None:
    profile = arch_profile(arch)
    if config_firmware:
        path = Path(config_firmware).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise ProjectError(f"firmware file not found: {path}")
        return path
    for candidate in profile.firmware_paths:
        path = Path(candidate)
        if path.is_file():
            return path
    if profile.firmware_paths:
        raise ProjectError(
            f"{arch} requires UEFI firmware; install edk2 firmware or set vm.firmware"
        )
    return None
