"""Environment lifecycle: build, run, stop and status."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import stamp
from .attach import attach_socket, show_log
from .cache import download, images_dir
from .config import LOCK_FILENAME, Config
from .distros import Distro, DistroRelease, load_distro
from .errors import ProjectError
from .kernel import ensure_kernel_artifacts, kernel_image_path, kernel_initrd_path
from .layout import ProjectLayout
from .lock import Lock, create_lock
from .net import resolve_port
from .paths import load_project
from .qemu import build_argv
from .seed import READY_MARKER, render_cloud_init
from .sshkeys import askpass_script, ensure_ssh_key
from .state import RunState, utc_now

EXIT_NOT_RUNNING = 3


def _run_command(argv: list[str], what: str, *, timeout: int | None = None) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
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

    if config.kernel.enabled:
        artifacts = ensure_kernel_artifacts(
            config, root, layout, dry_run=getattr(args, "dry_run", False)
        )
        if getattr(args, "dry_run", False):
            print(f"would use kernel image: {artifacts.image}")
            print(f"would use initrd      : {artifacts.initrd}")
        else:
            print(f"kernel image : {artifacts.image}")
            print(f"initrd       : {artifacts.initrd}")
        print("build done")
        return 0

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

    stamp.validate_cloud_build(
        config,
        layout,
        base=base,
        image_url=lock.image_url,
        image_filename=lock.image_filename,
        image_checksum=checksum or lock.image_checksum,
        force=getattr(args, "force", False),
    )
    if getattr(args, "force", False) and layout.overlay.is_file():
        print("warning: recreating overlay; guest data will be lost", file=sys.stderr)
    if getattr(args, "force", False) or not layout.overlay.is_file():
        _create_overlay(layout, base, release)

    key = ensure_ssh_key(config, layout.ssh_key)
    seed = render_cloud_init(
        config,
        distro,
        root,
        layout.run_dir,
        generated_public_key=key.public_key if key is not None else "",
    )
    stamp.save_cloud(
        config,
        layout,
        base=base,
        image_url=lock.image_url,
        image_filename=lock.image_filename,
        image_checksum=checksum or lock.image_checksum,
        seed_instance_id=seed.instance_id,
    )
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


def _cleanup_socket_dir(layout: ProjectLayout) -> None:
    if layout.socket_dir != layout.run_dir:
        shutil.rmtree(layout.socket_dir, ignore_errors=True)


def _wait_for_pid(layout: ProjectLayout, timeout: float = 5.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _read_pid(layout.pid_file)
        if pid is not None and _pid_alive(pid):
            return pid
        time.sleep(0.1)
    return None


def _save_run_state(
    layout: ProjectLayout, *, ssh_port: int, gdb_port: int | None, pid: int
) -> None:
    RunState(
        ssh_port=ssh_port,
        gdb_port=gdb_port,
        pid=pid,
        started_at=utc_now(),
    ).save(layout.state_file)


def _run_summary_data(
    config: Config,
    layout: ProjectLayout,
    *,
    ssh_port: int,
    gdb_port: int | None,
    pid: int,
) -> dict[str, Any]:
    return {
        "state": "running",
        "pid": pid,
        "ssh": (
            {
                "user": config.ssh.user,
                "host": config.ssh.host,
                "port": ssh_port,
            }
            if config.ssh.enabled
            else None
        ),
        "gdb": ({"host": "127.0.0.1", "port": gdb_port} if gdb_port is not None else None),
        "serial_log": str(layout.serial_log),
    }


def _print_run_summary(
    config: Config,
    layout: ProjectLayout,
    *,
    ssh_port: int,
    gdb_port: int | None,
    pid: int,
    json_output: bool = False,
) -> None:
    if json_output:
        print(
            json.dumps(
                _run_summary_data(config, layout, ssh_port=ssh_port, gdb_port=gdb_port, pid=pid),
                indent=2,
            )
        )
        return
    print(f"VM started: pid={pid}")
    if config.ssh.enabled:
        print(f"ssh       : {config.ssh.user}@{config.ssh.host}:{ssh_port}")
    if gdb_port is not None:
        print(f"gdb       : 127.0.0.1:{gdb_port}")
    print(f"serial log: {layout.serial_log}")


def run(args: Any) -> int:
    root, config = load_project(getattr(args, "directory", None))
    distro, release = _load_distro(config)
    layout = ProjectLayout(root, config)
    layout.ensure()

    if running_pid(layout) is not None:
        raise ProjectError("the VM is already running; run `simp-vmgen stop` first")
    if config.kernel.enabled:
        image = kernel_image_path(config, layout)
        initrd = kernel_initrd_path(config, layout)
        stamp.require_kernel_artifacts(config, layout, image, initrd)
    else:
        stamp.require_cloud_artifacts(config, layout)

    ssh_port = resolve_port(config.ssh.host, config.ssh.port, start=2222)
    gdb_port = None
    if config.gdb.enabled:
        gdb_port = resolve_port("127.0.0.1", config.gdb.port, start=1234, exclude={ssh_port})

    foreground = getattr(args, "foreground", False)
    argv = build_argv(
        config,
        distro,
        release,
        layout,
        ssh_port=ssh_port,
        gdb_port=gdb_port,
        foreground=foreground,
        snapshot=getattr(args, "snapshot", False),
    )
    if getattr(args, "dry_run", False):
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "state": "dry-run",
                        "qemu_argv": argv,
                        "foreground": foreground,
                        "snapshot": getattr(args, "snapshot", False),
                    },
                    indent=2,
                )
            )
        else:
            print(shlex.join(argv))
        return 0

    if shutil.which(argv[0]) is None:
        raise ProjectError(f"{argv[0]} not found")

    _cleanup_runtime(layout)
    layout.serial_log.write_text("", encoding="utf-8")

    if foreground:
        try:
            process = subprocess.Popen(argv)
        except OSError as exc:
            raise ProjectError(f"cannot start QEMU: {exc}") from exc
        pid = _wait_for_pid(layout)
        if pid is not None:
            _save_run_state(layout, ssh_port=ssh_port, gdb_port=gdb_port, pid=pid)
            _print_run_summary(
                config,
                layout,
                ssh_port=ssh_port,
                gdb_port=gdb_port,
                pid=pid,
                json_output=getattr(args, "json", False),
            )
        return process.wait()

    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ProjectError(f"cannot start QEMU: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"QEMU failed to start: {detail}")

    pid = _wait_for_pid(layout)
    if pid is None:
        raise ProjectError("QEMU did not create a pid file; check qemu.log")

    _save_run_state(layout, ssh_port=ssh_port, gdb_port=gdb_port, pid=pid)
    _print_run_summary(
        config,
        layout,
        ssh_port=ssh_port,
        gdb_port=gdb_port,
        pid=pid,
        json_output=getattr(args, "json", False),
    )
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
    _cleanup_socket_dir(layout)
    print(f"stopped QEMU (pid {pid})")
    return 0


def status(args: Any) -> int:
    root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(root, config)
    pid = running_pid(layout)
    state = RunState.load(layout.state_file) if pid is not None else None
    ready = _ready_marker_seen(layout) if pid is not None else False
    ssh_data: dict[str, Any] | None = (
        {
            "user": config.ssh.user,
            "host": config.ssh.host,
            "port": state.ssh_port,
        }
        if state is not None and config.ssh.enabled
        else None
    )
    gdb_data: dict[str, Any] | None = (
        {"host": "127.0.0.1", "port": state.gdb_port}
        if state is not None and state.gdb_port is not None
        else None
    )
    data: dict[str, Any] = {
        "state": "running" if pid is not None else "stopped",
        "pid": pid,
        "ssh": ssh_data,
        "gdb": gdb_data,
        "started_at": state.started_at if state is not None else None,
        "serial_log": str(layout.serial_log),
        "ready": ready,
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    elif pid is None:
        print("stopped")
    else:
        print(f"running   pid={pid}")
        if ssh_data is not None:
            print(f"ssh       {ssh_data['user']}@{ssh_data['host']}:{ssh_data['port']}")
        if gdb_data is not None:
            print(f"gdb       127.0.0.1:{gdb_data['port']}")
        if state is not None:
            print(f"started   {state.started_at}")
        print(f"ready     {'yes' if ready else 'no'}")
        print(f"serial    {layout.serial_log}")

    if pid is None and getattr(args, "check", False):
        return EXIT_NOT_RUNNING
    return 0


def _require_state(layout: ProjectLayout) -> RunState:
    if running_pid(layout) is None:
        raise ProjectError("the VM is not running; run `simp-vmgen run` first")
    state = RunState.load(layout.state_file)
    if state is None:
        raise ProjectError("runtime state is missing; restart the VM with `simp-vmgen run`")
    return state


def _remote_command(values: list[str] | None) -> list[str]:
    command = list(values or [])
    if command and command[0] == "--":
        command = command[1:]
    return command


def _remote_shell_command(remote: list[str]) -> str:
    """Render remote argv as one shell command string.

    A single argument is treated as an already-formed shell command. Multiple
    arguments are treated as argv and quoted so SSH does not lose boundaries.
    """
    if not remote:
        return ""
    if len(remote) == 1:
        return remote[0]
    return shlex.join(remote)


def _ssh_argv(
    config: Config,
    layout: ProjectLayout,
    state: RunState,
    remote: list[str],
    *,
    tty: bool,
    batch: bool,
    user: str | None = None,
    use_key: bool = True,
    connect_timeout: int = 5,
    workdir: str | None = None,
) -> list[str]:
    argv = [
        "ssh",
        "-p",
        str(state.ssh_port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "PreferredAuthentications=publickey,password",
    ]
    if use_key and layout.ssh_key.is_file() and layout.ssh_key.suffix != ".pub":
        argv += ["-i", str(layout.ssh_key)]
    if tty:
        argv.append("-t")
    if batch:
        argv += ["-o", "BatchMode=yes"]
    argv.append(f"{user or config.ssh.user}@{config.ssh.host}")
    command = _remote_shell_command(remote)
    if workdir:
        command = (
            f"cd {shlex.quote(workdir)} && {command}"
            if command
            else (f'cd {shlex.quote(workdir)} && exec "$SHELL" -l')
        )
    if command:
        argv.append(command)
    return argv


def _askpass_env(
    config: Config, layout: ProjectLayout, *, user_name: str | None = None
) -> dict[str, str] | None:
    script = askpass_script(config, layout.ssh_dir / "askpass.sh", user_name=user_name)
    if script is None:
        return None
    env = os.environ.copy()
    env["SSH_ASKPASS"] = str(script)
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env["DISPLAY"] = env.get("DISPLAY", ":0")
    return env


def _with_setsid(argv: list[str]) -> list[str]:
    if shutil.which("setsid"):
        return ["setsid", "-w", *argv]
    return argv


def _run_ssh(args: Any, *, tty: bool, require_command: bool) -> int:
    root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(root, config)
    if not config.ssh.enabled:
        raise ProjectError("SSH is disabled in vmgen.toml")
    state = _require_state(layout)

    remote = _remote_command(getattr(args, "guest_command", None))
    if require_command and not remote:
        raise ProjectError("a guest command is required")
    user_name = getattr(args, "user", None) or config.ssh.user
    target_user = config.get_user(user_name)
    has_key = layout.ssh_key.is_file() and layout.ssh_key.suffix != ".pub"
    use_key = has_key and (user_name == config.ssh.user or bool(target_user.ssh_keys))
    env = os.environ.copy()
    argv = _ssh_argv(
        config,
        layout,
        state,
        remote,
        tty=tty,
        batch=use_key,
        user=user_name,
        use_key=use_key,
        workdir=getattr(args, "workdir", None),
    )
    if not use_key and not tty:
        askpass = _askpass_env(config, layout, user_name=user_name)
        if askpass is None:
            raise ProjectError(f"no SSH key or password is available for user {user_name!r}")
        env = askpass
        argv = _with_setsid(argv)

    try:
        result = subprocess.run(argv, env=env, check=False)
    except OSError as exc:
        raise ProjectError(f"cannot run ssh: {exc}") from exc
    return result.returncode


def ssh(args: Any) -> int:
    remote = _remote_command(getattr(args, "guest_command", None))
    return _run_ssh(args, tty=not remote, require_command=False)


def tty(args: Any) -> int:
    return _run_ssh(args, tty=True, require_command=False)


def guest(args: Any) -> int:
    return _run_ssh(args, tty=False, require_command=True)


def _ready_marker_seen(layout: ProjectLayout) -> bool:
    try:
        return READY_MARKER in layout.serial_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _serial_tail(layout: ProjectLayout, lines: int = 20) -> str:
    try:
        content = layout.serial_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def wait(args: Any) -> int:
    _root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(_root, config)
    _require_state(layout)
    timeout = getattr(args, "timeout", None) or config.ssh.wait_timeout
    json_output = getattr(args, "json", False)
    start = time.monotonic()
    deadline = start + timeout
    last_report = start

    if not json_output:
        print(f"waiting for the guest (timeout {timeout}s) ...")
    while time.monotonic() < deadline:
        if _ready_marker_seen(layout):
            elapsed = time.monotonic() - start
            if json_output:
                print(
                    json.dumps(
                        {
                            "ready": True,
                            "elapsed_seconds": round(elapsed, 3),
                            "timeout": timeout,
                            "serial_log": str(layout.serial_log),
                        },
                        indent=2,
                    )
                )
            else:
                print("guest is ready")
            return 0
        if running_pid(layout) is None:
            tail = _serial_tail(layout)
            detail = f"\n{tail}" if tail else ""
            raise ProjectError(
                "the VM exited before the guest became ready; "
                f"see `simp-vmgen log` ({layout.serial_log}){detail}"
            )
        now = time.monotonic()
        if not json_output and now - last_report >= 5:
            print(f"  still waiting... {int(now - start)}s", file=sys.stderr)
            last_report = now
        time.sleep(0.5)

    tail = _serial_tail(layout)
    detail = f"\n{tail}" if tail else ""
    raise ProjectError(
        f"timed out after {timeout}s waiting for the guest; "
        f"see `simp-vmgen log` ({layout.serial_log}){detail}"
    )


def gdb(args: Any) -> int:
    _root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(_root, config)
    state = _require_state(layout)
    if not config.gdb.enabled or state.gdb_port is None:
        raise ProjectError("the gdb stub is not enabled for this VM")
    command = ["gdb", "-ex", f"target remote 127.0.0.1:{state.gdb_port}"]
    if getattr(args, "connect", False):
        try:
            return subprocess.run(command, check=False).returncode
        except OSError as exc:
            raise ProjectError(f"cannot run gdb: {exc}") from exc
    print(f"gdb port : 127.0.0.1:{state.gdb_port}")
    print(f"connect  : {' '.join(command)}")
    return 0


def serial(args: Any) -> int:
    _root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(_root, config)
    _require_state(layout)
    print(
        "attaching to the serial console; Ctrl-] detaches",
        file=sys.stderr,
    )
    return attach_socket(layout.serial_sock)


def console(args: Any) -> int:
    _root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(_root, config)
    _require_state(layout)
    print(
        "attaching to the QEMU monitor; try `info status`, Ctrl-] detaches",
        file=sys.stderr,
    )
    return attach_socket(layout.monitor_sock)


def log(args: Any) -> int:
    _root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(_root, config)
    return show_log(layout.serial_log, follow=getattr(args, "follow", False))


def clean(args: Any) -> int:
    root, config = load_project(getattr(args, "directory", None))
    layout = ProjectLayout(root, config)
    stop(args)
    _cleanup_socket_dir(layout)
    if getattr(args, "all", False):
        shutil.rmtree(layout.runtime_dir, ignore_errors=True)
        print(f"removed {layout.runtime_dir}")
    else:
        shutil.rmtree(layout.run_dir, ignore_errors=True)
        print(f"removed {layout.run_dir}")
    return 0
