"""Port allocation helpers."""

from __future__ import annotations

import socket

from .errors import ProjectError


def port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError:
        return False
    return True


def find_free_port(host: str, start: int = 2222, *, exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    for port in range(start, min(start + 1000, 65536)):
        if port in excluded:
            continue
        if port_available(host, port):
            return port
    raise ProjectError(f"no free TCP port found on {host} starting at {start}")


def resolve_port(
    host: str,
    requested: int | str,
    *,
    start: int = 2222,
    exclude: set[int] | None = None,
) -> int:
    if requested == "auto":
        return find_free_port(host, start=start, exclude=exclude)
    if not isinstance(requested, int):
        raise ProjectError(f"invalid port {requested!r}")
    if requested in (exclude or set()):
        raise ProjectError(f"port {requested} is already allocated")
    if not port_available(host, requested):
        raise ProjectError(f"{host}:{requested} is already in use")
    return requested
