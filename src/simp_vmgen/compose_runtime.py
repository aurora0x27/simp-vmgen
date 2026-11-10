"""Compose lifecycle: orchestrate existing single-VM runtime functions."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import runtime
from .compose import (
    ComposeConfig,
    ComposeError,
    ComposeState,
    clear_member,
    compose_lock,
    compose_show,
    compose_validate,
    ensure_state_locked,
    load_compose,
    load_state,
    materialize_service,
    release_state_locked,
    resolve_compose_root,
    write_member,
)
from .compose_network import bridge_exists, compose_network_dispatch
from .config import CONFIG_FILENAME, Config
from .errors import SimpVmgenError
from .layout import ProjectLayout


def _args(root: Path, config: Config, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(directory=str(root), config_override=config, **kwargs)


def _plain_config(service_root: Path) -> Config:
    return Config.load(service_root / CONFIG_FILENAME)


def _layout_config(service_root: Path) -> Config:
    """Config used only for runtime paths; tolerate a broken service file."""
    try:
        return Config.load(service_root / CONFIG_FILENAME)
    except SimpVmgenError:
        return Config.default(service_root.name)


def _service_config(
    compose: ComposeConfig, service_name: str, state: ComposeState | None
) -> Config:
    service = compose.services[service_name]
    if state is not None and all(name in state.networks for name in service.networks):
        return materialize_service(compose, service_name, state.networks, state.bridges)
    return _plain_config(service.root)


def _running(service_root: Path, config: Config) -> bool:
    return runtime.running_pid(ProjectLayout(service_root, config)) is not None


@contextlib.contextmanager
def _compose_active() -> Any:
    previous = os.environ.get("SIMP_VMGEN_COMPOSE_ACTIVE")
    os.environ["SIMP_VMGEN_COMPOSE_ACTIVE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SIMP_VMGEN_COMPOSE_ACTIVE", None)
        else:
            os.environ["SIMP_VMGEN_COMPOSE_ACTIVE"] = previous


def _print_error(name: str, exc: Exception) -> None:
    print(f"{name}: {exc}", file=sys.stderr)


def _status_summary(data: dict[str, dict[str, Any]]) -> str:
    running = sum(1 for item in data.values() if item["state"] == "running")
    if running == len(data):
        return "running"
    if running == 0:
        return "stopped"
    return "partial"


def compose_build(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    failures = 0
    with compose_lock(root):
        state = ensure_state_locked(compose)
        for name, service in compose.services.items():
            print(f"==> build {name}")
            config = materialize_service(compose, name, state.networks, state.bridges)
            try:
                result = runtime.build(
                    _args(service.root, config, dry_run=False, force=getattr(args, "force", False))
                )
            except SimpVmgenError as exc:
                _print_error(name, exc)
                failures += 1
                continue
            if result != 0:
                failures += 1
    return 1 if failures else 0


def _cleanup_started(
    compose: ComposeConfig,
    configs: dict[str, Config],
    preexisting: set[str],
) -> None:
    with _compose_active():
        for name, service in compose.services.items():
            if name in preexisting:
                continue
            config = configs[name]
            if _running(service.root, config):
                with (
                    contextlib.suppress(SimpVmgenError),
                    contextlib.redirect_stdout(sys.stderr),
                ):
                    runtime.stop(_args(service.root, config))
            with contextlib.suppress(SimpVmgenError):
                clear_member(service.root)


def compose_up(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    timeout = getattr(args, "timeout", None)
    no_build = getattr(args, "no_build", False)
    json_output = getattr(args, "json", False)

    with compose_lock(root):
        previous = load_state(root)
        previous_owned = set(previous.owned_bridges) if previous is not None else set()
        state = ensure_state_locked(compose)
        missing_bridges: list[str] = []
        unprepared_bridges: list[str] = []
        for name, network in compose.networks.items():
            if network.driver != "bridge":
                continue
            bridge = state.bridges[name]
            if not bridge_exists(bridge):
                missing_bridges.append(bridge)
            elif not network.bridge and bridge not in previous_owned:
                unprepared_bridges.append(bridge)
        if missing_bridges:
            raise ComposeError(
                "missing bridge(s): "
                + ", ".join(missing_bridges)
                + "; run `sudo simp-vmgen compose network prepare` first"
            )
        if unprepared_bridges:
            raise ComposeError(
                "bridge(s) are not owned by this compose project: "
                + ", ".join(unprepared_bridges)
                + "; run `sudo simp-vmgen compose network prepare` first"
            )
        configs = {
            name: materialize_service(compose, name, state.networks, state.bridges)
            for name in compose.services
        }
        preexisting = {
            name
            for name, config in configs.items()
            if _running(compose.services[name].root, config)
        }
        for name, service in compose.services.items():
            write_member(service.root, root, name)

        failures = 0
        try:
            for name, service in compose.services.items():
                print(f"==> up {name}", file=sys.stderr if json_output else sys.stdout)
                try:
                    with _compose_active():
                        output = (
                            contextlib.redirect_stdout(sys.stderr)
                            if json_output
                            else contextlib.nullcontext()
                        )
                        with output:
                            result = runtime.up(
                                _args(
                                    service.root,
                                    configs[name],
                                    no_build=no_build,
                                    timeout=timeout,
                                    json=False,
                                )
                            )
                except SimpVmgenError as exc:
                    _print_error(name, exc)
                    failures += 1
                    break
                if result != 0:
                    failures += 1
                    break
        except KeyboardInterrupt:
            _cleanup_started(compose, configs, preexisting)
            raise

        if failures:
            _cleanup_started(compose, configs, preexisting)
            return 1

    data = {
        name: runtime.status_data(compose.services[name].root, config)
        for name, config in configs.items()
    }
    if json_output:
        print(
            json.dumps(
                {
                    "project": compose.project.name,
                    "state": _status_summary(data),
                    "networks": state.networks,
                    "services": data,
                },
                indent=2,
            )
        )
    else:
        print(f"{compose.project.name} is up")
        for name, item in data.items():
            print(f"  {name}: {item['state']} pid={item['pid']}")
    return 0


def compose_status(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    state = load_state(root)
    data = {
        name: runtime.status_data(service.root, _service_config(compose, name, state))
        for name, service in compose.services.items()
    }
    summary = _status_summary(data)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "project": compose.project.name,
                    "state": summary,
                    "networks": state.networks if state is not None else {},
                    "services": data,
                },
                indent=2,
            )
        )
    else:
        print(f"project {compose.project.name}: {summary}")
        for name, item in data.items():
            if item["state"] == "stopped":
                print(f"  {name}: stopped")
            else:
                print(
                    f"  {name}: running pid={item['pid']} ready={'yes' if item['ready'] else 'no'}"
                )
    if getattr(args, "check", False) and summary != "running":
        return runtime.EXIT_NOT_RUNNING
    return 0


def compose_wait(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    state = load_state(root)
    timeout = getattr(args, "timeout", None)
    json_output = getattr(args, "json", False)
    results: dict[str, dict[str, Any]] = {}
    failures = 0
    for name, service in compose.services.items():
        config = _service_config(compose, name, state)
        try:
            data = runtime.wait_for_ready(
                service.root,
                config,
                timeout=timeout,
                progress=not json_output,
            )
        except SimpVmgenError as exc:
            _print_error(name, exc)
            results[name] = {"state": "error", "error": str(exc)}
            failures += 1
        else:
            results[name] = {"state": "ready", **data}
    if json_output:
        print(
            json.dumps(
                {
                    "project": compose.project.name,
                    "state": "ready" if failures == 0 else "error",
                    "services": results,
                },
                indent=2,
            )
        )
    elif failures == 0:
        print(f"{compose.project.name} is ready")
    return 1 if failures else 0


def compose_guest(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    service_name = getattr(args, "service", None)
    if service_name not in compose.services:
        raise ComposeError(f"unknown service {service_name!r}")
    service = compose.services[service_name]
    state = load_state(root)
    config = _service_config(compose, service_name, state)
    return runtime.guest(
        _args(
            service.root,
            config,
            user=getattr(args, "user", None),
            guest_command=getattr(args, "guest_command", None),
            timeout=getattr(args, "timeout", None),
        )
    )


def compose_logs(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    service_name = getattr(args, "service", None)
    if service_name not in compose.services:
        raise ComposeError(f"unknown service {service_name!r}")
    service = compose.services[service_name]
    config = _layout_config(service.root)
    return runtime.log(
        _args(
            service.root,
            config,
            follow=getattr(args, "follow", False),
            lines=getattr(args, "lines", 80),
            no_color=getattr(args, "no_color", False),
        )
    )


def compose_stop(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None))
    compose = load_compose(root)
    json_output = getattr(args, "json", False)
    failures = 0
    with compose_lock(root):
        state = load_state(root)
        targets: dict[str, tuple[Path, Config]] = {
            name: (service.root, _layout_config(service.root))
            for name, service in compose.services.items()
        }
        if state is not None:
            for name, root_text in state.service_roots.items():
                service_root = Path(root_text)
                if name not in targets and service_root.is_dir():
                    targets[name] = (service_root, _layout_config(service_root))
        stale = set(targets) - set(compose.services)
        for name, (service_root, config) in targets.items():
            try:
                output = (
                    contextlib.redirect_stdout(sys.stderr)
                    if json_output
                    else contextlib.nullcontext()
                )
                with output:
                    result = runtime.stop(_args(service_root, config))
            except SimpVmgenError as exc:
                _print_error(name, exc)
                failures += 1
                continue
            if result != 0:
                failures += 1
                continue
            if name in stale:
                with contextlib.suppress(SimpVmgenError):
                    clear_member(service_root)
    if json_output:
        data = {
            name: runtime.status_data(service_root, config)
            for name, (service_root, config) in targets.items()
        }
        print(
            json.dumps(
                {"project": compose.project.name, "services": data},
                indent=2,
            )
        )
    return 1 if failures else 0


def compose_down(args: Any) -> int:
    root = resolve_compose_root(getattr(args, "directory", None), allow_state=True)
    json_output = getattr(args, "json", False)
    load_error: ComposeError | None = None
    try:
        compose: ComposeConfig | None = load_compose(root)
    except ComposeError as exc:
        compose = None
        load_error = exc
    initial_state = load_state(root)
    if compose is None and (initial_state is None or not initial_state.service_roots):
        assert load_error is not None
        raise load_error
    with compose_lock(root):
        state = load_state(root)
        targets: dict[str, tuple[Path, Config]] = {}
        if compose is not None:
            targets = {
                name: (service.root, _layout_config(service.root))
                for name, service in compose.services.items()
            }
        if state is not None:
            for name, root_text in state.service_roots.items():
                if name in targets:
                    continue
                service_root = Path(root_text)
                if service_root.is_dir():
                    targets[name] = (service_root, _layout_config(service_root))

        failures = 0
        for name, (service_root, config) in targets.items():
            print(f"==> stop {name}", file=sys.stderr if json_output else sys.stdout)
            try:
                output = (
                    contextlib.redirect_stdout(sys.stderr)
                    if json_output
                    else contextlib.nullcontext()
                )
                with output:
                    result = runtime.stop(_args(service_root, config))
            except SimpVmgenError as exc:
                _print_error(name, exc)
                failures += 1
                continue
            if result != 0:
                failures += 1

        if failures:
            return 1
        marker_roots = (
            {service.root for service in compose.services.values()}
            if compose is not None
            else set()
        )
        if state is not None:
            marker_roots.update(Path(text) for text in state.service_roots.values())
        for service_root in marker_roots:
            if service_root.is_dir():
                clear_member(service_root)
        release_state_locked(root)
        remaining = load_state(root)

    project_name = (
        compose.project.name
        if compose is not None
        else (initial_state.project if initial_state is not None else "compose")
    )
    owned_bridges = remaining.owned_bridges if remaining is not None else []
    if owned_bridges:
        print(
            "owned bridges remain: "
            + ", ".join(owned_bridges)
            + "; run `sudo simp-vmgen compose network cleanup`",
            file=sys.stderr,
        )
    if json_output:
        print(
            json.dumps(
                {
                    "project": project_name,
                    "state": "down",
                    "owned_bridges": owned_bridges,
                },
                indent=2,
            )
        )
    else:
        print(f"{project_name} is down")
    return 0


def compose_dispatch(args: Any) -> int:
    command = getattr(args, "compose_command", None) or ""
    handlers = {
        "validate": compose_validate,
        "show": compose_show,
        "build": compose_build,
        "up": compose_up,
        "status": compose_status,
        "wait": compose_wait,
        "guest": compose_guest,
        "logs": compose_logs,
        "stop": compose_stop,
        "down": compose_down,
        "network": compose_network_dispatch,
    }
    handler = handlers.get(command)
    if handler is None:
        raise ComposeError("compose requires a subcommand")
    return handler(args)
