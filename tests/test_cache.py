from __future__ import annotations

import functools
import hashlib
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from simp_vmgen import cache
from simp_vmgen.errors import CacheError

PAYLOAD = b"sImp-vmgen image payload\n" * 1024
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def http_server(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "image.qcow2").write_bytes(PAYLOAD)
    (tmp_path / "SHA256SUMS").write_text(
        f"{SHA256}  image.qcow2\n", encoding="utf-8"
    )
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_parse_checksum_file() -> None:
    text = f"{SHA256}  image.qcow2\n{'b' * 128} *other\n"
    assert cache.parse_checksum_file(text, "image.qcow2") == f"sha256:{SHA256}"
    assert cache.parse_checksum_file(text, "other") == f"sha512:{'b' * 128}"
    with pytest.raises(CacheError, match="no checksum"):
        cache.parse_checksum_file(text, "missing")


def test_parse_single_hash_checksum_file() -> None:
    assert cache.parse_checksum_file(f"{'c' * 128}\n", "image.qcow2") == (
        f"sha512:{'c' * 128}"
    )


def test_verify_checksum(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(PAYLOAD)
    assert cache.verify_checksum(path, f"sha256:{SHA256}")
    assert not cache.verify_checksum(path, f"sha256:{'0' * 64}")
    with pytest.raises(CacheError, match="invalid checksum"):
        cache.verify_checksum(path, "md5:abc")


def test_download_with_checksum(
    tmp_path: Path, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "images_dir", lambda: tmp_path / "images")
    path, checksum = cache.download(
        f"{http_server}/image.qcow2",
        "image.qcow2",
        checksum=f"sha256:{SHA256}",
    )
    assert path.read_bytes() == PAYLOAD
    assert checksum == f"sha256:{SHA256}"

    # A second call uses the cache.
    again, _ = cache.download(
        f"{http_server}/image.qcow2",
        "image.qcow2",
        checksum=f"sha256:{SHA256}",
    )
    assert again == path


def test_download_uses_checksum_url(
    tmp_path: Path, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "images_dir", lambda: tmp_path / "images")
    path, checksum = cache.download(
        f"{http_server}/image.qcow2",
        "image.qcow2",
        checksum_url=f"{http_server}/SHA256SUMS",
    )
    assert path.read_bytes() == PAYLOAD
    assert checksum == f"sha256:{SHA256}"


def test_download_rejects_bad_checksum(
    tmp_path: Path, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "images_dir", lambda: tmp_path / "images")
    with pytest.raises(CacheError, match="checksum mismatch"):
        cache.download(
            f"{http_server}/image.qcow2",
            "image.qcow2",
            checksum=f"sha256:{'0' * 64}",
        )
    assert not (tmp_path / "images" / "image.qcow2.part").exists()


def test_download_requires_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "images_dir", lambda: tmp_path / "images")
    with pytest.raises(CacheError, match="no checksum"):
        cache.download("https://example.invalid/image.qcow2", "image.qcow2")
