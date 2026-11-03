"""QEMU command-line construction."""

from __future__ import annotations

import os
from pathlib import Path

from .config import Config
from .distros import Distro, DistroRelease
from .errors import ProjectError
from .layout import ProjectLayout
from .mounts import resolve_mounts


def kvm_available() -> bool:
    path = Path("/dev/kvm")
    return path.exists() and os.access(path, os.R_OK | os.W_OK)


def resolve_accel(config: Config) -> tuple[str, str]:
    if config.vm.accel == "kvm":
        if not kvm_available():
            raise ProjectError("KVM was requested but /dev/kvm is not writable")
        return "kvm", "host"
    if config.vm.accel == "tcg":
        return "tcg", "max"
    if kvm_available():
        return "kvm", "host"
    return "tcg", "max"


def build_argv(
    config: Config,
    distro: Distro,
    release: DistroRelease,
    layout: ProjectLayout,
    *,
    ssh_port: int,
    gdb_port: int | None,
    foreground: bool = False,
    snapshot: bool = False,
) -> list[str]:
    accel, cpu = resolve_accel(config)
    machine = config.vm.machine or release.machine or "q35"
    mounts = resolve_mounts(config, layout.root)

    argv = [
        f"qemu-system-{config.distro.arch}",
        "-name",
        config.project.hostname,
        "-machine",
        f"{machine},accel={accel}",
        "-cpu",
        cpu,
        "-smp",
        str(config.vm.cpus),
        "-m",
        config.vm.mem,
        "-rtc",
        "base=utc",
    ]

    if config.kernel.enabled:
        from .kernel import kernel_image_path, kernel_initrd_path

        append = "console=ttyS0 panic=-1"
        if not config.kernel.initrd:
            append += " rdinit=/init"
        if config.kernel.append:
            append += f" {config.kernel.append}"
        argv += [
            "-kernel",
            str(kernel_image_path(config, layout)),
            "-initrd",
            str(kernel_initrd_path(config, layout)),
            "-append",
            append,
        ]
    else:
        argv += [
            "-drive",
            f"if=virtio,file={layout.overlay},format=qcow2",
            "-drive",
            f"if=virtio,file={layout.seed_iso},format=raw,readonly=on",
        ]

    netdev = "user,id=n0"
    if config.ssh.enabled:
        netdev += f",hostfwd=tcp:{config.ssh.host}:{ssh_port}-:22"
    argv += ["-netdev", netdev, "-device", "virtio-net-pci,netdev=n0"]
    argv += ["-device", "virtio-rng-pci"]

    if config.vm.display == "none":
        argv += ["-display", "none"]
    elif config.vm.display == "vnc":
        argv += ["-display", "vnc=127.0.0.1:0"]
    else:
        argv += ["-display", config.vm.display]

    argv += [
        "-monitor",
        f"unix:{layout.monitor_sock},server=on,wait=off",
        "-chardev",
        (
            f"socket,id=ser0,path={layout.serial_sock},server=on,wait=off,"
            f"logfile={layout.serial_log},logappend=on"
        ),
        "-serial",
        "chardev:ser0",
        "-pidfile",
        str(layout.pid_file),
        "-D",
        str(layout.qemu_log),
    ]

    for mount in mounts:
        options = (
            f"local,path={mount.source},mount_tag={mount.tag},security_model=none,id={mount.tag}"
        )
        if mount.readonly:
            options += ",readonly=on"
        argv += ["-virtfs", options]

    if config.gdb.enabled:
        if gdb_port is None:
            raise ProjectError("gdb is enabled but no gdb port was allocated")
        argv += ["-gdb", f"tcp:127.0.0.1:{gdb_port}"]
        if config.gdb.wait:
            argv.append("-S")

    if snapshot or config.vm.snapshot:
        argv.append("-snapshot")
    if not foreground:
        argv.append("-daemonize")
    return argv
