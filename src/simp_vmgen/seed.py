"""Cloud-init seed rendering and ISO creation."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from . import __version__
from .config import Config
from .distros import Distro
from .errors import ProjectError
from .mounts import resolve_mounts
from .network import cloud_init_network_config
from .users import provision_users

READY_MARKER = "SIMP_VMGEN_READY"


@dataclass(frozen=True, slots=True)
class SeedArtifacts:
    user_data: Path
    meta_data: Path
    instance_id: str
    seed_iso: Path | None
    network_config: Path | None = None


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("simp_vmgen", "distros"),
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
    try:
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as exc:
        raise ProjectError(f"cannot write {path}: {exc}") from exc


def _extra_user_data(config: Config, root: Path) -> str:
    if not config.provision.extra_user_data:
        return ""
    path = Path(config.provision.extra_user_data).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read extra user-data {path}: {exc}") from exc


def _render_context(
    config: Config,
    distro: Distro,
    root: Path,
    *,
    generated_public_key: str,
) -> dict[str, object]:
    users = provision_users(config, distro, root, generated_public_key=generated_public_key)
    mounts = resolve_mounts(config, root)
    packages = distro.packages(config.provision.package_sets, config.provision.extra_packages)
    hostname = config.project.hostname
    context: dict[str, object] = {
        "config": config,
        "distro": distro,
        "tool_version": __version__,
        "hostname": hostname,
        "fqdn": f"{hostname}.local",
        "timezone": config.provision.timezone,
        "users": users,
        "packages": packages,
        "mounts": mounts,
        "root_login": config.ssh.permit_root_login,
        "ssh_pwauth": any(user.password for user in users),
        "ready_marker": f"{READY_MARKER} name={hostname}",
        "generated_public_key": generated_public_key,
    }
    return context


def render_cloud_init(
    config: Config,
    distro: Distro,
    root: Path,
    run_dir: Path,
    *,
    generated_public_key: str = "",
    make_iso: bool = True,
) -> SeedArtifacts:
    context = _render_context(config, distro, root, generated_public_key=generated_public_key)
    template_dir = f"{distro.name}/{distro.templates}"
    env = _environment()
    user_data = env.get_template(f"{template_dir}/user-data.j2").render(**context)
    extra = _extra_user_data(config, root)
    if extra:
        user_data = f"{user_data.rstrip()}\n\n# extra user data\n{extra.rstrip()}\n"

    network_config = cloud_init_network_config(config)
    hash_input = user_data if not network_config else f"{user_data}\n{network_config}"
    instance_id = (
        f"{config.project.hostname}-{hashlib.sha256(hash_input.encode()).hexdigest()[:12]}"
    )
    context["instance_id"] = instance_id
    meta_data = env.get_template(f"{template_dir}/meta-data.j2").render(**context)

    cloud_dir = run_dir / "cloud-init"
    user_data_path = cloud_dir / "user-data"
    meta_data_path = cloud_dir / "meta-data"
    network_config_path: Path | None = None
    _write(user_data_path, user_data)
    _write(meta_data_path, meta_data)
    if network_config:
        network_config_path = cloud_dir / "network-config"
        _write(network_config_path, network_config)

    seed_iso: Path | None = None
    if make_iso:
        seed_iso = run_dir / "seed.iso"
        _make_seed_iso(seed_iso, user_data_path, meta_data_path, network_config_path)
    return SeedArtifacts(
        user_data=user_data_path,
        meta_data=meta_data_path,
        instance_id=instance_id,
        seed_iso=seed_iso,
        network_config=network_config_path,
    )


def _make_seed_iso(
    seed_iso: Path, user_data: Path, meta_data: Path, network_config: Path | None = None
) -> None:
    seed_iso.unlink(missing_ok=True)
    xorriso = shutil.which("xorriso")
    cloud_localds = shutil.which("cloud-localds")
    genisoimage = shutil.which("genisoimage")

    if xorriso:
        command = [
            xorriso,
            "-as",
            "mkisofs",
            "-quiet",
            "-output",
            str(seed_iso),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            str(user_data),
            str(meta_data),
        ]
        if network_config is not None:
            command.append(str(network_config))
    elif cloud_localds:
        command = [cloud_localds]
        if network_config is not None:
            command.append(f"--network-config={network_config}")
        command += [str(seed_iso), str(user_data), str(meta_data)]
    elif genisoimage:
        command = [
            genisoimage,
            "-quiet",
            "-output",
            str(seed_iso),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            str(user_data),
            str(meta_data),
        ]
        if network_config is not None:
            command.append(str(network_config))
    else:
        raise ProjectError(
            "no cloud-init seed tool found (install xorriso, cloud-image-utils or genisoimage)"
        )

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"cannot create cloud-init seed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"cannot create cloud-init seed: {detail}")
    if not seed_iso.is_file() or seed_iso.stat().st_size == 0:
        raise ProjectError(f"cloud-init seed was not created: {seed_iso}")
