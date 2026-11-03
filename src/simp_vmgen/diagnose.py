"""Host dependency diagnostics."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import cache_dir
from .config import Config
from .errors import ProjectError
from .paths import load_project

_STATUS_LABEL = {
    "ok": "[ok]",
    "warn": "[--]",
    "missing": "[!!]",
    "optional": "[--]",
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    required: bool = True

    @property
    def is_failure(self) -> bool:
        return self.status == "missing"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "required": self.required,
        }


@dataclass(slots=True)
class DiagnosticReport:
    platform: str
    profile: str
    checks: list[CheckResult]

    @property
    def ok(self) -> bool:
        return not any(check.is_failure for check in self.checks)

    @property
    def missing(self) -> list[CheckResult]:
        return [check for check in self.checks if check.is_failure]

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "profile": self.profile,
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
        }


def _host_platform() -> str:
    return f"{platform.system().lower()} {platform.machine()}"


def _host_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def _cache_dir() -> Path:
    return cache_dir()


def _os_id() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    return ""


def _install_hint(tool: str) -> str:
    os_id = _os_id()
    packages: dict[str, str] = {
        "qemu-system-x86_64": "qemu-system-x86" if os_id in ("debian", "ubuntu") else "qemu-full",
        "qemu-img": "qemu-utils" if os_id in ("debian", "ubuntu") else "qemu-full",
        "cloud-localds": "cloud-image-utils",
        "ssh": "openssh",
        "ssh-keygen": "openssh",
        "pahole": "dwarves",
    }
    package = packages.get(tool, tool)
    if os_id in ("arch", "manjaro"):
        return f"sudo pacman -S {package}"
    if os_id in ("debian", "ubuntu"):
        return f"sudo apt install {package}"
    if os_id in ("fedora", "rhel", "centos"):
        return f"sudo dnf install {package}"
    if os_id == "alpine":
        return f"sudo apk add {package}"
    return f"install {package}"


def _check_tool(
    name: str,
    *,
    required: bool = True,
    version_args: tuple[str, ...] = (),
) -> CheckResult:
    path = shutil.which(name)
    if path is None:
        status = "missing" if required else "optional"
        return CheckResult(name, status, hint=_install_hint(name), required=required)

    detail = path
    if version_args:
        try:
            result = subprocess.run(
                [path, *version_args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output = (result.stdout or result.stderr).strip().splitlines()
            if output:
                detail = f"{path} ({output[0]})"
        except (OSError, subprocess.SubprocessError):
            pass
    return CheckResult(name, "ok", detail=detail, required=required)


def _check_seed_tool() -> CheckResult:
    for name in ("xorriso", "cloud-localds", "genisoimage"):
        path = shutil.which(name)
        if path is not None:
            return CheckResult("cloud-init seed tool", "ok", detail=f"{name} ({path})")
    return CheckResult(
        "cloud-init seed tool",
        "missing",
        hint="install xorriso, cloud-image-utils or genisoimage",
    )


def _check_kvm() -> CheckResult:
    path = Path("/dev/kvm")
    if not path.exists():
        return CheckResult(
            "kvm",
            "warn",
            detail="/dev/kvm not found; QEMU will use TCG (slow)",
            required=False,
        )
    if not os.access(path, os.R_OK | os.W_OK):
        return CheckResult(
            "kvm",
            "warn",
            detail="/dev/kvm is not readable/writable by this user",
            hint="add the user to the kvm group and log in again",
            required=False,
        )
    return CheckResult("kvm", "ok", detail="/dev/kvm is writable", required=False)


def _check_cache() -> tuple[CheckResult, CheckResult]:
    path = _cache_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".diagnose-", delete=True):
            pass
    except OSError:
        cache = CheckResult(
            "cache directory",
            "missing",
            detail=str(path),
            hint=f"make {path} writable or set XDG_CACHE_HOME",
        )
    else:
        cache = CheckResult("cache directory", "ok", detail=str(path))

    try:
        free = shutil.disk_usage(path if path.exists() else path.parent).free
    except OSError:
        disk = CheckResult(
            "disk space",
            "warn",
            detail="could not determine free space",
            required=False,
        )
    else:
        gib = free / (1024**3)
        if gib < 5:
            disk = CheckResult(
                "disk space",
                "warn",
                detail=f"{gib:.1f} GiB free in the cache filesystem",
                hint="images and kernels need several GiB",
                required=False,
            )
        else:
            disk = CheckResult("disk space", "ok", detail=f"{gib:.1f} GiB free")
    return cache, disk


def _check_port(name: str, host: str, port: int | str) -> CheckResult | None:
    if port == "auto":
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        return CheckResult(
            name,
            "warn",
            detail=f"{host}:{port} is unavailable ({exc})",
            hint="the port is in use; the VM may already be running",
            required=False,
        )
    return CheckResult(name, "ok", detail=f"{host}:{port} is free", required=False)


def _check_python() -> CheckResult:
    return CheckResult("python", "ok", detail=platform.python_version())


def _load_optional_project(directory: str | None) -> tuple[Path | None, Config | None]:
    if directory is not None:
        root, config = load_project(directory)
        return root, config
    try:
        root, config = load_project(None)
    except ProjectError:
        return None, None
    return root, config


def build_report(profile: str | None = None, directory: str | None = None) -> DiagnosticReport:
    _root, config = _load_optional_project(directory)
    arch = config.distro.arch if config is not None else _host_arch()
    if profile is not None:
        selected = profile
    elif config is not None and config.kernel.enabled:
        selected = "kernel"
    elif config is not None:
        selected = "cloudimg"
    else:
        selected = "base"

    checks: list[CheckResult] = [_check_python()]
    checks.append(_check_tool(f"qemu-system-{arch}", version_args=("--version",)))
    checks.append(_check_tool("qemu-img", version_args=("--version",)))
    checks.append(_check_tool("tar"))
    checks.append(_check_tool("gzip"))

    if selected in ("cloudimg", "kernel"):
        checks.append(_check_seed_tool())
        checks.append(_check_tool("ssh"))
        checks.append(_check_tool("ssh-keygen"))

    if selected == "kernel":
        for tool in (
            "make",
            "gcc",
            "flex",
            "bison",
            "bc",
            "perl",
            "cpio",
            "xz",
            "openssl",
            "pahole",
        ):
            checks.append(_check_tool(tool))
        checks.append(_check_tool("busybox", required=False))

    cache, disk = _check_cache()
    checks.extend([cache, disk, _check_kvm()])

    if config is not None:
        if config.ssh.enabled:
            port = _check_port("ssh port", config.ssh.host, config.ssh.port)
            if port is not None:
                checks.append(port)
        if config.gdb.enabled:
            port = _check_port("gdb port", "127.0.0.1", config.gdb.port)
            if port is not None:
                checks.append(port)

    return DiagnosticReport(_host_platform(), selected, checks)


def render_text(report: DiagnosticReport) -> str:
    lines = [
        f"platform : {report.platform}",
        f"profile  : {report.profile}",
    ]
    for check in report.checks:
        detail = f"  {check.detail}" if check.detail else ""
        lines.append(f"{_STATUS_LABEL.get(check.status, '[??]')} {check.name}{detail}")
        if check.hint and check.status in ("missing", "warn"):
            lines.append(f"       hint: {check.hint}")
    missing = len(report.missing)
    warnings = sum(check.status == "warn" for check in report.checks)
    if report.ok:
        lines.append(f"result   : ok ({warnings} warning(s))")
    else:
        lines.append(f"result   : {missing} missing tool(s), {warnings} warning(s)")
    return "\n".join(lines)


def diagnose(args: Any) -> int:
    report = build_report(
        profile=getattr(args, "profile", None),
        directory=getattr(args, "directory", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render_text(report))
    return 0 if report.ok else 1
