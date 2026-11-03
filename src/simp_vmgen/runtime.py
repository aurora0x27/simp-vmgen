"""Environment lifecycle: build, run, stop and status."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .cache import download, images_dir
from .config import LOCK_FILENAME, Config
from .distros import Distro, DistroRelease, load_distro
from .errors import ProjectError
from .layout import ProjectLayout
from .lock import Lock, create_lock
from .net import resolve_port
from .paths import load_project
from .qemu import build_argv
from .seed import render_cloud_init
from .state import RunState, utc_now


def _run_command(argv: list[str], what: str, *, timeout: int | None = None) -> None:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"{what} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"{what} failed: {detail}")


def _load_distro(config: Config) -> tuple[Distro, DistroRelease]:
    distro = load_distro(config.distro.name)
    return distro, distro.release(config.distro.release)


def _load_or_create_lock(
    root: Path, config: Config, distro: Distro, release: DistroRelease
) -> Lock:
    path = root / LOCK_FILENAME
    desired = create_lock(config, distro, release)
    if not path.is_file():
        return desired
    lock = Lock.load(path)
    if lock.image_url != desired.image_url:
        return desired
    lock.image_checksum = lock.image_checksum or desired.image_checksum
    lock.image_checksum_url = lock.image_checksum_url or desired.image_checksum_url
    lock.image_filename = lock.image_filename or desired.image_filename
    return lock


def _create_overlay(layout: ProjectLayout, base: Path, release: DistroRelease) -> None:
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        raise ProjectError("qemu-img not found")
    layout.overlay.unlink(missing_ok=True)
    _run_command(
        [
            qemu_img,
            "create",
            "-f",
            "qcow2",
            "-F",
            release.image_format,
            "-b",
            str(base),
            str(layout.overlay),
            layout.config.vm.disk,
        ],
        "cannot create the qcow2 overlay",
    )


def build(args: Any) -> int:
    root, config = load_project(getattr(args, "directory", None))
    distro, release = _load_distro(config)
    layout = ProjectLayout(root, config)
    layout.ensure()

    lock = _load_or_create_lock(root, config, distro, release)
    if getattr(args, "dry_run", False):
        base = images_dir() / lock.image_filename
        print(f"project : {root}")
        print(f"image   : {base}")
        print(f"overlay : {layout.overlay}")
        print(f"seed    : {layout.seed_iso}")
        print(f"lock    : {root / LOCK_FILENAME}")
        return 0

    base, checksum = download(
        lock.image_url,
        lock.image_filename,
        checksum=lock.image_checksum,
        checksum_url=lock.image_checksum_url,
    )
    if checksum and checksum != lock.image_checksum:
        lock.image_checksum = checksum
    lock.save(root / LOCK_FILENAME)

    if getattr(args, "force", False) or not layout.overlay.is_file():
        _create_overlay(layout, base, release)

    seed = render_cloud_init(config, distro, root, layout.run_dir)
    print(f"base image : {base}")
    print(f"overlay    : {layout.overlay}")
    print(f"seed       : {seed.seed_iso}")
    print(f"instance   : {seed.instance_id}")
    print("build done")
    return 0


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return True
    return len(fields) < 3 or fields[2] != "Z"


def running_pid(layout: ProjectLayout) -> int | None:
    pid = _read_pid(layout.pid_file)
    if pid is None or not _pid_alive(pid):
        return None
    return pid


def _cleanup_runtime(layout: ProjectLayout, *, remove_state: bool = True) -> None:
    layout.pid_file.unlink(missing_ok=True)
    layout.serial_sock.unlink(missing_ok=True)
    layout.monitor_sock.unlink(missing_ok=True)
    if remove_state:
        layout.state_file.unlink(missing_ok=True)


def run(args: Any) -> int:
    root, config = load_project(getattr(args, "directory", None))
    distro, release = _load_distro(config)
    layout = ProjectLayout(root, config)
    layout.ensure()

    if running_pid(layout) is not None:
        raise ProjectError("the VM is already running; run `simp-vmgen stop` first")
    if not layout.overlay.is_file() or not layout.seed_iso.is_file():
        raise ProjectError("VM artifacts are missing; run `simp-vmgen build` first")

    ssh_port = resolve_port(config.ssh.host, config.ssh.port, start=2222)
    gdb_port = None
    if config.gdb.enabled:
        gdb_port = resolve_port(
            "127.0.0.1", config.gdb.port, start=1234, exclude={ssh_port}
        )

    argv = build_argv(
        config,
        distro,
        release,
        layout,
        ssh_port=ssh_port,
        gdb_port=gdb_port,
        foreground=getattr(args, "foreground", False),
        snapshot=getattr(args, "snapshot", False),
    )
    if getattr(args, "dry_run", False):
        print(shlex.join(argv))
        return 0

    if shutil.which(argv[0]) is None:
        raise ProjectError(f"{argv[0]} not found")

    _cleanup_runtime(layout)
    layout.serial_log.write_text("", encoding="utf-8")
    foreground = getattr(args, "foreground", False)
    if foreground:
        try:
            foreground_result = subprocess.run(argv, check=False)
        except OSError as exc:
            raise ProjectError(f"cannot start QEMU: {exc}") from exc
        return foreground_result.returncode

    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ProjectError(f"cannot start QEMU: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"QEMU failed to start: {detail}")

    deadline = time.monotonic() + 5
    pid = None
    while time.monotonic() < deadline:
        pid = _read_pid(layout.pid_file)
        if pid is not None and _pid_alive(pid):
            break
        time.sleep(0.1)
    if pid is None or not _pid_alive(pid):
        raise ProjectError("QEMU did not create a pid file; check qemu.log")

    state = RunState(
        ssh_port=ssh_port,
        gdb_port=gdb_port,
        pid=pid,
        started_at=utc_now(),
    )
    state.save(layout.state_file)
    print(f"VM started: pid={pid}")
    if config.ssh.enabled:
        print(f"ssh       : {config.ssh.user}@{config.ssh.host}:{ssh_port}")
    if gdb_port is not None:
        print(f"gdb       : 127.0.0.1:{gdb_port}")
    print(f"serial log: {layout.serial_log}")
    return 0


def stop(args: Any) -> int:
    _root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(_root, config)
    pid = running_pid(layout)
    if pid is None:
        _cleanup_runtime(layout)
        print("VM is not running")
        return 0

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    _cleanup_runtime(layout)
    print(f"stopped QEMU (pid {pid})")
    return 0


def status(args: Any) -> int:
    root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(root, config)
    pid = running_pid(layout)
    if pid is None:
        print("stopped")
        return 0

    state = RunState.load(layout.state_file)
    print(f"running   pid={pid}")
    if state is not None:
        print(f"ssh       {config.ssh.user}@{config.ssh.host}:{state.ssh_port}")
        if state.gdb_port is not None:
            print(f"gdb       127.0.0.1:{state.gdb_port}")
        print(f"started   {state.started_at}")
    print(f"serial    {layout.serial_log}")
    return 0
