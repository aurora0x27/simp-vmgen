"""Streaming subprocess execution with persistent logs."""

from __future__ import annotations

import os
import selectors
import shlex
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .errors import ProjectError

_VERBOSE = False
_TAIL_LINES = 30


def set_verbose(value: bool) -> None:
    global _VERBOSE
    _VERBOSE = value


def is_verbose() -> bool:
    return _VERBOSE


@dataclass(frozen=True, slots=True)
class StreamResult:
    returncode: int
    log_path: Path | None
    tail: tuple[str, ...]
    stdout: str


def _log_path_text(path: Path | None) -> str:
    return f" (log: {path})" if path is not None else ""


def run_streaming(
    argv: list[str],
    *,
    log_path: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    echo: bool = True,
    capture: bool = False,
    what: str | None = None,
) -> StreamResult:
    """Run a command, stream its output and append it to ``log_path``."""
    label = what or argv[0]
    if _VERBOSE:
        print(f"+ {shlex.join(argv)}", flush=True)

    log_handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        log_handle.write(f"\n$ {shlex.join(argv)}\n")
        log_handle.flush()

    try:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise ProjectError(f"{label} failed: {exc}") from exc

        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        captured: list[str] = []
        deadline = time.monotonic() + timeout if timeout is not None else None

        try:
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    process.kill()
                    process.wait()
                    message = f"{label} timed out after {timeout:g}s{_log_path_text(log_path)}"
                    if log_handle is not None:
                        log_handle.write(f"[timeout after {timeout:g}s]\n")
                    raise ProjectError(message)
                events = selector.select(timeout=0.2)
                if events:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    if log_handle is not None:
                        log_handle.write(text)
                        log_handle.flush()
                    if echo:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                    if capture:
                        captured.append(text)
                    tail.extend(text.splitlines())
                elif process.poll() is not None:
                    break
        finally:
            selector.close()

        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise ProjectError(f"{label} did not exit{_log_path_text(log_path)}") from None
        if log_handle is not None:
            log_handle.write(f"[exit {returncode}]\n")
            log_handle.flush()
        return StreamResult(
            returncode=returncode,
            log_path=log_path,
            tail=tuple(tail),
            stdout="".join(captured).strip(),
        )
    finally:
        if log_handle is not None:
            log_handle.close()
