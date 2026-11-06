from __future__ import annotations

import json
from pathlib import Path

from simp_vmgen.cli import main
from simp_vmgen.config import Config


def _project(tmp_path: Path) -> Path:
    target = tmp_path / "lab"
    assert main(["new", str(target)]) == 0
    return target


def test_show_human(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    assert main(["-C", str(root), "show"]) == 0
    out = capsys.readouterr().out
    assert f"project : {root}" in out
    assert "[project]" in out
    assert 'name = "lab"' in out
    assert "noble-server-cloudimg-amd64.img" in out


def test_show_json(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    capsys.readouterr()
    assert main(["-C", str(root), "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["root"] == str(root)
    assert data["config"]["project"]["name"] == "lab"
    assert data["config"]["ssh"]["user"] == "root"
    assert data["mounts"][0]["source"] == "work"
    assert data["mounts"][0]["tag"] == "host0"
    assert data["lock"]["image_filename"].endswith(".img")


def test_show_json_redacts_passwords(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    capsys.readouterr()
    assert main(["-C", str(root), "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["config"]["users"][0]["password"] == "***"

    assert main(["-C", str(root), "show", "--json", "--show-secrets"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["config"]["users"][0]["password"] == "114514"


def test_show_rejects_unknown_distro(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    config = Config.load(root / "vmgen.toml")
    config.distro.name = "debian"
    config.save(root / "vmgen.toml")

    assert main(["-C", str(root), "show"]) == 1
    assert "unknown distro" in capsys.readouterr().err


def test_generated_readme_and_gitignore(tmp_path: Path) -> None:
    root = _project(tmp_path)
    readme = (root / "README.md").read_text()
    assert "make regen-check" in readme
    assert "make show" in readme
    assert "make diagnose" in readme
    assert "Non-interactive / agent usage" in readme
    assert "make log-follow" in readme
    assert "`root`" in readme
    assert "`/work`" in readme
    assert "vmgen.lock" in readme

    gitignore = (root / ".gitignore").read_text()
    assert ".simp-vmgen/" in gitignore
    assert "*.qcow2" in gitignore
    assert "*.iso" in gitignore

    makefile = (root / "Makefile").read_text()
    assert "regen-check:" in makefile
    assert "show:" in makefile
