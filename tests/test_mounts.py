from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.config import Config, MountConfig
from simp_vmgen.errors import ConfigError, ProjectError
from simp_vmgen.mounts import resolve_mounts


def test_resolve_relative_absolute_and_readonly(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "src").mkdir()
    absolute = tmp_path / "absolute"
    absolute.mkdir()

    config = Config.default("lab")
    config.mounts = [
        MountConfig("work", "/work"),
        MountConfig("src", "/src", readonly=True),
        MountConfig(str(absolute), "/absolute"),
    ]
    config.validate()
    resolved = resolve_mounts(config, tmp_path)
    assert [mount.target for mount in resolved] == ["/work", "/src", "/absolute"]
    assert [mount.tag for mount in resolved] == ["host0", "host1", "host2"]
    assert resolved[1].readonly is True
    assert resolved[2].source == absolute


def test_explicit_mount_tags(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    config = Config.default("lab")
    config.mounts = [
        MountConfig("work", "/work", tag="work"),
    ]
    config.validate()
    assert config.mount_tags() == ["work"]


def test_missing_source_is_rejected(tmp_path: Path) -> None:
    config = Config.default("lab")
    with pytest.raises(ProjectError, match="not a directory"):
        resolve_mounts(config, tmp_path)


def test_comma_in_source_is_rejected(tmp_path: Path) -> None:
    config = Config.default("lab")
    config.mounts = [MountConfig("work,data", "/work")]
    with pytest.raises(ConfigError, match="commas"):
        config.validate()


def test_duplicate_target_is_rejected() -> None:
    config = Config.default("lab")
    config.mounts = [
        MountConfig("a", "/work"),
        MountConfig("b", "/work"),
    ]
    with pytest.raises(ConfigError, match="duplicate target"):
        config.validate()
