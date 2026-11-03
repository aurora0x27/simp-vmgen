"""Global image cache with resumable downloads and checksum verification."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .errors import CacheError

_CHUNK_SIZE = 1024 * 1024


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "simp-vmgen"


def images_dir() -> Path:
    return cache_dir() / "images"


def kernel_sources_dir() -> Path:
    return cache_dir() / "kernel" / "sources"


@contextmanager
def _file_lock(path: Path) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Linux is the supported host
            yield handle
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _checksum_parts(checksum: str) -> tuple[str, str]:
    algorithm, separator, value = checksum.partition(":")
    algorithm = algorithm.lower()
    if separator != ":" or algorithm not in ("sha256", "sha512") or not value:
        raise CacheError(f"invalid checksum {checksum!r} (expected sha256:<hex> or sha512:<hex>)")
    return algorithm, value.lower()


def file_checksum(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(path: Path, checksum: str) -> bool:
    if not checksum:
        return True
    algorithm, expected = _checksum_parts(checksum)
    return file_checksum(path, algorithm) == expected


def parse_checksum_file(text: str, filename: str) -> str:
    wanted = Path(filename).name
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 1:
            digest = parts[0]
            algorithm = {64: "sha256", 128: "sha512"}.get(len(digest))
            if algorithm is not None:
                return f"{algorithm}:{digest.lower()}"
            continue
        if len(parts) != 2:
            continue
        digest, name = parts
        name = name.lstrip("*").strip()
        if Path(name).name != wanted:
            continue
        algorithm = {64: "sha256", 128: "sha512"}.get(len(digest))
        if algorithm is None:
            continue
        return f"{algorithm}:{digest.lower()}"
    raise CacheError(f"no checksum for {wanted!r} in the checksum file")


def fetch_text(url: str, *, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": f"simp-vmgen/{__version__}"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError) as exc:
        raise CacheError(f"cannot fetch {url}: {exc}") from exc


def resolve_checksum(checksum_url: str, filename: str) -> str:
    if not checksum_url:
        return ""
    return parse_checksum_file(fetch_text(checksum_url), filename)


def _download_to(url: str, target: Path) -> None:
    existing = target.stat().st_size if target.exists() else 0
    request = Request(url, headers={"User-Agent": f"simp-vmgen/{__version__}"})
    if existing:
        request.add_header("Range", f"bytes={existing}-")
    try:
        response = urlopen(request, timeout=60)
    except HTTPError as exc:
        if exc.code == 416 and existing:
            return
        if existing:
            target.unlink(missing_ok=True)
            _download_to(url, target)
            return
        raise CacheError(f"cannot download {url}: {exc}") from exc
    except (URLError, OSError) as exc:
        raise CacheError(f"cannot download {url}: {exc}") from exc

    status = getattr(response, "status", response.getcode())
    mode = "ab" if existing and status == 206 else "wb"
    try:
        with target.open(mode) as handle:
            shutil.copyfileobj(response, handle, _CHUNK_SIZE)
    finally:
        response.close()


def download(
    url: str,
    filename: str,
    *,
    checksum: str = "",
    checksum_url: str = "",
    allow_unverified: bool = False,
    directory: Path | None = None,
) -> tuple[Path, str]:
    """Return the cached image path and the checksum used for verification."""
    filename = Path(filename).name
    if not filename:
        raise CacheError(f"cannot determine a filename for {url!r}")
    target = (directory or images_dir()) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")

    with _file_lock(lock_path):
        if target.exists() and checksum and verify_checksum(target, checksum):
            return target, checksum
        if target.exists() and not checksum and allow_unverified:
            return target, ""

        resolved = checksum or resolve_checksum(checksum_url, filename)
        if not resolved and not allow_unverified:
            raise CacheError(
                f"no checksum available for {filename}; refusing an unverified download"
            )
        if target.exists() and resolved and verify_checksum(target, resolved):
            target.with_name(f"{target.name}.part").unlink(missing_ok=True)
            return target, resolved

        part = target.with_name(f"{target.name}.part")
        _download_to(url, part)
        if resolved and not verify_checksum(part, resolved):
            part.unlink(missing_ok=True)
            raise CacheError(f"checksum mismatch for {filename}: expected {resolved}")
        os.replace(part, target)
        return target, resolved
