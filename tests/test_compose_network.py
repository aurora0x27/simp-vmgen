from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import compose as compose_module
from simp_vmgen import compose_network
from simp_vmgen.cli import build_parser
from simp_vmgen.compose import (
    ComposeError,
    derive_bridge_name,
    ensure_state,
    ensure_state_locked,
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

SOCKET = """schema = 1

[project]
name = "socket-lab"

[networks.lan]
driver = "socket"
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


def test_prepare_mode_preserves_socket_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "mixed-lab"

[networks.lan]
driver = "socket"
subnet = "192.168.96.0/24"

[networks.back]
driver = "bridge"
subnet = "192.168.97.0/24"

[services.a]
project = "./a"

[services.a.networks.lan]
address = "192.168.96.10/24"

[services.a.networks.back]
address = "192.168.97.10/24"
""",
    )
    compose = load_compose(root)
    first = ensure_state(compose)
    endpoint = first.networks["lan"]

    def unexpected(*args: object) -> str:
        pytest.fail("socket registry must not be touched during bridge prepare")

    monkeypatch.setattr(compose_module, "_allocate", unexpected)
    monkeypatch.setattr(compose_module, "_claim", unexpected)
    second = ensure_state_locked(compose, allocate_socket=False)
    assert second.networks == {"lan": endpoint}
    assert second.bridges["back"] in second.owned_bridges
    assert second.services == ["a"]


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

    ownership: list[Path] = []
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", exists)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(compose_network, "_require_bridge_type", lambda name: None)
    monkeypatch.setattr(
        compose_network, "fix_compose_ownership", lambda root: ownership.append(root)
    )

    assert compose_network.compose_network_prepare(SimpleNamespace(directory=str(root))) == 0
    assert ownership == [root]
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


def test_prepare_accepts_cap_net_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    existing: set[str] = set()
    commands: list[list[str]] = []
    run, exists = _fake_ip(existing, commands)
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(compose_network, "has_net_admin", lambda: True)
    monkeypatch.setattr(compose_network, "bridge_exists", exists)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(compose_network, "_require_bridge_type", lambda name: None)
    monkeypatch.setattr(compose_network, "fix_compose_ownership", lambda root: None)
    assert compose_network.compose_network_prepare(SimpleNamespace(directory=str(root))) == 0
    assert any("add" in command for command in commands)


def test_registry_schema_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    registry = tmp_path / "cache" / "simp-vmgen" / "compose" / "endpoints.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"schema": 99, "endpoints": {}}', encoding="utf-8")
    with pytest.raises(ComposeError, match="registry schema"):
        ensure_state(load_compose(root))


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
    monkeypatch.setattr(compose_network, "_require_bridge_type", lambda name: None)
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


def test_cleanup_checks_bridge_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    bridge = ensure_state(load_compose(root)).bridges["lan"]
    existing = {bridge}
    commands: list[list[str]] = []
    run, exists = _fake_ip(existing, commands)
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 0)
    monkeypatch.setattr(compose_network, "bridge_exists", exists)
    monkeypatch.setattr(compose_network, "_run_ip", run)
    monkeypatch.setattr(
        compose_network,
        "_require_bridge_type",
        lambda name: (_ for _ in ()).throw(ComposeError("not a bridge")),
    )
    monkeypatch.setattr(compose_network, "_running_services", lambda state: [])
    with pytest.raises(ComposeError, match="not a bridge"):
        compose_network.compose_network_cleanup(SimpleNamespace(directory=str(root)))
    assert not any("delete" in command for command in commands)


def test_socket_only_cleanup_does_not_require_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path, SOCKET)
    ensure_state(load_compose(root))
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 1000)
    assert compose_network.compose_network_cleanup(SimpleNamespace(directory=str(root))) == 0


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
    conf = tmp_path / "bridge.conf"
    conf.write_text("allow *\n", encoding="utf-8")
    monkeypatch.setattr(compose_network, "BRIDGE_CONF", conf)
    monkeypatch.setattr(compose_network.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(compose_network, "bridge_exists", lambda name: name == bridge)
    monkeypatch.setattr(compose_network, "bridge_helper", lambda: "/helper")
    assert (
        compose_network.compose_network_status(SimpleNamespace(directory=str(root), json=True)) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["privileged"] is False
    assert payload["bridge_helper"] == "/helper"
    assert payload["allowlist"] == ["*"]
    assert payload["networks"]["lan"]["bridge"] == bridge
    assert payload["networks"]["lan"]["owned"] is True
    assert payload["networks"]["lan"]["allowed"] is True
    assert payload["owned_bridges"] == [{"name": bridge, "exists": True}]


def test_network_dry_run_and_cli() -> None:
    parser = build_parser()
    args = parser.parse_args(["compose", "network", "prepare"])
    assert args.command == "compose"
    assert args.network_command == "prepare"
    args = parser.parse_args(["compose", "network", "status", "--json"])
    assert args.network_command == "status"
    assert args.json is True
