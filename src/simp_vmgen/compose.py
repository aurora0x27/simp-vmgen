"""Compose projects: service topology, socket endpoints and shared state."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import cache_dir, file_lock
from .config import (
    CONFIG_FILENAME,
    Config,
    NetworkConfig,
    derive_mac,
    normalize_ipv4_interface,
    normalize_mac,
    normalize_multicast_endpoint,
    normalize_network_name,
)
from .distros import load_distro
from .errors import ComposeError, ConfigError

COMPOSE_FILENAME = "vmgen-compose.toml"
COMPOSE_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
COMPOSE_DIR = Path(".simp-vmgen") / "compose"
STATE_FILENAME = "state.json"
LOCK_FILENAME = "lock"
MEMBER_FILENAME = Path(".simp-vmgen") / "compose-member.json"

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_COMPOSE_DRIVERS = {"socket", "bridge"}


@dataclass(slots=True)
class ComposeProject:
    name: str
    description: str = ""


@dataclass(slots=True)
class ComposeNetwork:
    name: str
    subnet: str
    driver: str = "socket"
    mcast: str = ""
    bridge: str = ""


@dataclass(slots=True)
class ServiceNetwork:
    network: str
    address: str
    mac: str


@dataclass(slots=True)
class ComposeService:
    name: str
    project: str
    root: Path
    config: Config
    networks: dict[str, ServiceNetwork] = field(default_factory=dict)


@dataclass(slots=True)
class ComposeConfig:
    schema: int
    project: ComposeProject
    networks: dict[str, ComposeNetwork] = field(default_factory=dict)
    services: dict[str, ComposeService] = field(default_factory=dict)
    root: Path = field(default_factory=Path.cwd)


@dataclass(slots=True)
class ComposeState:
    schema: int = STATE_SCHEMA_VERSION
    project: str = ""
    root: str = ""
    networks: dict[str, str] = field(default_factory=dict)
    services: list[str] = field(default_factory=list)
    service_roots: dict[str, str] = field(default_factory=dict)
    bridges: dict[str, str] = field(default_factory=dict)
    owned_bridges: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _EndpointRegistry:
    schema: int = REGISTRY_SCHEMA_VERSION
    endpoints: dict[str, str] = field(default_factory=dict)


def resolve_compose_root(directory: str | None = None, *, allow_state: bool = False) -> Path:
    current = Path(directory).expanduser() if directory else Path.cwd()
    if current.is_file():
        current = current.parent
    current = current.resolve()
    for candidate in (current, *current.parents):
        if (candidate / COMPOSE_FILENAME).is_file():
            return candidate
        if allow_state and (candidate / COMPOSE_DIR / STATE_FILENAME).is_file():
            return candidate
    raise ComposeError(f"no {COMPOSE_FILENAME} found in {current} or any parent directory")


def load_compose(root: Path) -> ComposeConfig:
    root = root.resolve()
    path = root / COMPOSE_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ComposeError(f"cannot read {path}: {exc}") from exc
    return loads_compose(text, source=str(path), root=root)


def loads_compose(text: str, *, source: str = COMPOSE_FILENAME, root: Path) -> ComposeConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ComposeError(f"{source}: invalid TOML: {exc}") from exc
    try:
        return _from_dict(data, source=source, root=root)
    except ConfigError as exc:
        message = str(exc)
        prefix = f"{source}."
        if message.startswith(prefix):
            message = f"{Path(source).name}: {message[len(prefix) :]}"
        raise ComposeError(message) from exc


def _from_dict(data: dict[str, Any], *, source: str, root: Path) -> ComposeConfig:
    _unknown_keys(data, {"schema", "project", "networks", "services"}, source)
    schema = data.get("schema", COMPOSE_SCHEMA_VERSION)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ComposeError(f"{source}.schema: expected an integer")
    if schema != COMPOSE_SCHEMA_VERSION:
        raise ComposeError(
            f"{source}.schema: unsupported schema {schema} (expected {COMPOSE_SCHEMA_VERSION})"
        )

    project_table = _table(data.get("project"), f"{source}.project")
    _unknown_keys(project_table, {"name", "description"}, f"{source}.project")
    name = _string(project_table, "name", f"{source}.project", required=True)
    if _PROJECT_NAME_RE.match(name) is None:
        raise ComposeError(f"{source}.project.name: invalid name {name!r}")
    project = ComposeProject(
        name=name,
        description=_string(project_table, "description", f"{source}.project"),
    )

    networks_table = _table(data.get("networks"), f"{source}.networks")
    networks: dict[str, ComposeNetwork] = {}
    for raw_name, raw in networks_table.items():
        where = f"{source}.networks.{raw_name}"
        if not isinstance(raw, dict):
            raise ComposeError(f"{where}: expected a table")
        network_name = normalize_network_name(raw_name, where)
        _unknown_keys(raw, {"driver", "subnet", "mcast", "bridge"}, where)
        driver = _string(raw, "driver", where, default="bridge" if "bridge" in raw else "socket")
        if driver not in _COMPOSE_DRIVERS:
            raise ComposeError(
                f"{where}.driver: expected one of {', '.join(sorted(_COMPOSE_DRIVERS))}"
            )
        subnet = _ipv4_subnet(_string(raw, "subnet", where, required=True), f"{where}.subnet")
        mcast = _string(raw, "mcast", where)
        bridge = _string(raw, "bridge", where)
        if driver == "socket":
            if bridge:
                raise ComposeError(f'{where}.bridge: only valid with driver = "bridge"')
            if mcast:
                mcast = normalize_multicast_endpoint(mcast, f"{where}.mcast")
        else:
            if mcast:
                raise ComposeError(f'{where}.mcast: only valid with driver = "socket"')
            if bridge:
                bridge = normalize_network_name(bridge, f"{where}.bridge")
        networks[network_name] = ComposeNetwork(
            name=network_name,
            subnet=subnet,
            driver=driver,
            mcast=mcast,
            bridge=bridge,
        )

    services_table = _table(data.get("services"), f"{source}.services")
    services: dict[str, ComposeService] = {}
    for raw_name, raw in services_table.items():
        where = f"{source}.services.{raw_name}"
        if _SERVICE_NAME_RE.match(raw_name) is None:
            raise ComposeError(f"{where}: invalid service name {raw_name!r}")
        if not isinstance(raw, dict):
            raise ComposeError(f"{where}: expected a table")
        _unknown_keys(raw, {"project", "networks"}, where)
        project_path = _string(raw, "project", where, required=True)
        service_root = (root / project_path).expanduser().resolve()
        if not service_root.is_dir():
            raise ComposeError(f"{where}.project: directory does not exist: {service_root}")
        config_path = service_root / CONFIG_FILENAME
        if not config_path.is_file():
            raise ComposeError(f"{where}.project: no {CONFIG_FILENAME} in {service_root}")
        config = Config.load(config_path)
        config.validate()
        load_distro(config.distro.name).release(config.distro.release)
        if config.networks:
            raise ComposeError(
                f"{where}.project: {config_path} already defines [[network]]; "
                "compose manages service networks"
            )
        bindings: dict[str, ServiceNetwork] = {}
        bindings_table = _table(raw.get("networks"), f"{where}.networks")
        for network_name, binding in bindings_table.items():
            binding_where = f"{where}.networks.{network_name}"
            if network_name not in networks:
                raise ComposeError(f"{binding_where}: unknown network {network_name!r}")
            if not isinstance(binding, dict):
                raise ComposeError(f"{binding_where}: expected a table")
            _unknown_keys(binding, {"address", "mac"}, binding_where)
            address = normalize_ipv4_interface(
                _string(binding, "address", binding_where, required=True),
                f"{binding_where}.address",
            )
            network = ipaddress.IPv4Network(networks[network_name].subnet)
            interface = ipaddress.IPv4Interface(address)
            if interface.ip not in network:
                raise ComposeError(f"{binding_where}.address: {interface.ip} is outside {network}")
            if interface.network != network:
                raise ComposeError(
                    f"{binding_where}.address: prefix must match {network} "
                    "(for example 192.168.77.10/24)"
                )
            if interface.ip in (network.network_address, network.broadcast_address):
                raise ComposeError(
                    f"{binding_where}.address: cannot use the network or broadcast address"
                )
            mac = _string(binding, "mac", binding_where)
            if mac:
                mac = normalize_mac(mac, f"{binding_where}.mac")
            else:
                mac = derive_mac(config.project.name, network_name)
            bindings[network_name] = ServiceNetwork(
                network=network_name,
                address=address,
                mac=mac,
            )
        services[raw_name] = ComposeService(
            name=raw_name,
            project=project_path,
            root=service_root,
            config=config,
            networks=bindings,
        )

    compose = ComposeConfig(
        schema=schema,
        project=project,
        networks=networks,
        services=services,
        root=root,
    )
    _validate(compose, source=source)
    return compose


def _validate(compose: ComposeConfig, *, source: str) -> None:
    if not compose.services:
        raise ComposeError(f"{source}: at least one service is required")
    subnets = list(compose.networks.values())
    for index, network in enumerate(subnets):
        for other in subnets[index + 1 :]:
            if ipaddress.IPv4Network(network.subnet).overlaps(ipaddress.IPv4Network(other.subnet)):
                raise ComposeError(
                    f"{source}.networks: {network.name!r} and {other.name!r} "
                    "have overlapping subnets"
                )
    seen_roots: dict[Path, str] = {}
    seen_projects: dict[str, str] = {}
    seen_addresses: dict[tuple[str, str], str] = {}
    seen_macs: dict[str, str] = {}
    seen_ports: dict[int, str] = {}
    seen_mcasts: dict[str, str] = {}
    seen_bridges: dict[str, str] = {}
    for network in compose.networks.values():
        if network.mcast:
            if network.mcast in seen_mcasts:
                raise ComposeError(
                    f"{source}.networks: duplicate multicast endpoint {network.mcast!r}"
                )
            seen_mcasts[network.mcast] = network.name
        if network.bridge:
            if network.bridge in seen_bridges:
                raise ComposeError(f"{source}.networks: duplicate bridge {network.bridge!r}")
            seen_bridges[network.bridge] = network.name
    for service in compose.services.values():
        if service.root in seen_roots:
            raise ComposeError(
                f"{source}.services: {service.name!r} and {seen_roots[service.root]!r} "
                "reference the same project"
            )
        seen_roots[service.root] = service.name
        project_name = service.config.project.name
        if project_name in seen_projects:
            raise ComposeError(
                f"{source}.services: {service.name!r} and {seen_projects[project_name]!r} "
                f"use the same project name {project_name!r}"
            )
        seen_projects[project_name] = service.name
        for port_name, port in (
            ("ssh.port", service.config.ssh.port),
            ("gdb.port", service.config.gdb.port if service.config.gdb.enabled else "auto"),
        ):
            if isinstance(port, int):
                if port in seen_ports:
                    raise ComposeError(
                        f"{source}.services.{service.name}: {port_name} {port} is also "
                        f"used by {seen_ports[port]!r}"
                    )
                seen_ports[port] = service.name
        for binding in service.networks.values():
            host = str(ipaddress.IPv4Interface(binding.address).ip)
            key = (binding.network, host)
            if key in seen_addresses:
                raise ComposeError(
                    f"{source}.services: {service.name!r} and "
                    f"{seen_addresses[key]!r} share {host} on {binding.network!r}"
                )
            seen_addresses[key] = service.name
            if binding.mac in seen_macs:
                raise ComposeError(
                    f"{source}.services: {service.name!r} and {seen_macs[binding.mac]!r} "
                    f"share MAC {binding.mac!r}"
                )
            seen_macs[binding.mac] = service.name


def materialize_service(
    compose: ComposeConfig,
    service_name: str,
    endpoints: dict[str, str],
    bridges: dict[str, str] | None = None,
) -> Config:
    service = compose.services[service_name]
    bridges = bridges or {}
    missing = {
        name
        for name in service.networks
        if compose.networks[name].driver == "bridge"
        and name not in bridges
        or compose.networks[name].driver == "socket"
        and name not in endpoints
    }
    if missing:
        raise ComposeError(f"missing compose endpoints for {', '.join(sorted(missing))}")
    config = Config.load(service.root / CONFIG_FILENAME)
    config.networks = [
        NetworkConfig(
            name=binding.network,
            address=binding.address,
            mac=binding.mac,
            mcast=(
                endpoints[binding.network]
                if compose.networks[binding.network].driver == "socket"
                else ""
            ),
            bridge=(
                bridges[binding.network]
                if compose.networks[binding.network].driver == "bridge"
                else ""
            ),
        )
        for binding in service.networks.values()
    ]
    config.validate()
    return config


def state_path(root: Path) -> Path:
    return root.resolve() / COMPOSE_DIR / STATE_FILENAME


def load_state(root: Path) -> ComposeState | None:
    path = state_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ComposeError(f"cannot read compose state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ComposeError(f"invalid compose state {path}")
    schema = data.get("schema")
    if schema != STATE_SCHEMA_VERSION:
        raise ComposeError(
            f"{path}: unsupported compose state schema {schema!r} (expected {STATE_SCHEMA_VERSION})"
        )
    networks = data.get("networks", {})
    if not isinstance(networks, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in networks.items()
    ):
        raise ComposeError(f"invalid compose state networks in {path}")
    services = data.get("services", [])
    if not isinstance(services, list) or not all(isinstance(item, str) for item in services):
        raise ComposeError(f"invalid compose state services in {path}")
    service_roots = data.get("service_roots", {})
    if not isinstance(service_roots, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in service_roots.items()
    ):
        raise ComposeError(f"invalid compose state service roots in {path}")
    bridges = data.get("bridges", {})
    if not isinstance(bridges, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in bridges.items()
    ):
        raise ComposeError(f"invalid compose state bridges in {path}")
    owned_bridges = data.get("owned_bridges", [])
    if not isinstance(owned_bridges, list) or not all(
        isinstance(item, str) for item in owned_bridges
    ):
        raise ComposeError(f"invalid compose state owned bridges in {path}")
    return ComposeState(
        schema=STATE_SCHEMA_VERSION,
        project=str(data.get("project", "")),
        root=str(data.get("root", "")),
        networks=dict(networks),
        services=list(services),
        service_roots=dict(service_roots),
        bridges=dict(bridges),
        owned_bridges=list(owned_bridges),
    )


def save_state(root: Path, state: ComposeState) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(
            json.dumps(
                {
                    "schema": state.schema,
                    "project": state.project,
                    "root": state.root,
                    "networks": dict(sorted(state.networks.items())),
                    "services": sorted(state.services),
                    "service_roots": dict(sorted(state.service_roots.items())),
                    "bridges": dict(sorted(state.bridges.items())),
                    "owned_bridges": sorted(state.owned_bridges),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        raise ComposeError(f"cannot write compose state {path}: {exc}") from exc


@contextmanager
def compose_lock(root: Path) -> Iterator[None]:
    directory = root.resolve() / COMPOSE_DIR
    try:
        with file_lock(directory / LOCK_FILENAME):
            yield
    except OSError as exc:
        raise ComposeError(
            f"cannot lock compose state in {directory}: {exc}; "
            "check directory ownership/permissions (a privileged prepare may have "
            "created it as root)"
        ) from exc


@contextmanager
def _registry_lock() -> Iterator[None]:
    try:
        with file_lock(_registry_lock_path()):
            yield
    except OSError as exc:
        raise ComposeError(f"cannot lock compose endpoint registry: {exc}") from exc


def fix_compose_ownership(root: Path) -> None:
    """Give sudo-created state files back to the invoking user."""
    if (
        not hasattr(os, "geteuid")
        or os.geteuid() != 0
        or not hasattr(os, "getuid")
        or os.getuid() != 0
    ):
        return
    raw_uid = os.environ.get("SUDO_UID")
    raw_gid = os.environ.get("SUDO_GID")
    if not raw_uid or not raw_gid:
        return
    try:
        uid, gid = int(raw_uid), int(raw_gid)
    except ValueError:
        return
    paths = (
        root.resolve() / ".simp-vmgen",
        root.resolve() / COMPOSE_DIR,
        root.resolve() / COMPOSE_DIR / LOCK_FILENAME,
        state_path(root),
    )
    for path in paths:
        if not path.exists():
            continue
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            raise ComposeError(f"cannot restore ownership of {path}: {exc}") from exc


def ensure_state(compose: ComposeConfig) -> ComposeState:
    with compose_lock(compose.root):
        return ensure_state_locked(compose)


def ensure_state_locked(compose: ComposeConfig, *, allocate_socket: bool = True) -> ComposeState:
    root = compose.root.resolve()
    state = load_state(root) or ComposeState()
    state.project = compose.project.name
    state.root = str(root)
    endpoints: dict[str, str] = dict(state.networks)
    bridges: dict[str, str] = {}
    owned_bridges = set(state.owned_bridges)
    if allocate_socket:
        with _registry_lock():
            registry = _load_registry()
            _prune_registry(registry)
            endpoints = {}
            for name, network in compose.networks.items():
                if network.driver == "bridge":
                    continue
                if network.mcast:
                    endpoint = network.mcast
                    _claim(registry, endpoint, root)
                elif name in state.networks:
                    endpoint = state.networks[name]
                    _claim(registry, endpoint, root)
                else:
                    endpoint = _allocate(registry, root, name)
                endpoints[name] = endpoint
            for endpoint in state.networks.values():
                if endpoint not in endpoints.values():
                    _release(registry, root, endpoint)
            _save_registry(registry)
    for name, network in compose.networks.items():
        if network.driver != "bridge":
            continue
        previous_bridge = state.bridges.get(name)
        if network.bridge:
            bridge = network.bridge
            owned_bridges.discard(bridge)
        elif previous_bridge and previous_bridge in owned_bridges:
            bridge = previous_bridge
        else:
            bridge = derive_bridge_name(root, name)
            owned_bridges.add(bridge)
        bridges[name] = bridge
    state.networks = endpoints
    state.bridges = bridges
    state.owned_bridges = sorted(owned_bridges)
    state.services = sorted(compose.services)
    state.service_roots = {name: str(service.root) for name, service in compose.services.items()}
    save_state(root, state)
    return state


def release_state(root: Path) -> None:
    with compose_lock(root):
        release_state_locked(root)


def release_state_locked(root: Path) -> None:
    root = root.resolve()
    state = load_state(root)
    if state is None:
        return
    with _registry_lock():
        registry = _load_registry()
        _prune_registry(registry)
        for endpoint in state.networks.values():
            _release(registry, root, endpoint)
        _save_registry(registry)
    state.networks = {}
    state.services = []
    state.service_roots = {}
    if state.owned_bridges:
        save_state(root, state)
    else:
        delete_state(root)


def delete_state(root: Path) -> None:
    try:
        state_path(root).unlink(missing_ok=True)
    except OSError as exc:
        raise ComposeError(f"cannot remove compose state {state_path(root)}: {exc}") from exc


def derive_bridge_name(root: Path, network_name: str) -> str:
    digest = hashlib.sha256(f"{root.resolve()}\0{network_name}\0bridge".encode()).hexdigest()
    return f"svbr{digest[:8]}"


def service_status(
    compose: ComposeConfig, service_name: str, state: ComposeState
) -> dict[str, object]:
    service = compose.services[service_name]
    networks = {
        name: {
            "address": binding.address,
            "mac": binding.mac,
            "mcast": state.networks.get(name),
            "bridge": state.bridges.get(name),
        }
        for name, binding in service.networks.items()
    }
    return {
        "name": service.name,
        "project": service.project,
        "root": str(service.root),
        "networks": networks,
    }


def show_data(compose: ComposeConfig, state: ComposeState) -> dict[str, object]:
    return {
        "root": str(compose.root),
        "config_path": str(compose.root / COMPOSE_FILENAME),
        "project": {
            "name": compose.project.name,
            "description": compose.project.description,
        },
        "networks": {
            name: {
                "driver": network.driver,
                "subnet": network.subnet,
                "mcast": state.networks.get(name),
                "bridge": state.bridges.get(name),
            }
            for name, network in compose.networks.items()
        },
        "services": {name: service_status(compose, name, state) for name in compose.services},
    }


def print_show(compose: ComposeConfig, state: ComposeState, *, json_output: bool) -> None:
    data = show_data(compose, state)
    if json_output:
        print(json.dumps(data, indent=2))
        return
    print(f"compose : {data['root']}")
    print(f"config  : {data['config_path']}")
    print(f"project : {compose.project.name}")
    print()
    print("networks:")
    for name, network in compose.networks.items():
        endpoint = (
            f"bridge={state.bridges[name]}"
            if network.driver == "bridge"
            else f"mcast={state.networks[name]}"
        )
        print(f"  {name}: {network.driver} {network.subnet} {endpoint}")
    print()
    print("services:")
    for name, service in compose.services.items():
        print(f"  {name}: {service.project} ({service.root})")
        for network_name, binding in service.networks.items():
            network = compose.networks[network_name]
            endpoint = (
                f"bridge={state.bridges[network_name]}"
                if network.driver == "bridge"
                else f"mcast={state.networks[network_name]}"
            )
            print(f"    {network_name}: {binding.address} mac={binding.mac} {endpoint}")


def compose_validate(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    print(
        f"{root}: compose project {compose.project.name!r} is valid "
        f"({len(compose.services)} service(s), {len(compose.networks)} network(s))"
    )
    return 0


def compose_show(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    state = ensure_state(compose)
    print_show(compose, state, json_output=getattr(args, "json", False))
    return 0


def member_marker(root: Path) -> Path:
    return root.resolve() / MEMBER_FILENAME


def read_member(root: Path) -> dict[str, object] | None:
    path = member_marker(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ComposeError(f"cannot read compose membership marker {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ComposeError(f"invalid compose membership marker {path}")
    return data


def write_member(root: Path, compose_root: Path, service: str) -> None:
    path = member_marker(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"compose_root": str(compose_root.resolve()), "service": service},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ComposeError(f"cannot write compose membership marker {path}: {exc}") from exc


def clear_member(root: Path) -> None:
    path = member_marker(root)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ComposeError(f"cannot remove compose membership marker {path}: {exc}") from exc


def assert_standalone_allowed(root: Path) -> None:
    if os.environ.get("SIMP_VMGEN_COMPOSE_ACTIVE") == "1":
        return
    marker = member_marker(root)
    if marker.is_file():
        raise ComposeError(
            f"{root} is managed by compose ({marker}); use `simp-vmgen compose ...` "
            "instead of running it standalone"
        )


def _registry_path() -> Path:
    return cache_dir() / "compose" / "endpoints.json"


def _registry_lock_path() -> Path:
    return cache_dir() / "compose" / "endpoints.lock"


def _load_registry() -> _EndpointRegistry:
    path = _registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _EndpointRegistry()
    except (OSError, json.JSONDecodeError) as exc:
        raise ComposeError(f"cannot read compose endpoint registry {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("endpoints", {}), dict):
        raise ComposeError(f"invalid compose endpoint registry {path}")
    schema = data.get("schema")
    if schema != REGISTRY_SCHEMA_VERSION:
        raise ComposeError(
            f"{path}: unsupported compose endpoint registry schema {schema!r} "
            f"(expected {REGISTRY_SCHEMA_VERSION})"
        )
    endpoints = data.get("endpoints", {})
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in endpoints.items()):
        raise ComposeError(f"invalid compose endpoint registry {path}")
    return _EndpointRegistry(endpoints=dict(endpoints))


def _save_registry(registry: _EndpointRegistry) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(
            json.dumps(
                {
                    "schema": registry.schema,
                    "endpoints": dict(sorted(registry.endpoints.items())),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        raise ComposeError(f"cannot write compose endpoint registry {path}: {exc}") from exc


def _prune_registry(registry: _EndpointRegistry) -> None:
    for endpoint, owner in list(registry.endpoints.items()):
        root = Path(owner)
        try:
            state = load_state(root)
        except ComposeError:
            state = None
        if state is None or endpoint not in state.networks.values():
            del registry.endpoints[endpoint]


def _claim(registry: _EndpointRegistry, endpoint: str, root: Path) -> None:
    owner = registry.endpoints.get(endpoint)
    if owner is not None and Path(owner).resolve() != root.resolve():
        raise ComposeError(f"multicast endpoint {endpoint} is already used by {owner}")
    registry.endpoints[endpoint] = str(root.resolve())


def _release(registry: _EndpointRegistry, root: Path, endpoint: str) -> None:
    if registry.endpoints.get(endpoint) == str(root.resolve()):
        del registry.endpoints[endpoint]


def _allocate(registry: _EndpointRegistry, root: Path, network_name: str) -> str:
    digest = hashlib.sha256(f"{root.resolve()}\0{network_name}".encode()).digest()
    group = f"239.192.{digest[0] % 254 + 1}.{digest[1] % 254 + 1}"
    start = 20000 + int.from_bytes(digest[2:4], "big") % 10000
    for offset in range(10000):
        endpoint = f"{group}:{20000 + ((start - 20000 + offset) % 10000)}"
        if endpoint not in registry.endpoints:
            registry.endpoints[endpoint] = str(root.resolve())
            return endpoint
    raise ComposeError("cannot allocate a free multicast endpoint")


def _ipv4_subnet(value: str, where: str) -> str:
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except ValueError as exc:
        raise ComposeError(f"{where}: invalid IPv4 subnet {value!r}") from exc
    if network.prefixlen > 30:
        raise ComposeError(f"{where}: subnet must have at least two host addresses")
    if network.is_loopback or network.is_multicast or network.is_unspecified:
        raise ComposeError(f"{where}: subnet must be a unicast network")
    return str(network)


def _table(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComposeError(f"{where}: expected a table")
    return value


def _unknown_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ComposeError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _string(
    table: dict[str, Any], key: str, where: str, *, default: str = "", required: bool = False
) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ComposeError(f"{where}.{key}: expected a string")
    if required and not value.strip():
        raise ComposeError(f"{where}.{key}: must not be empty")
    return value
