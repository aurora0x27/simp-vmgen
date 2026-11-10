from __future__ import annotations

import json
from pathlib import Path

import pytest

from simp_vmgen.cli import EXIT_ERROR, EXIT_OK, build_parser, main
from simp_vmgen.compose import (
    assert_standalone_allowed,
    clear_member,
    ensure_state,
    load_compose,
    materialize_service,
    state_path,
    write_member,
)
from simp_vmgen.config import Config, NetworkConfig
from simp_vmgen.errors import ComposeError


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _service(path: Path, name: str) -> Path:
    path.mkdir(parents=True)
    Config.default(name).save(path / "vmgen.toml")
    return path


def _compose(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "vmgen-compose.toml").write_text(text, encoding="utf-8")
    return root


BASE = """schema = 1

[project]
name = "network-lab"

[networks.lan]
driver = "socket"
subnet = "192.168.77.0/24"

[services.attacker]
project = "./attacker"

[services.attacker.networks.lan]
address = "192.168.77.10/24"

[services.target]
project = "./target"

[services.target.networks.lan]
address = "192.168.77.20/24"
"""


def _base(tmp_path: Path) -> Path:
    _service(tmp_path / "attacker", "attacker")
    _service(tmp_path / "target", "target")
    return _compose(tmp_path, BASE)


def test_compose_validate_and_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _base(tmp_path)
    assert main(["-C", str(root), "compose", "validate"]) == EXIT_OK
    assert "is valid" in capsys.readouterr().out

    assert main(["-C", str(root), "compose", "show"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "network-lab" in output
    assert "attacker" in output
    assert "192.168.77.10/24" in output
    assert state_path(root).is_file()


def test_compose_show_json_and_stable_endpoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _base(tmp_path)
    assert main(["-C", str(root), "compose", "show", "--json"]) == EXIT_OK
    first = json.loads(capsys.readouterr().out)
    assert (
        first["networks"]["lan"]["mcast"]
        == first["services"]["attacker"]["networks"]["lan"]["mcast"]
    )
    assert first["services"]["target"]["networks"]["lan"]["address"] == "192.168.77.20/24"

    assert main(["-C", str(root), "compose", "show", "--json"]) == EXIT_OK
    second = json.loads(capsys.readouterr().out)
    assert first["networks"]["lan"]["mcast"] == second["networks"]["lan"]["mcast"]


def test_materialize_service_adds_compose_networks(tmp_path: Path) -> None:
    root = _base(tmp_path)
    compose = load_compose(root)
    state = ensure_state(compose)
    config = materialize_service(compose, "attacker", state.networks)
    assert len(config.networks) == 1
    network = config.networks[0]
    assert network.name == "lan"
    assert network.address == "192.168.77.10/24"
    assert network.mcast == state.networks["lan"]
    config.validate()


def test_explicit_multicast_is_registered(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[networks.lan]
subnet = "192.168.77.0/24"
mcast = "239.192.99.1:29999"

[services.attacker]
project = "./attacker"
""",
    )
    compose = load_compose(root)
    state = ensure_state(compose)
    assert state.networks["lan"] == "239.192.99.1:29999"


def test_two_projects_get_distinct_endpoints(tmp_path: Path) -> None:
    first = _base(tmp_path / "first")
    second = _base(tmp_path / "second")
    first_state = ensure_state(load_compose(first))
    second_state = ensure_state(load_compose(second))
    assert first_state.networks["lan"] != second_state.networks["lan"]


def test_stale_registry_owner_is_reclaimed(tmp_path: Path) -> None:
    first = _base(tmp_path / "first")
    first_state = ensure_state(load_compose(first))
    state_path(first).unlink()

    second = _base(tmp_path / "second")
    second_state = ensure_state(load_compose(second))
    assert second_state.networks["lan"] != first_state.networks["lan"]
    registry = json.loads(
        (tmp_path / "cache" / "simp-vmgen" / "compose" / "endpoints.json").read_text()
    )
    assert first_state.networks["lan"] not in registry["endpoints"]


def test_validate_does_not_allocate_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _base(tmp_path)
    assert main(["-C", str(root), "compose", "validate"]) == EXIT_OK
    capsys.readouterr()
    assert not state_path(root).exists()


def test_removed_network_releases_endpoint(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    text = """schema = 1

[project]
name = "network-lab"

[networks.lan]
subnet = "192.168.77.0/24"

[networks.back]
subnet = "192.168.78.0/24"

[services.attacker]
project = "./attacker"

[services.attacker.networks.lan]
address = "192.168.77.10/24"

[services.attacker.networks.back]
address = "192.168.78.10/24"
"""
    root = _compose(tmp_path, text)
    first = ensure_state(load_compose(root))
    old_back = first.networks["back"]

    (root / "vmgen-compose.toml").write_text(
        text.replace('[networks.back]\nsubnet = "192.168.78.0/24"\n\n', "").replace(
            '\n[services.attacker.networks.back]\naddress = "192.168.78.10/24"\n', ""
        ),
        encoding="utf-8",
    )
    second = ensure_state(load_compose(root))
    assert "back" not in second.networks
    registry = json.loads(
        (tmp_path / "cache" / "simp-vmgen" / "compose" / "endpoints.json").read_text()
    )
    assert old_back not in registry["endpoints"]


def test_explicit_endpoint_conflict_is_rejected(tmp_path: Path) -> None:
    _service(tmp_path / "one" / "attacker", "attacker")
    _service(tmp_path / "two" / "attacker", "attacker")
    text = """schema = 1

[project]
name = "network-lab"

[networks.lan]
subnet = "192.168.77.0/24"
mcast = "239.192.99.1:29999"

[services.attacker]
project = "./attacker"

[services.attacker.networks.lan]
address = "192.168.77.10/24"
"""
    first = _compose(tmp_path / "one", text)
    second = _compose(tmp_path / "two", text)
    ensure_state(load_compose(first))
    with pytest.raises(ComposeError, match="already used"):
        ensure_state(load_compose(second))


BRIDGE = """schema = 1

[project]
name = "bridge-lab"

[networks.lan]
driver = "bridge"
subnet = "192.168.93.0/24"
bridge = "br-test"

[services.attacker]
project = "./attacker"

[services.attacker.networks.lan]
address = "192.168.93.10/24"
"""


def test_compose_bridge_topology_and_materialize(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(tmp_path, BRIDGE)
    assert main(["-C", str(root), "compose", "show", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["networks"]["lan"]["driver"] == "bridge"
    assert payload["networks"]["lan"]["bridge"] == "br-test"
    assert payload["networks"]["lan"]["mcast"] is None

    compose = load_compose(root)
    state = ensure_state(compose)
    config = materialize_service(compose, "attacker", state.networks, state.bridges)
    assert config.networks[0].bridge == "br-test"
    assert config.networks[0].mcast == ""


def test_driver_switch_keeps_address_and_mac(tmp_path: Path) -> None:
    socket_root = tmp_path / "socket"
    _service(socket_root / "attacker", "attacker")
    _compose(
        socket_root,
        """schema = 1

[project]
name = "socket-lab"

[networks.lan]
driver = "socket"
subnet = "192.168.77.0/24"

[services.attacker]
project = "./attacker"

[services.attacker.networks.lan]
address = "192.168.77.10/24"
""",
    )
    socket_compose = load_compose(socket_root)
    socket_state = ensure_state(socket_compose)
    socket_config = materialize_service(
        socket_compose, "attacker", socket_state.networks, socket_state.bridges
    )

    bridge_root = tmp_path / "bridge"
    _service(bridge_root / "attacker", "attacker")
    _compose(
        bridge_root,
        """schema = 1

[project]
name = "bridge-lab"

[networks.lan]
driver = "bridge"
subnet = "192.168.77.0/24"

[services.attacker]
project = "./attacker"

[services.attacker.networks.lan]
address = "192.168.77.10/24"
""",
    )
    bridge_compose = load_compose(bridge_root)
    bridge_state = ensure_state(bridge_compose)
    bridge_config = materialize_service(
        bridge_compose, "attacker", bridge_state.networks, bridge_state.bridges
    )
    assert socket_config.networks[0].address == bridge_config.networks[0].address
    assert socket_config.networks[0].mac == bridge_config.networks[0].mac
    assert socket_config.networks[0].mcast
    assert bridge_config.networks[0].bridge


def test_compose_rejects_bridge_with_socket_driver(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "bridge-lab"

[networks.lan]
driver = "socket"
subnet = "192.168.93.0/24"
bridge = "br-test"

[services.attacker]
project = "./attacker"
""",
    )
    with pytest.raises(ComposeError, match="only valid with driver"):
        load_compose(root)


def test_compose_rejects_duplicate_bridge(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "bridge-lab"

[networks.lan]
driver = "bridge"
subnet = "192.168.93.0/24"
bridge = "br-test"

[networks.back]
driver = "bridge"
subnet = "192.168.94.0/24"
bridge = "br-test"

[services.attacker]
project = "./attacker"
""",
    )
    with pytest.raises(ComposeError, match="duplicate bridge"):
        load_compose(root)


def test_compose_member_blocks_standalone_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _base(tmp_path)
    service = root / "attacker"
    write_member(service, root, "attacker")
    assert main(["-C", str(service), "run", "-n"]) == EXIT_ERROR
    assert "managed by compose" in capsys.readouterr().err

    monkeypatch.setenv("SIMP_VMGEN_COMPOSE_ACTIVE", "1")
    assert assert_standalone_allowed(service) is None
    clear_member(service)
    assert assert_standalone_allowed(service) is None


def test_service_config_must_not_define_networks(tmp_path: Path) -> None:
    service = _service(tmp_path / "attacker", "attacker")
    config = Config.load(service / "vmgen.toml")
    config.networks = [NetworkConfig("lan", address="192.168.77.10/24", mcast="239.192.77.1:4789")]
    config.validate()
    config.save(service / "vmgen.toml")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[networks.lan]
subnet = "192.168.77.0/24"

[services.attacker]
project = "./attacker"
""",
    )
    with pytest.raises(ComposeError, match="already defines"):
        load_compose(root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (BASE.replace('subnet = "192.168.77.0/24"', 'subnet = "192.168.77.0/31"'), "two host"),
        (BASE.replace('driver = "socket"', 'driver = "tap"'), "expected one of"),
        (
            BASE.replace('address = "192.168.77.20/24"', 'address = "192.168.78.20/24"'),
            "outside",
        ),
        (
            BASE.replace('address = "192.168.77.20/24"', 'address = "192.168.77.10/24"'),
            "share 192.168.77.10/24",
        ),
        (
            BASE.replace('address = "192.168.77.20/24"', 'address = "192.168.77.0/24"'),
            "network or broadcast",
        ),
    ],
)
def test_compose_validation_errors(tmp_path: Path, mutation: str, message: str) -> None:
    _service(tmp_path / "attacker", "attacker")
    _service(tmp_path / "target", "target")
    root = _compose(tmp_path, mutation)
    with pytest.raises(ComposeError, match=message):
        load_compose(root)


def test_compose_rejects_duplicate_multicast(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[networks.lan]
subnet = "192.168.77.0/24"
mcast = "239.192.1.1:20000"

[networks.back]
subnet = "192.168.78.0/24"
mcast = "239.192.1.1:20000"

[services.attacker]
project = "./attacker"
""",
    )
    with pytest.raises(ComposeError, match="duplicate multicast"):
        load_compose(root)


def test_compose_rejects_duplicate_project_path(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[services.one]
project = "./attacker"

[services.two]
project = "./attacker"
""",
    )
    with pytest.raises(ComposeError, match="same project"):
        load_compose(root)


def test_compose_rejects_duplicate_project_name(tmp_path: Path) -> None:
    _service(tmp_path / "one", "same")
    _service(tmp_path / "two", "same")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[services.one]
project = "./one"

[services.two]
project = "./two"
""",
    )
    with pytest.raises(ComposeError, match="same project name"):
        load_compose(root)


def test_compose_rejects_overlapping_subnets(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[networks.lan]
subnet = "192.168.77.0/24"

[networks.back]
subnet = "192.168.77.128/25"

[services.attacker]
project = "./attacker"
""",
    )
    with pytest.raises(ComposeError, match="overlapping"):
        load_compose(root)


def test_compose_rejects_unknown_network(tmp_path: Path) -> None:
    _service(tmp_path / "attacker", "attacker")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[networks.lan]
subnet = "192.168.77.0/24"

[services.attacker]
project = "./attacker"

[services.attacker.networks.back]
address = "192.168.77.10/24"
""",
    )
    with pytest.raises(ComposeError, match="unknown network"):
        load_compose(root)


def test_compose_rejects_duplicate_fixed_port(tmp_path: Path) -> None:
    for name in ("one", "two"):
        service = _service(tmp_path / name, name)
        config = Config.load(service / "vmgen.toml")
        config.ssh.port = 2222
        config.save(service / "vmgen.toml")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[services.one]
project = "./one"

[services.two]
project = "./two"
""",
    )
    with pytest.raises(ComposeError, match="ssh.port"):
        load_compose(root)


def test_compose_rejects_missing_service_project(tmp_path: Path) -> None:
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[services.missing]
project = "./missing"
""",
    )
    with pytest.raises(ComposeError, match="does not exist"):
        load_compose(root)


def test_compose_subcommands_are_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["compose", "validate"])
    assert args.command == "compose"
    assert args.compose_command == "validate"
    args = parser.parse_args(["compose", "show", "--json"])
    assert args.compose_command == "show"
    assert args.json is True


def test_compose_show_reports_state_errors(tmp_path: Path) -> None:
    root = _base(tmp_path)
    ensure_state(load_compose(root))
    state_path(root).write_text("{not json", encoding="utf-8")
    with pytest.raises(ComposeError, match="cannot read compose state"):
        ensure_state(load_compose(root))


def test_invalid_service_config_is_reported(tmp_path: Path) -> None:
    service = _service(tmp_path / "attacker", "attacker")
    (service / "vmgen.toml").write_text("schema = 1\n", encoding="utf-8")
    root = _compose(
        tmp_path,
        """schema = 1

[project]
name = "network-lab"

[services.attacker]
project = "./attacker"
""",
    )
    with pytest.raises(ComposeError):
        load_compose(root)
