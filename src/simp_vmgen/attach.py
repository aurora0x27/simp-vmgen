"""Raw terminal helpers for serial consoles and QEMU monitors."""

from __future__ import annotations

import contextlib
import os
import re
import select
import socket
import sys
import termios
import time
import tty
from pathlib import Path

from .errors import ProjectError

DETACH_BYTE = 0x1D  # Ctrl-]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def attach_socket(path: Path, *, detach_byte: int = DETACH_BYTE) -> int:
    if not path.exists():
        raise ProjectError(f"socket not found: {path} (is the VM running?)")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except OSError as exc:
        raise ProjectError(f"cannot connect to {path}: {exc}") from exc

    fd_in = sys.stdin.fileno()
    fd_out = sys.stdout.fileno()
    saved = None
    if sys.stdin.isatty():
        saved = termios.tcgetattr(fd_in)

    def restore() -> None:
        if saved is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(fd_in, termios.TCSADRAIN, saved)

    try:
        if sys.stdin.isatty():
            tty.setraw(fd_in)
        while True:
            ready, _, _ = select.select([fd_in, sock], [], [])
            if sock in ready:
                data = sock.recv(4096)
                if not data:
                    break
                os.write(fd_out, data)
            if fd_in in ready:
                data = os.read(fd_in, 1024)
                if not data:
                    break
                if detach_byte in data:
                    os.write(fd_out, b"\r\n[detached]\r\n")
                    break
                sock.sendall(data)
    finally:
        restore()
        sock.close()
    return 0


def show_log(path: Path, *, follow: bool, lines: int = 80, no_color: bool = False) -> int:
    if not path.is_file():
        raise ProjectError(f"log not found: {path} (run the VM first)")
    strip = no_color or not sys.stdout.isatty()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
            recent = content.splitlines()[-lines:]
            if recent:
                text = "\n".join(recent)
                print(_ANSI_RE.sub("", text) if strip else text)
            if not follow:
                return 0
            handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if line:
                    sys.stdout.write(_ANSI_RE.sub("", line) if strip else line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        raise ProjectError(f"cannot read log {path}: {exc}") from exc
