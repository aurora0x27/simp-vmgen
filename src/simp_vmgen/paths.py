"""Project discovery helpers."""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG_FILENAME, Config
from .errors import ProjectError


def find_project_root(start: Path | None = None) -> Path:
    """Find the nearest directory containing vmgen.toml."""
    current = (start or Path.cwd()).expanduser()
    if current.is_file():
        current = current.parent
    current = current.resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    raise ProjectError(f"no {CONFIG_FILENAME} found in {current} or any parent directory")


def resolve_project_root(directory: str | None = None) -> Path:
    if directory is None:
        return find_project_root()
    root = Path(directory).expanduser()
    if not root.exists():
        raise ProjectError(f"project directory does not exist: {root}")
    if not root.is_dir():
        raise ProjectError(f"project path is not a directory: {root}")
    root = root.resolve()
    if not (root / CONFIG_FILENAME).is_file():
        raise ProjectError(f"no {CONFIG_FILENAME} in {root}")
    return root


def load_project(directory: str | None = None) -> tuple[Path, Config]:
    root = resolve_project_root(directory)
    return root, Config.load(root / CONFIG_FILENAME)
