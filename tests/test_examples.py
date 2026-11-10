from __future__ import annotations

from pathlib import Path

from simp_vmgen.compose import load_compose, materialize_service
from simp_vmgen.config import Config

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "network-lab"


def test_network_lab_example_loads() -> None:
    compose = load_compose(EXAMPLE)
    assert compose.project.name == "network-lab"
    assert set(compose.services) == {"attacker", "target", "observer"}
    assert set(compose.networks) == {"lan"}
    assert compose.networks["lan"].driver == "socket"

    endpoints = {name: "239.192.77.1:4789" for name in compose.networks}
    for name, service in compose.services.items():
        config = Config.load(service.root / "vmgen.toml")
        config.validate()
        effective = materialize_service(compose, name, endpoints, {})
        assert len(effective.networks) == 1
        assert effective.networks[0].name == "lan"
        assert effective.networks[0].mcast == "239.192.77.1:4789"
