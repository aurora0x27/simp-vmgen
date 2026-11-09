from __future__ import annotations

import pytest
import yaml

from simp_vmgen import stamp
from simp_vmgen.config import Config, NetworkConfig
from simp_vmgen.errors import ConfigError
from simp_vmgen.network import (
    cloud_init_network_config,
    network_fingerprint,
    resolve_networks,
)


def _config(*networks: NetworkConfig) -> Config:
    config = Config.default("lab")
    config.networks = list(networks)
    config.validate()
    return config


def test_network_config_round_trip() -> None:
    config = _config(
        NetworkConfig(
            "lan",
            address="192.168.77.10/24",
            gateway="192.168.77.1",
            mcast="239.192.77.1:4789",
        ),
        NetworkConfig("back", dhcp=True, mcast="239.192.77.2:4789"),
        NetworkConfig(
            "storage",
            address="10.0.7.2/24",
            mac="02:11:22:33:44:55",
            mcast="239.192.77.3:4789",
        ),
    )
    loaded = Config.loads(config.dumps())
    assert loaded.networks == config.networks
    assert "[[network]]" in config.dumps()


def test_no_networks_keeps_config_unchanged() -> None:
    config = Config.default("lab")
    config.validate()
    assert "network" not in config.dumps()


def test_effective_mac_is_stable_and_unicast() -> None:
    first = NetworkConfig("lan", dhcp=True, mcast="239.192.77.1:4789")
    second = NetworkConfig("back", dhcp=True, mcast="239.192.77.2:4789")
    mac = first.effective_mac("lab")
    assert mac == first.effective_mac("lab")
    assert mac != second.effective_mac("lab")
    assert mac != first.effective_mac("other")
    assert int(mac[:2], 16) & 1 == 0


def test_resolved_networks_use_effective_mac_and_index() -> None:
    config = _config(
        NetworkConfig("lan", dhcp=True, mcast="239.192.77.1:4789"),
        NetworkConfig("back", dhcp=True, mcast="239.192.77.2:4789"),
    )
    networks = resolve_networks(config)
    assert [network.name for network in networks] == ["lan", "back"]
    assert [network.qemu_id for network in networks] == ["net1", "net2"]
    assert networks[0].mac == config.networks[0].effective_mac("lab")


def test_cloud_init_network_config() -> None:
    config = _config(
        NetworkConfig(
            "lan",
            address="192.168.77.10/24",
            gateway="192.168.77.1",
            mcast="239.192.77.1:4789",
        ),
        NetworkConfig("back", dhcp=True, mcast="239.192.77.2:4789"),
    )
    data = yaml.safe_load(cloud_init_network_config(config))
    assert data["version"] == 2
    assert data["ethernets"]["mgmt"]["dhcp4"] is True
    assert "addresses" not in data["ethernets"]["mgmt"]
    lan = data["ethernets"]["lan"]
    assert lan["match"]["macaddress"] == config.networks[0].effective_mac("lab")
    assert lan["dhcp4"] is False
    assert lan["addresses"] == ["192.168.77.10/24"]
    assert lan["routes"] == [{"to": "0.0.0.0/0", "via": "192.168.77.1"}]
    back = data["ethernets"]["back"]
    assert back["dhcp4"] is True
    assert "addresses" not in back


def test_no_network_config_without_networks() -> None:
    assert cloud_init_network_config(_config()) == ""


def test_fingerprint_tracks_effective_mac_and_address() -> None:
    config = _config(NetworkConfig("lan", dhcp=True, mcast="239.192.77.1:4789"))
    before = network_fingerprint(config)
    assert before[0]["mac"] == config.networks[0].effective_mac("lab")

    config.networks[0].address = "192.168.77.10/24"
    config.networks[0].dhcp = False
    config.validate()
    after = network_fingerprint(config)
    assert before != after


def test_config_digest_tracks_networks() -> None:
    config = _config()
    before = stamp.config_digest(config)
    config.networks = [NetworkConfig("lan", dhcp=True, mcast="239.192.77.1:4789")]
    config.validate()
    after = stamp.config_digest(config)
    assert before != after

    config.networks[0].mcast = "239.192.77.2:4789"
    config.validate()
    assert stamp.config_digest(config) != after


@pytest.mark.parametrize(
    ("network", "message"),
    [
        (NetworkConfig("Lan", dhcp=True, mcast="239.192.77.1:4789"), "invalid network name"),
        (NetworkConfig("lo", dhcp=True, mcast="239.192.77.1:4789"), "reserved"),
        (NetworkConfig("lan", dhcp=True), "expected a multicast endpoint"),
        (NetworkConfig("lan", dhcp=True, mcast="192.168.1.1:4789"), "not a multicast"),
        (NetworkConfig("lan", dhcp=True, mcast="239.192.77.1:99999"), "invalid UDP port"),
        (
            NetworkConfig("lan", address="192.168.77.10/24", dhcp=True, mcast="239.192.77.1:4789"),
            "cannot set address",
        ),
        (NetworkConfig("lan", dhcp=False, mcast="239.192.77.1:4789"), "address"),
        (
            NetworkConfig("lan", address="192.168.77.10", mcast="239.192.77.1:4789"),
            "IPv4 CIDR",
        ),
        (
            NetworkConfig("lan", address="192.168.77.0/24", mcast="239.192.77.1:4789"),
            None,
        ),
        (
            NetworkConfig(
                "lan",
                address="192.168.77.10/24",
                gateway="192.168.78.1",
                mcast="239.192.77.1:4789",
            ),
            "must be inside",
        ),
        (
            NetworkConfig("lan", dhcp=True, mac="01:00:00:00:00:01", mcast="239.192.77.1:4789"),
            "must be unicast",
        ),
        (
            NetworkConfig("lan", dhcp=True, mac="bad", mcast="239.192.77.1:4789"),
            "invalid MAC",
        ),
    ],
)
def test_network_validation_errors(network: NetworkConfig, message: str | None) -> None:
    config = Config.default("lab")
    config.networks = [network]
    if message is None:
        config.validate()
        return
    with pytest.raises(ConfigError, match=message):
        config.validate()


def test_duplicate_network_fields_are_rejected() -> None:
    duplicate_name = Config.default("lab")
    duplicate_name.networks = [
        NetworkConfig("lan", dhcp=True, mcast="239.192.77.1:4789"),
        NetworkConfig("lan", dhcp=True, mcast="239.192.77.2:4789"),
    ]
    with pytest.raises(ConfigError, match="duplicate network"):
        duplicate_name.validate()

    duplicate_mcast = Config.default("lab")
    duplicate_mcast.networks = [
        NetworkConfig("lan", dhcp=True, mcast="239.192.77.1:4789"),
        NetworkConfig("back", dhcp=True, mcast="239.192.77.1:4789"),
    ]
    with pytest.raises(ConfigError, match="duplicate multicast"):
        duplicate_mcast.validate()

    duplicate_mac = Config.default("lab")
    duplicate_mac.networks = [
        NetworkConfig("lan", dhcp=True, mac="02:00:00:00:00:01", mcast="239.192.77.1:4789"),
        NetworkConfig("back", dhcp=True, mac="02:00:00:00:00:01", mcast="239.192.77.2:4789"),
    ]
    with pytest.raises(ConfigError, match="duplicate MAC"):
        duplicate_mac.validate()

    duplicate_gateway = Config.default("lab")
    duplicate_gateway.networks = [
        NetworkConfig(
            "lan",
            address="192.168.77.10/24",
            gateway="192.168.77.1",
            mcast="239.192.77.1:4789",
        ),
        NetworkConfig(
            "back",
            address="192.168.78.10/24",
            gateway="192.168.78.1",
            mcast="239.192.77.2:4789",
        ),
    ]
    with pytest.raises(ConfigError, match="only one experimental network"):
        duplicate_gateway.validate()

    duplicate_address = Config.default("lab")
    duplicate_address.networks = [
        NetworkConfig("lan", address="192.168.77.10/24", mcast="239.192.77.1:4789"),
        NetworkConfig("back", address="192.168.77.10/25", mcast="239.192.77.2:4789"),
    ]
    with pytest.raises(ConfigError, match="duplicate address"):
        duplicate_address.validate()
