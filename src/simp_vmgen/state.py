"""Small runtime state file for allocated ports and the running VM."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import ProjectError


@dataclass(slots=True)
class RunState:
    ssh_port: int
    gdb_port: int | None
    pid: int
    started_at: str
    instance_id: str = ""

    @classmethod
    def load(cls, path: Path) -> RunState | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"cannot read runtime state {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectError(f"invalid runtime state {path}")
        try:
            return cls(
                ssh_port=int(data["ssh_port"]),
                gdb_port=(int(data["gdb_port"]) if data.get("gdb_port") is not None else None),
                pid=int(data["pid"]),
                started_at=str(data["started_at"]),
                instance_id=str(data.get("instance_id", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectError(f"invalid runtime state {path}: {exc}") from exc

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ProjectError(f"cannot write runtime state {path}: {exc}") from exc


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
