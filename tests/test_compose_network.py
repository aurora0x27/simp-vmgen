from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import compose_network
from simp_vmgen.cli import build_parser
from simp_vmgen.compose import (
    ComposeError,
    derive_bridge_name,
    ensure_state,
    load_compose,
    load_state,
)
from simp_vmgen.config import Config

BRIDGE = """schema = 1

[project]
name = "bridge-lab"

[networks.lan]
driver = "bridge"
subnet = "192.168.93.0/24"

[services.a]
project = "./a"

[services.a.networks.lan]
address = "192.168.93.10/24"
"""

EXTERNAL = BRIDGE.replace(
    'subnet = "192.168.93.0/24"',
    'subnet = "192.168.93.0/24"\nbridge = "br-ext"',
)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _compose(tmp_path: Path, text: str = BRIDGE) -> Path:
    service = tmp_path / "a"
    service.mkdir(exist_ok=True)
    Config.default("a").save(service / "vmgen.toml")
    (tmp_path / "vmgen-compose.toml").write_text(text, encoding="utf-8")
    return tmp_path


def _fake_ip(existing: set[str], commands: list[list[str]]) -> tuple[object, object]:
    def run(argv: list[str], what: str) -> None:
        commands.append(argv)
        if argv[:4] == ["ip", "link", "add", "name"]:
            existing.add(argv[4])
        elif argv[:4] == ["ip", "link", "delete", "dev"]:
            existing.discard(argv[4])
        elif argv[:4] == ["ip", "link", "set", "dev"]:
            pass

    def exists(name: str) -> bool:
        return name in existing

    return run, exists


def test_derive_bridge_name_is_valid() -> None:
    name = derive_bridge_name(Path("/project"), "lan")
    assert name.startswith("svbr")
    assert len(name) <= 15
    assert name.islower()


def test_prepare_creates_owned_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    compose = load_compose(root)
    bridge = ensure_state(compose).bridges["lan"]
    existing: set[str] = set()
    commands: list[list[str]] = []
    run, exists = _fake_ip(existing, commands)

    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", exists)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(compose_network, "_require_bridge_type", lambda name: None)

    assert compose_network.compose_network_prepare(SimpleNamespace(directory=str(root))) == 0
    assert bridge in existing
    state = load_state(root)
    assert state is not None
    assert state.owned_bridges == [bridge]
    assert [["ip", "link", "add", "name", bridge, "type", "bridge"]] == [
        command for command in commands if "add" in command
    ]


def test_prepare_reuses_external_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path, EXTERNAL)
    existing = {"br-ext"}
    commands: list[list[str]] = []
    run, exists = _fake_ip(existing, commands)
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", exists)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(compose_network, "_require_bridge_type", lambda name: None)

    assert compose_network.compose_network_prepare(SimpleNamespace(directory=str(root))) == 0
    assert not commands
    state = load_state(root)
    assert state is not None
    assert state.bridges["lan"] == "br-ext"
    assert state.owned_bridges == []


def test_prepare_refuses_unowned_existing_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path)
    bridge = derive_bridge_name(root, "lan")
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", lambda name: name == bridge)
    monkeypatch.setattr(compose_network, "_require_bridge_type", lambda name: None)
    monkeypatch.setattr(compose_network, "_run_ip", lambda argv, what: None)

    with pytest.raises(ComposeError, match="already exists"):
        compose_network.compose_network_prepare(SimpleNamespace(directory=str(root)))


def test_prepare_rolls_back_created_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    existing: set[str] = set()
    commands: list[list[str]] = []

    def run(argv: list[str], what: str) -> None:
        commands.append(argv)
        if argv[:4] == ["ip", "link", "add", "name"]:
            existing.add(argv[4])
        elif argv[:4] == ["ip", "link", "set", "dev"]:
            raise ComposeError("set failed")
        elif argv[:4] == ["ip", "link", "delete", "dev"]:
            existing.discard(argv[4])

    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", lambda name: name in existing)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(compose_network, "_require_bridge_type", lambda name: None)

    with pytest.raises(ComposeError, match="set failed"):
        compose_network.compose_network_prepare(SimpleNamespace(directory=str(root)))
    assert not existing
    assert load_state(root) is None


def test_prepare_requires_privilege(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 1000)
    with pytest.raises(ComposeError, match="needs root"):
        compose_network.compose_network_prepare(SimpleNamespace(directory=str(root)))


def test_cleanup_only_deletes_owned_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    compose = load_compose(root)
    state = ensure_state(compose)
    bridge = state.bridges["lan"]
    existing = {bridge, "br-external"}
    commands: list[list[str]] = []
    run, exists = _fake_ip(existing, commands)
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", exists)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(compose_network, "_running_services", lambda state: [])

    assert compose_network.compose_network_cleanup(SimpleNamespace(directory=str(root))) == 0
    assert bridge not in existing
    assert "br-external" in existing
    assert [["ip", "link", "delete", "dev", bridge]] == [
        command for command in commands if "delete" in command
    ]
    remaining = load_state(root)
    assert remaining is None or remaining.owned_bridges == []


def test_cleanup_does_not_touch_external_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path, EXTERNAL)
    ensure_state(load_compose(root))
    existing = {"br-ext"}
    commands: list[list[str]] = []
    run, exists = _fake_ip(existing, commands)
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", exists)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(compose_network, "_running_services", lambda state: [])

    assert compose_network.compose_network_cleanup(SimpleNamespace(directory=str(root))) == 0
    assert commands == []
    assert "br-ext" in existing


def test_cleanup_refuses_running_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    ensure_state(load_compose(root))
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "_running_services", lambda state: ["a"])
    with pytest.raises(ComposeError, match="still running"):
        compose_network.compose_network_cleanup(SimpleNamespace(directory=str(root)))


def test_network_status_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _compose(tmp_path)
    compose = load_compose(root)
    bridge = ensure_state(compose).bridges["lan"]
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(compose_network, "bridge_exists", lambda name: name == bridge)
    monkeypatch.setattr(compose_network, "bridge_helper", lambda: "/helper")
    assert (
        compose_network.compose_network_status(SimpleNamespace(directory=str(root), json=True)) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["privileged"] is False
    assert payload["bridge_helper"] == "/helper"
    assert payload["networks"]["lan"]["bridge"] == bridge
    assert payload["networks"]["lan"]["owned"] is True
    assert payload["owned_bridges"] == [{"name": bridge, "exists": True}]


def test_network_dry_run_and_cli() -> None:
    parser = build_parser()
    args = parser.parse_args(["compose", "network", "prepare"])
    assert args.command == "compose"
    assert args.network_command == "prepare"
    args = parser.parse_args(["compose", "network", "status", "--json"])
    assert args.network_command == "status"
    assert args.json is True
