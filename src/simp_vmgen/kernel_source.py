"""Fetch Linux kernel sources or resolve an existing kernel artifact."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .cache import download, file_checksum, kernel_sources_dir
from .config import Config
from .errors import ProjectError
from .layout import ProjectLayout
from .lock import KernelLock, Lock


@dataclass(frozen=True, slots=True)
class KernelSourceInfo:
    mode: str
    path: Path
    version: str = ""
    url: str = ""
    checksum: str = ""
    repo: str = ""
    commit: str = ""


def kernel_url(version: str) -> str:
    major = version.split(".", 1)[0]
    return (
        f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/"
        f"linux-{version}.tar.xz"
    )


def kernel_checksum_url(version: str) -> str:
    major = version.split(".", 1)[0]
    return f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/sha256sums.asc"


def _run(argv: list[str], what: str, *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(f"{what} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectError(f"{what} failed: {detail}")
    return result.stdout.strip()


def _extract_tarball(tarball: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    tar = shutil.which("tar")
    if tar is None:
        raise ProjectError("tar not found")
    _run([tar, "-xf", str(tarball), "-C", str(destination)], "cannot extract kernel source")
    entries = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(entries) == 1:
        return entries[0]
    expected = destination / tarball.name.removesuffix(".tar.xz")
    if expected.is_dir():
        return expected
    raise ProjectError(f"cannot locate the extracted kernel tree under {destination}")


def _fetch_tarball(
    config: Config, layout: ProjectLayout, *, dry_run: bool
) -> KernelSourceInfo:
    kernel = config.kernel
    url = kernel.source_url or kernel_url(kernel.version)
    filename = Path(urlparse(url).path).name
    if not filename:
        raise ProjectError(f"cannot determine kernel tarball name from {url!r}")
    checksum_url = "" if kernel.source_url else kernel_checksum_url(kernel.version)
    if not kernel.source_checksum and not checksum_url:
        raise ProjectError(
            "custom kernel source_url requires source_checksum for reproducibility"
        )
    destination = layout.kernel_dir / "src"
    tree = destination / f"linux-{kernel.version}"
    if dry_run:
        return KernelSourceInfo(
            mode="tarball",
            path=tree,
            version=kernel.version,
            url=url,
            checksum=kernel.source_checksum,
        )
    if not tree.is_dir():
        tarball, checksum = download(
            url,
            filename,
            checksum=kernel.source_checksum,
            checksum_url=checksum_url,
            directory=kernel_sources_dir(),
        )
        tree = _extract_tarball(tarball, destination)
    else:
        checksum = kernel.source_checksum
    return KernelSourceInfo(
        mode="tarball",
        path=tree,
        version=kernel.version,
        url=url,
        checksum=checksum,
    )


def _git_fetch(config: Config, tree: Path) -> str:
    kernel = config.kernel
    git = shutil.which("git")
    if git is None:
        raise ProjectError("git not found")
    if not tree.is_dir():
        tree.mkdir(parents=True)
        _run([git, "init", "-q"], "cannot initialize kernel git tree", cwd=tree)
        _run(
            [git, "remote", "add", "origin", kernel.repo],
            "cannot add kernel git remote",
            cwd=tree,
        )

    try:
        _run(
            [git, "fetch", "--depth=1", "origin", kernel.commit],
            "cannot fetch kernel commit",
            cwd=tree,
        )
    except ProjectError:
        _run(
            [git, "fetch", "origin"],
            "cannot fetch kernel repository",
            cwd=tree,
        )
        _run(
            [git, "checkout", "--detach", kernel.commit],
            "cannot check out kernel commit",
            cwd=tree,
        )
    else:
        _run(
            [git, "checkout", "--detach", "FETCH_HEAD"],
            "cannot check out kernel commit",
            cwd=tree,
        )
    return _run([git, "rev-parse", "HEAD"], "cannot resolve kernel commit", cwd=tree)


def _fetch_git(
    config: Config, layout: ProjectLayout, *, dry_run: bool
) -> KernelSourceInfo:
    tree = layout.kernel_dir / "src" / "linux-git"
    if dry_run:
        return KernelSourceInfo(
            mode="git",
            path=tree,
            repo=config.kernel.repo,
            commit=config.kernel.commit,
        )
    commit = _git_fetch(config, tree)
    return KernelSourceInfo(
        mode="git",
        path=tree,
        repo=config.kernel.repo,
        commit=commit,
    )


def artifact_path(config: Config, root: Path) -> Path:
    path = Path(config.kernel.artifact).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_file():
        raise ProjectError(f"kernel artifact not found: {path}")
    return path


def _resolve_artifact(config: Config, root: Path) -> KernelSourceInfo:
    path = artifact_path(config, root)
    return KernelSourceInfo(
        mode="artifact",
        path=path,
        checksum=f"sha256:{file_checksum(path)}",
    )


def resolve_source(
    config: Config, root: Path, layout: ProjectLayout, *, dry_run: bool = False
) -> KernelSourceInfo:
    if not config.kernel.enabled:
        raise ProjectError("kernel support is not enabled in vmgen.toml")
    if config.kernel.mode == "artifact":
        return _resolve_artifact(config, root)
    if config.kernel.commit:
        return _fetch_git(config, layout, dry_run=dry_run)
    return _fetch_tarball(config, layout, dry_run=dry_run)


def apply_to_lock(lock: Lock, info: KernelSourceInfo) -> None:
    if info.mode == "artifact":
        lock.kernel = KernelLock(
            source="artifact",
            artifact=str(info.path),
            artifact_checksum=info.checksum,
        )
    elif info.mode == "git":
        lock.kernel = KernelLock(
            source="git",
            repo=info.repo,
            commit=info.commit,
        )
    else:
        lock.kernel = KernelLock(
            source="tarball",
            version=info.version,
            url=info.url,
            checksum=info.checksum,
        )
