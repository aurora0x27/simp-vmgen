"""Privileged bridge management for compose projects."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import runtime
from .compose import (
    ComposeConfig,
    ComposeError,
    compose_lock,
    ensure_state_locked,
    load_compose,
    load_state,
    resolve_compose_root,
    save_state,
    state_path,
)
from .config import CONFIG_FILENAME, Config
from .errors import SimpVmgenError
from .layout import ProjectLayout

_COMMON_HELPERS = (
    "/usr/libexec/qemu-bridge-helper",
    "/usr/lib/qemu/qemu-bridge-helper",
    "/usr/local/libexec/qemu-bridge-helper",
)


def bridge_helper() -> str | None:
    found = shutil.which("qemu-bridge-helper")
    if found:
        return found
    for candidate in _COMMON_HELPERS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def bridge_exists(name: str) -> bool:
    if shutil.which("ip") is None:
        return False
    try:
        result = subprocess.run(
            ["ip", "link", "show", "dev", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _require_bridge_type(name: str) -> None:
    try:
        result = subprocess.run(
            ["ip", "-d", "link", "show", "dev", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ComposeError(f"cannot inspect bridge {name!r}: {exc}") from exc
    if result.returncode != 0:
        raise ComposeError(f"cannot inspect bridge {name!r}: {result.stderr.strip()}")
    if "bridge" not in result.stdout.split():
        raise ComposeError(f"network device {name!r} exists but is not a bridge")


def _run_ip(argv: list[str], what: str) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ComposeError(f"{what} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ComposeError(f"{what} failed: {detail}")


def _require_privilege(action: str) -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise ComposeError(
            f"compose network {action} needs root or CAP_NET_ADMIN; "
            f"run it with sudo, for example: sudo simp-vmgen compose network {action}"
        )


def _layout_config(service_root: Path) -> Config:
    try:
        return Config.load(service_root / CONFIG_FILENAME)
    except SimpVmgenError:
        return Config.default(service_root.name)


def _running_services(state: Any) -> list[str]:
    running: list[str] = []
    for name, root_text in getattr(state, "service_roots", {}).items():
        service_root = Path(root_text)
        if (
            runtime.running_pid(ProjectLayout(service_root, _layout_config(service_root)))
            is not None
        ):
            running.append(name)
    return running


def _bridge_networks(compose: ComposeConfig) -> list[str]:
    return [name for name, network in compose.networks.items() if network.driver == "bridge"]


def compose_network_prepare(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    if not _bridge_networks(compose):
        print("no bridge networks in this compose project")
        return 0
    _require_privilege("prepare")
    with compose_lock(root):
        previous = load_state(root)
        previous_owned = set(previous.owned_bridges) if previous is not None else set()
        state = ensure_state_locked(compose)
        created: list[str] = []
        try:
            for name, network in compose.networks.items():
                if network.driver != "bridge":
                    continue
                bridge = state.bridges[name]
                exists = bridge_exists(bridge)
                if exists:
                    _require_bridge_type(bridge)
                    if not network.bridge and bridge not in previous_owned:
                        raise ComposeError(
                            f"bridge {bridge!r} already exists but is not owned by this "
                            "compose project; set bridge = ... to use it explicitly"
                        )
                    continue
                if network.bridge:
                    raise ComposeError(f"external bridge {bridge!r} does not exist")
                _run_ip(
                    ["ip", "link", "add", "name", bridge, "type", "bridge"],
                    f"cannot create bridge {bridge!r}",
                )
                created.append(bridge)
                _run_ip(
                    ["ip", "link", "set", "dev", bridge, "up"],
                    f"cannot bring bridge {bridge!r} up",
                )
            save_state(root, state)
        except Exception:
            for bridge in reversed(created):
                with contextlib.suppress(ComposeError):
                    _run_ip(["ip", "link", "delete", "dev", bridge], "rollback")
            if previous is None:
                state_path(root).unlink(missing_ok=True)
            else:
                save_state(root, previous)
            raise
    print(f"prepared {len(_bridge_networks(compose))} bridge network(s) for {compose.project.name}")
    return 0


def compose_network_cleanup(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    _require_privilege("cleanup")
    with compose_lock(root):
        state = load_state(root)
        if state is None:
            print("no compose network state")
            return 0
        running = _running_services(state)
        if running:
            raise ComposeError(
                "services are still running: "
                + ", ".join(sorted(running))
                + "; run `simp-vmgen compose down` first"
            )
        removed: set[str] = set()
        for bridge in list(state.owned_bridges):
            if bridge_exists(bridge):
                _run_ip(["ip", "link", "delete", "dev", bridge], f"cannot delete bridge {bridge!r}")
            removed.add(bridge)
        state.owned_bridges = [bridge for bridge in state.owned_bridges if bridge not in removed]
        state.bridges = {
            name: bridge for name, bridge in state.bridges.items() if bridge not in removed
        }
        if state.owned_bridges or state.networks or state.services:
            save_state(root, state)
        else:
            state_path(root).unlink(missing_ok=True)
    print(f"removed {len(removed)} owned bridge(s)")
    return 0


def compose_network_status(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    load_error: ComposeError | None = None
    try:
        compose: ComposeConfig | None = load_compose(root)
    except ComposeError as exc:
        compose = None
        load_error = exc
    state = load_state(root)
    if compose is None and state is None:
        assert load_error is not None
        raise load_error
    data: dict[str, Any] = {
        "root": str(root),
        "privileged": not hasattr(os, "geteuid") or os.geteuid() == 0,
        "ip": shutil.which("ip") is not None,
        "tun": Path("/dev/net/tun").exists(),
        "bridge_helper": bridge_helper(),
        "networks": {},
        "owned_bridges": [],
    }
    owned = set(state.owned_bridges) if state is not None else set()
    if compose is not None:
        for name, network in compose.networks.items():
            if network.driver != "bridge":
                continue
            bridge = state.bridges.get(name) if state is not None else None
            data["networks"][name] = {
                "bridge": bridge,
                "owned": bridge in owned if bridge else False,
                "exists": bridge_exists(bridge) if bridge else False,
                "external": bool(network.bridge),
            }
    data["owned_bridges"] = [
        {"name": bridge, "exists": bridge_exists(bridge)} for bridge in sorted(owned)
    ]
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0
    print(f"compose : {root}")
    print(f"root    : {'yes' if data['privileged'] else 'no'}")
    print(f"ip      : {'yes' if data['ip'] else 'no'}")
    print(f"tun     : {'yes' if data['tun'] else 'no'}")
    print(f"helper  : {data['bridge_helper'] or '(not found)'}")
    for name, item in data["networks"].items():
        print(
            f"network {name}: bridge={item['bridge']} exists={item['exists']} "
            f"owned={item['owned']} external={item['external']}"
        )
    for item in data["owned_bridges"]:
        print(f"owned   {item['name']}: exists={item['exists']}")
    return 0


def compose_network_dispatch(args: Any) -> int:
    command = getattr(args, "network_command", None) or ""
    handlers = {
        "prepare": compose_network_prepare,
        "cleanup": compose_network_cleanup,
        "status": compose_network_status,
    }
    handler = handlers.get(command)
    if handler is None:
        raise ComposeError("compose network requires a subcommand")
    return handler(args)
