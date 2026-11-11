"""Render the thin project files from templates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from . import __version__
from .arch import arch_profile
from .config import Config, MountConfig
from .errors import ProjectError


@dataclass(frozen=True, slots=True)
class RenderedFile:
    path: str
    content: str


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("simp_vmgen", "templates"),
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )


def _work_target(config: Config) -> str:
    for mount in config.mounts:
        if Path(mount.source).name == "work":
            return mount.target
    if config.mounts:
        return config.mounts[0].target
    return "/work"


def _mount_pairs(config: Config) -> list[tuple[MountConfig, str]]:
    return list(zip(config.mounts, config.mount_tags(), strict=True))


def render_project(config: Config) -> list[RenderedFile]:
    env = _environment()
    context = {
        "config": config,
        "tool_version": __version__,
        "console": arch_profile(config.distro.arch).console,
        "work_target": _work_target(config),
        "mounts": _mount_pairs(config),
    }
    return [
        RenderedFile("Makefile", env.get_template("Makefile.j2").render(**context)),
        RenderedFile("README.md", env.get_template("README.md.j2").render(**context)),
        RenderedFile(".gitignore", env.get_template("gitignore.j2").render(**context)),
    ]


def _write_atomic(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as exc:
        raise ProjectError(f"cannot write {path}: {exc}") from exc


def write_rendered(root: Path, files: list[RenderedFile]) -> list[Path]:
    written: list[Path] = []
    for item in files:
        path = root / item.path
        _write_atomic(path, item.content)
        written.append(path)
    return written


def changed_files(root: Path, files: list[RenderedFile]) -> list[RenderedFile]:
    changed: list[RenderedFile] = []
    for item in files:
        path = root / item.path
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            changed.append(item)
            continue
        except OSError as exc:
            raise ProjectError(f"cannot read {path}: {exc}") from exc
        if current != item.content:
            changed.append(item)
    return changed
