"""Track hashes of generated files to detect local modifications."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .errors import ProjectError

MANIFEST_PATH = ".simp-vmgen/generated.json"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class GeneratedManifest:
    files: dict[str, str] = field(default_factory=dict)
    tool_version: str = __version__

    @classmethod
    def path(cls, root: Path) -> Path:
        return root / MANIFEST_PATH

    @classmethod
    def load(cls, path: Path) -> GeneratedManifest:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"cannot read generated manifest {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectError(f"invalid generated manifest {path}")
        files_value = data.get("files", {})
        if not isinstance(files_value, dict):
            raise ProjectError(f"invalid generated manifest {path}: files must be an object")
        files = {
            str(name): str(digest)
            for name, digest in files_value.items()
        }
        return cls(files=files, tool_version=str(data.get("tool_version", __version__)))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        data = {"tool_version": self.tool_version, "files": dict(sorted(self.files.items()))}
        try:
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ProjectError(f"cannot write generated manifest {path}: {exc}") from exc

    def record(self, name: str, text: str) -> None:
        self.files[name] = text_hash(text)

    def is_unmodified(self, name: str, current: str) -> bool:
        recorded = self.files.get(name)
        return recorded is not None and recorded == text_hash(current)
