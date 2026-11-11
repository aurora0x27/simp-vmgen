"""QEMU command-line construction."""

from __future__ import annotations

import os
from pathlib import Path

from .arch import arch_profile, firmware_path, host_arch
from .config import Config
from .distros import Distro, DistroRelease
from .errors import ProjectError
from .layout import ProjectLayout
from .mounts import resolve_mounts
from .network import management_mac, resolve_networks


def kvm_available() -> bool:
    path = Path("/dev/kvm")
    return path.exists() and os.access(path, os.R_OK | os.W_OK)


def resolve_accel(config: Config) -> tuple[str, str]:
    profile = arch_profile(config.distro.arch)
    native = config.distro.arch == host_arch()
    if config.vm.accel == "kvm":
        if not native:
            raise ProjectError(f"KVM cannot run {config.distro.arch} on a {host_arch()} host")
        if not kvm_available():
            raise ProjectError("KVM was requested but /dev/kvm is not writable")
        return "kvm", profile.cpu_kvm
    if config.vm.accel == "tcg":
        return "tcg", profile.cpu_tcg
    if native and kvm_available():
        return "kvm", profile.cpu_kvm
    return "tcg", profile.cpu_tcg


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
    dry_run: bool = False,
) -> list[str]:
    profile = arch_profile(config.distro.arch)
    accel, cpu = resolve_accel(config)
    machine = config.vm.machine or release.machine_for(config.distro.arch) or profile.machine
    mounts = resolve_mounts(config, layout.root)

    argv = [
        profile.binary,
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

    if not config.kernel.enabled:
        firmware = firmware_path(config.vm.firmware, config.distro.arch, layout.root)
        if firmware is not None:
            argv += ["-bios", str(firmware)]

    if config.kernel.enabled:
        from .kernel import built_image_path, kernel_image_path, kernel_initrd_path

        append = f"console={profile.console} panic=-1"
        if not config.kernel.initrd:
            append += " rdinit=/init"
        if config.kernel.append:
            append += f" {config.kernel.append}"
        if dry_run:
            if config.kernel.mode == "artifact":
                image = Path(config.kernel.artifact).expanduser()
                if not image.is_absolute():
                    image = layout.root / image
            else:
                image = built_image_path(config, layout) or (
                    layout.kernel_dir / "build" / "vmlinux"
                )
            if config.kernel.initrd:
                initrd = Path(config.kernel.initrd).expanduser()
                if not initrd.is_absolute():
                    initrd = layout.root / initrd
            else:
                initrd = layout.kernel_dir / "initramfs.cpio.gz"
        else:
            image = kernel_image_path(config, layout)
            initrd = kernel_initrd_path(config, layout)
        argv += [
            "-kernel",
            str(image),
            "-initrd",
            str(initrd),
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
    management_device = "virtio-net-pci,netdev=n0"
    if config.networks:
        management_device += f",mac={management_mac(config)}"
    argv += ["-netdev", netdev, "-device", management_device]
    for network in resolve_networks(config):
        netdev = (
            f"bridge,id={network.qemu_id},br={network.bridge}"
            if network.bridge
            else f"socket,id={network.qemu_id},mcast={network.mcast},localaddr=127.0.0.1"
        )
        argv += [
            "-netdev",
            netdev,
            "-device",
            f"virtio-net-pci,netdev={network.qemu_id},mac={network.mac}",
        ]
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
