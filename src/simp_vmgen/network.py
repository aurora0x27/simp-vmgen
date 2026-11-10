"""Resolve configured experimental networks for QEMU and guests."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import Config, derive_mac


@dataclass(frozen=True, slots=True)
class ResolvedNetwork:
    name: str
    mac: str
    address: str
    gateway: str
    dhcp: bool
    mcast: str
    bridge: str
    index: int

    @property
    def qemu_id(self) -> str:
        return f"net{self.index}"


def management_mac(config: Config) -> str:
    return derive_mac(config.project.name, "mgmt")


def resolve_networks(config: Config) -> list[ResolvedNetwork]:
    return [
        ResolvedNetwork(
            name=network.name,
            mac=network.effective_mac(config.project.name),
            address=network.address,
            gateway=network.gateway,
            dhcp=network.dhcp,
            mcast=network.mcast,
            bridge=network.bridge,
            index=index,
        )
        for index, network in enumerate(config.networks, start=1)
    ]


def network_fingerprint(config: Config) -> list[dict[str, object]]:
    return [
        {
            "name": network.name,
            "mac": network.mac,
            "address": network.address,
            "gateway": network.gateway,
            "dhcp": network.dhcp,
            "mcast": network.mcast,
            "bridge": network.bridge,
        }
        for network in resolve_networks(config)
    ]


def cloud_init_network_config(config: Config) -> str:
    networks = resolve_networks(config)
    if not networks:
        return ""
    lines = [
        "version: 2",
        "ethernets:",
        "  mgmt:",
        "    match:",
        f"      macaddress: {json.dumps(management_mac(config))}",
        "    dhcp4: true",
    ]
    for network in networks:
        lines += [
            f"  {network.name}:",
            "    match:",
            f"      macaddress: {json.dumps(network.mac)}",
            f"    dhcp4: {str(network.dhcp).lower()}",
        ]
        if not network.dhcp:
            lines += [
                "    addresses:",
                f"      - {json.dumps(network.address)}",
            ]
            if network.gateway:
                lines += [
                    "    routes:",
                    '      - to: "0.0.0.0/0"',
                    f"        via: {json.dumps(network.gateway)}",
                ]
    return "\n".join(lines) + "\n"
