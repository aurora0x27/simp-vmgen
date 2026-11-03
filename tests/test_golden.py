from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.config import Config
from simp_vmgen.distros import load_distro
from simp_vmgen.render import render_project
from simp_vmgen.seed import render_cloud_init

GOLDEN = Path(__file__).parent / "golden"


@pytest.mark.parametrize("distro", ["ubuntu", "alpine"])
def test_project_files_match_golden(distro: str, tmp_path: Path) -> None:
    directory = GOLDEN / distro
    config = Config.load(directory / "vmgen.toml")
    rendered = {item.path: item.content for item in render_project(config)}
    for name in ("Makefile", "README.md", ".gitignore"):
        assert rendered[name] == (directory / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("distro", ["ubuntu", "alpine"])
def test_cloud_init_matches_golden(distro: str, tmp_path: Path) -> None:
    directory = GOLDEN / distro
    config = Config.load(directory / "vmgen.toml")
    (tmp_path / "work").mkdir()
    (tmp_path / "src").mkdir()
    artifacts = render_cloud_init(
        config,
        load_distro(distro),
        tmp_path,
        tmp_path / "run",
        generated_public_key="ssh-ed25519 AAAA generated",
        make_iso=False,
    )
    assert artifacts.user_data.read_text(encoding="utf-8") == (directory / "user-data").read_text(
        encoding="utf-8"
    )
    assert artifacts.meta_data.read_text(encoding="utf-8") == (directory / "meta-data").read_text(
        encoding="utf-8"
    )
