"""Alpine minirootfs provisioning."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tarfile
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from . import __version__
from .cache import cache_dir, download
from .config import Config
from .distros import Distro, load_distro
from .errors import ProjectError
from .layout import ProjectLayout
from .mounts import resolve_mounts
from .rootfs import RootfsSpec, load_rootfs
from .rootfs_source import RootfsSourceInfo, fetch_rootfs
from .runner import run_streaming
from .seed import READY_MARKER
from .users import ProvisionUser, provision_users


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("simp_vmgen", "rootfs"),
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    env.filters["shquote"] = shlex.quote
    return env


def _write(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _run(argv: list[str], what: str, *, log_path: Path | None = None) -> None:
    result = run_streaming(argv, log_path=log_path, timeout=900, what=what)
    if result.returncode != 0:
        suffix = f" (log: {log_path})" if log_path is not None else ""
        detail = (result.stdout or "\n".join(result.tail)).strip()
        raise ProjectError(f"{what} failed{suffix}:\n{detail}")


def ensure_apk_tools(spec: RootfsSpec) -> Path:
    if not spec.apk_tools_url or not spec.apk_tools_filename:
        raise ProjectError(f"rootfs {spec.name!r} does not provide apk tools")
    target_dir = cache_dir() / "apk-tools" / spec.name
    binary = target_dir / "sbin" / "apk.static"
    if binary.is_file():
        return binary
    archive, _checksum = download(
        spec.apk_tools_url,
        spec.apk_tools_filename,
        checksum=spec.apk_tools_checksum,
        directory=target_dir,
    )
    try:
        with tarfile.open(archive) as handle:
            handle.extractall(target_dir)
    except (OSError, tarfile.TarError) as exc:
        raise ProjectError(f"cannot extract apk tools: {exc}") from exc
    if not binary.is_file():
        raise ProjectError(f"apk.static not found in {archive}")
    os.chmod(binary, 0o755)
    return binary


def install_packages(
    spec: RootfsSpec,
    rootfs: Path,
    packages: list[str],
    *,
    dry_run: bool = False,
    log_path: Path | None = None,
) -> None:
    packages = list(dict.fromkeys(packages))
    if not packages:
        return
    apk = ensure_apk_tools(spec)
    argv = [
        str(apk),
        "--root",
        str(rootfs),
        "--arch",
        "x86_64",
        "--keys-dir",
        str(rootfs / "etc" / "apk" / "keys"),
        "--repositories-file",
        str(rootfs / "etc" / "apk" / "repositories"),
        "--no-cache",
        "--no-scripts",
        "--no-chown",
        "add",
        *packages,
    ]
    if dry_run:
        print("+", " ".join(shlex.quote(item) for item in argv))
        return
    _run(argv, "cannot install Alpine packages", log_path=log_path)


def _password_hash(password: str) -> str:
    if not password:
        return "!"
    openssl = shutil.which("openssl")
    if openssl is None:
        raise ProjectError("openssl is required to hash Alpine passwords")
    try:
        result = subprocess.run(
            [openssl, "passwd", "-6", "-stdin"],
            input=password,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"cannot hash password: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ProjectError("cannot hash password with openssl")
    return result.stdout.strip()


def _load_colon_file(path: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split(":")
        if fields:
            result[fields[0]] = fields
    return result


def _write_colon_file(path: Path, entries: dict[str, list[str]], *, mode: int = 0o644) -> None:
    _write(
        path,
        "\n".join(":".join(fields) for fields in entries.values()) + "\n",
        mode=mode,
    )


def _allocate(used: set[int]) -> int:
    candidate = 1000
    while candidate in used:
        candidate += 1
    used.add(candidate)
    return candidate


def _prepare_users(
    config: Config,
    distro: Distro,
    rootfs: Path,
    users: list[ProvisionUser],
) -> None:
    passwd = _load_colon_file(rootfs / "etc" / "passwd")
    group = _load_colon_file(rootfs / "etc" / "group")
    shadow = _load_colon_file(rootfs / "etc" / "shadow")
    used_uids = {
        int(fields[2]) for fields in passwd.values() if len(fields) > 2 and fields[2].isdigit()
    }
    used_gids = {
        int(fields[2]) for fields in group.values() if len(fields) > 2 and fields[2].isdigit()
    }
    sudoers: list[str] = []

    for user in users:
        existing = passwd.get(user.name)
        if user.name == "root":
            uid = gid = 0
            home = "/root"
        elif existing is not None and len(existing) > 5:
            uid = int(existing[2])
            gid = int(existing[3])
            home = existing[5]
        else:
            uid = _allocate(used_uids)
            gid = _allocate(used_gids)
            home = f"/home/{user.name}"
            group[user.name] = [user.name, "x", str(gid), ""]

        passwd[user.name] = [
            user.name,
            "x",
            str(uid),
            str(gid),
            "",
            home,
            user.shell,
        ]
        shadow[user.name] = [
            user.name,
            _password_hash(user.password),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
        for name in user.groups:
            membership = group.get(name)
            if membership is None:
                gid = _allocate(used_gids)
                membership = [name, "x", str(gid), ""]
                group[name] = membership
            members = [item for item in membership[3].split(",") if item]
            if user.name not in members:
                members.append(user.name)
                membership[3] = ",".join(members)

        home_path = rootfs / home.lstrip("/")
        home_path.mkdir(parents=True, exist_ok=True)
        os.chmod(home_path, 0o700)
        skel = rootfs / "etc" / "skel"
        if skel.is_dir() and user.name != "root":
            shutil.copytree(skel, home_path, dirs_exist_ok=True)
        if user.ssh_keys:
            ssh_dir = home_path / ".ssh"
            ssh_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(ssh_dir, 0o700)
            _write(
                ssh_dir / "authorized_keys",
                "\n".join(user.ssh_keys) + "\n",
                mode=0o600,
            )
        if user.sudo and user.name != "root":
            sudoers.append(f"{user.name} ALL=(ALL) NOPASSWD:ALL")

    _write_colon_file(rootfs / "etc" / "passwd", passwd)
    _write_colon_file(rootfs / "etc" / "group", group)
    _write_colon_file(rootfs / "etc" / "shadow", shadow, mode=0o640)
    if sudoers:
        _write(
            rootfs / "etc" / "sudoers.d" / "90-simp-vmgen",
            "# Generated by simp-vmgen\n" + "\n".join(sudoers) + "\n",
            mode=0o440,
        )


def _write_ssh_config(config: Config, rootfs: Path, *, ssh_pwauth: bool) -> None:
    content = (
        _environment()
        .get_template("alpine/templates/sshd.conf.j2")
        .render(root_login=config.ssh.permit_root_login, ssh_pwauth=ssh_pwauth)
    )
    _write(rootfs / "etc" / "ssh" / "sshd_config.d" / "01-simp-vmgen.conf", content)


def _write_network(config: Config, rootfs: Path) -> None:
    _write(rootfs / "etc" / "hostname", f"{config.project.hostname}\n")
    _write(
        rootfs / "etc" / "network" / "interfaces",
        "auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet dhcp\n",
    )


def _write_init(config: Config, layout: ProjectLayout, rootfs: Path) -> None:
    mounts = resolve_mounts(config, layout.root)
    content = (
        _environment()
        .get_template("alpine/templates/init.j2")
        .render(
            config=config,
            tool_version=__version__,
            hostname=config.project.hostname,
            mounts=mounts,
        )
    )
    _write(rootfs / "init", content, mode=0o755)
    mtab = rootfs / "etc" / "mtab"
    if not mtab.exists() and not mtab.is_symlink():
        mtab.symlink_to("/proc/mounts")


def _write_ready(config: Config, rootfs: Path, *, ssh_pwauth: bool) -> None:
    context = {
        "config": config,
        "tool_version": __version__,
        "ready_marker": f"{READY_MARKER} name={config.project.hostname}",
        "root_login": config.ssh.permit_root_login,
        "ssh_pwauth": ssh_pwauth,
    }
    ready = _environment().get_template("alpine/templates/ready.sh.j2").render(**context)
    initd = _environment().get_template("alpine/templates/ready.initd.j2").render(**context)
    _write(rootfs / "usr" / "local" / "bin" / "simp-vmgen-ready", ready, mode=0o755)
    _write(rootfs / "etc" / "init.d" / "simp-vmgen-ready", initd, mode=0o755)


def _enable_service(rootfs: Path, runlevel: str, service: str) -> None:
    script = rootfs / "etc" / "init.d" / service
    if not script.is_file():
        return
    link = rootfs / "etc" / "runlevels" / runlevel / service
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists() and not link.is_symlink():
        link.symlink_to(f"/etc/init.d/{service}")


def _enable_services(rootfs: Path) -> None:
    for service in ("devfs", "procfs", "sysfs", "mtab"):
        _enable_service(rootfs, "sysinit", service)
    for service in ("bootmisc", "hostname", "localmount", "modules", "sysctl"):
        _enable_service(rootfs, "boot", service)
    for service in ("networking", "sshd", "simp-vmgen-ready", "local"):
        _enable_service(rootfs, "default", service)

    inittab = rootfs / "etc" / "inittab"
    if inittab.is_file():
        lines = inittab.read_text(encoding="utf-8").splitlines()
        serial = "ttyS0::respawn:/sbin/getty -L ttyS0 115200 vt100"
        found = False
        for index, line in enumerate(lines):
            if line.strip().startswith("ttyS0::") or line.strip() == (
                "#ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100"
            ):
                lines[index] = serial
                found = True
        if not found:
            lines.append(serial)
        _write(inittab, "\n".join(lines) + "\n")


def _copy_modules(layout: ProjectLayout, rootfs: Path) -> None:
    modules = layout.kernel_dir / "modules" / "lib"
    if modules.is_dir():
        shutil.copytree(modules, rootfs / "lib", dirs_exist_ok=True)


def prepare_alpine_rootfs(
    config: Config,
    root: Path,
    layout: ProjectLayout,
    *,
    generated_public_key: str = "",
    force: bool = False,
    dry_run: bool = False,
) -> RootfsSourceInfo:
    spec = load_rootfs(config.kernel.rootfs)
    if spec.name != "alpine":
        raise ProjectError(f"rootfs {spec.name!r} is not an Alpine rootfs")
    info = fetch_rootfs(config, root, layout, force=force, dry_run=dry_run)
    if dry_run:
        return info
    distro = load_distro(config.distro.name)
    users = provision_users(config, distro, root, generated_public_key=generated_public_key)
    packages = distro.packages(config.provision.package_sets, config.provision.extra_packages)
    packages.extend(spec.packages)
    install_packages(
        spec,
        info.root_dir,
        packages,
        log_path=layout.logs_dir / "alpine-packages.log",
    )
    ssh_pwauth = any(user.password for user in users)
    _prepare_users(config, distro, info.root_dir, users)
    _write_ssh_config(config, info.root_dir, ssh_pwauth=ssh_pwauth)
    _write_network(config, info.root_dir)
    _write_init(config, layout, info.root_dir)
    _write_ready(config, info.root_dir, ssh_pwauth=ssh_pwauth)
    _enable_services(info.root_dir)
    _copy_modules(layout, info.root_dir)
    return info
