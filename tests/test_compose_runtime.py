from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from simp_vmgen import compose_runtime
from simp_vmgen.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, main
from simp_vmgen.compose import (
    ensure_state,
    load_compose,
    load_state,
    member_marker,
    state_path,
    write_member,
)
from simp_vmgen.config import Config
from simp_vmgen.errors import ComposeError, ProjectError

TEXT = """schema = 1

[project]
name = "runtime-lab"

[networks.lan]
subnet = "192.168.91.0/24"

[services.a]
project = "./a"

[services.a.networks.lan]
address = "192.168.91.10/24"

[services.b]
project = "./b"

[services.b.networks.lan]
address = "192.168.91.11/24"
"""


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _compose(tmp_path: Path) -> Path:
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        Config.default(name).save(root / "vmgen.toml")
    (tmp_path / "vmgen-compose.toml").write_text(TEXT, encoding="utf-8")
    return tmp_path


def _status(root: Path) -> dict[str, object]:
    return {
        "state": "running",
        "pid": 123,
        "ssh": None,
        "gdb": None,
        "started_at": "now",
        "serial_log": str(root / "serial.log"),
        "qemu_log": str(root / "qemu.log"),
        "ready": True,
    }


def test_compose_build_uses_materialized_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path)
    calls: list[SimpleNamespace] = []

    def fake_build(args: SimpleNamespace) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(compose_runtime.runtime, "build", fake_build)
    assert compose_runtime.compose_build(SimpleNamespace(directory=str(root), force=True)) == 0
    assert [Path(call.directory).name for call in calls] == ["a", "b"]
    assert all(call.config_override.networks for call in calls)
    assert all(call.force is True for call in calls)


def test_compose_up_writes_members_and_reports_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _compose(tmp_path)
    calls: list[SimpleNamespace] = []

    def fake_up(args: SimpleNamespace) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(compose_runtime.runtime, "up", fake_up)
    monkeypatch.setattr(compose_runtime.runtime, "status_data", lambda root, config: _status(root))
    assert (
        compose_runtime.compose_up(
            SimpleNamespace(directory=str(root), no_build=False, timeout=10, json=True)
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "running"
    assert set(payload["services"]) == {"a", "b"}
    assert member_marker(root / "a").is_file()
    assert member_marker(root / "b").is_file()
    assert all(call.no_build is False for call in calls)


def test_compose_up_requires_owned_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bridge_compose(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(compose_runtime, "bridge_exists", lambda name: True)
    monkeypatch.setattr(
        compose_runtime.runtime,
        "up",
        lambda args: calls.append(Path(args.directory).name) or 0,
    )
    with pytest.raises(ComposeError, match="not owned"):
        compose_runtime.compose_up(
            SimpleNamespace(directory=str(root), no_build=True, timeout=None, json=False)
        )
    assert calls == []


def test_compose_up_keyboard_interrupt_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path)
    running = {"a": False}
    stopped: list[str] = []

    def fake_running(service_root: Path, config: Config) -> bool:
        return running.get(service_root.name, False)

    def fake_up(args: SimpleNamespace) -> int:
        if Path(args.directory).name == "b":
            raise KeyboardInterrupt
        running["a"] = True
        return 0

    def fake_stop(args: SimpleNamespace) -> int:
        stopped.append(Path(args.directory).name)
        return 0

    monkeypatch.setattr(compose_runtime, "_running", fake_running)
    monkeypatch.setattr(compose_runtime.runtime, "up", fake_up)
    monkeypatch.setattr(compose_runtime.runtime, "stop", fake_stop)
    monkeypatch.setattr(compose_runtime.runtime, "status_data", lambda root, config: _status(root))

    with pytest.raises(KeyboardInterrupt):
        compose_runtime.compose_up(
            SimpleNamespace(directory=str(root), no_build=True, timeout=None, json=False)
        )
    assert stopped == ["a"]
    assert not member_marker(root / "a").exists()
    assert not member_marker(root / "b").exists()
    assert state_path(root).is_file()


def test_compose_up_failure_stops_started_services_and_clears_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path)
    running = {"a": False}
    stopped: list[str] = []

    def fake_running(service_root: Path, config: Config) -> bool:
        return running.get(service_root.name, False)

    def fake_up(args: SimpleNamespace) -> int:
        if Path(args.directory).name == "b":
            raise ProjectError("boom")
        running["a"] = True
        return 0

    def fake_stop(args: SimpleNamespace) -> int:
        stopped.append(Path(args.directory).name)
        return 0

    monkeypatch.setattr(compose_runtime, "_running", fake_running)
    monkeypatch.setattr(compose_runtime.runtime, "up", fake_up)
    monkeypatch.setattr(compose_runtime.runtime, "stop", fake_stop)
    monkeypatch.setattr(compose_runtime.runtime, "status_data", lambda root, config: _status(root))

    args = SimpleNamespace(directory=str(root), no_build=True, timeout=None, json=False)
    assert compose_runtime.compose_up(args) == 1
    assert stopped == ["a"]
    assert not member_marker(root / "a").exists()
    assert not member_marker(root / "b").exists()
    assert state_path(root).is_file()


def test_compose_status_check_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _compose(tmp_path)
    assert main(["-C", str(root), "compose", "status", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "stopped"
    assert payload["services"]["a"]["state"] == "stopped"

    assert main(["-C", str(root), "compose", "status", "--check"]) == 3


def test_compose_wait_uses_service_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _compose(tmp_path)
    seen: list[str] = []

    def fake_wait(
        service_root: Path, config: Config, *, timeout: int | None, progress: bool
    ) -> dict[str, object]:
        seen.append(service_root.name)
        return {"ready": True, "elapsed_seconds": 0.0, "timeout": timeout, "serial_log": ""}

    monkeypatch.setattr(compose_runtime.runtime, "wait_for_ready", fake_wait)
    args = SimpleNamespace(directory=str(root), timeout=15, json=True)
    assert compose_runtime.compose_wait(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "ready"
    assert seen == ["a", "b"]


def test_compose_guest_and_logs_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    guest_calls: list[SimpleNamespace] = []
    log_calls: list[SimpleNamespace] = []

    monkeypatch.setattr(
        compose_runtime.runtime,
        "guest",
        lambda args: guest_calls.append(args) or 0,
    )
    monkeypatch.setattr(
        compose_runtime.runtime,
        "log",
        lambda args: log_calls.append(args) or 0,
    )
    assert (
        compose_runtime.compose_guest(
            SimpleNamespace(
                directory=str(root),
                service="a",
                user=None,
                guest_command=["--", "true"],
                timeout=5,
            )
        )
        == 0
    )
    assert Path(guest_calls[0].directory).name == "a"
    assert guest_calls[0].guest_command == ["--", "true"]

    assert (
        compose_runtime.compose_logs(
            SimpleNamespace(
                directory=str(root),
                service="b",
                follow=False,
                lines=5,
                no_color=True,
            )
        )
        == 0
    )
    assert Path(log_calls[0].directory).name == "b"
    assert log_calls[0].lines == 5


def test_compose_guest_unknown_service(tmp_path: Path) -> None:
    root = _compose(tmp_path)
    with pytest.raises(Exception, match="unknown service"):
        compose_runtime.compose_guest(
            SimpleNamespace(
                directory=str(root),
                service="missing",
                user=None,
                guest_command=["true"],
                timeout=None,
            )
        )


def test_compose_stop_and_down_clear_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    compose = load_compose(root)
    state = ensure_state(compose)
    for name in ("a", "b"):
        write_member(root / name, root, name)

    stopped: list[str] = []
    monkeypatch.setattr(
        compose_runtime.runtime,
        "stop",
        lambda args: stopped.append(Path(args.directory).name) or 0,
    )
    monkeypatch.setattr(
        compose_runtime.runtime, "status_data", lambda service_root, config: _status(service_root)
    )

    assert compose_runtime.compose_stop(SimpleNamespace(directory=str(root), json=False)) == 0
    assert stopped == ["a", "b"]
    assert member_marker(root / "a").is_file()

    stopped.clear()
    assert compose_runtime.compose_down(SimpleNamespace(directory=str(root), json=False)) == 0
    assert stopped == ["a", "b"]
    assert not state_path(root).exists()
    assert load_state(root) is None
    assert not member_marker(root / "a").exists()
    assert not member_marker(root / "b").exists()
    registry = json.loads(
        (tmp_path / "cache" / "simp-vmgen" / "compose" / "endpoints.json").read_text()
    )
    assert state.networks["lan"] not in registry["endpoints"]


def test_compose_down_stops_stale_state_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path)
    stale = tmp_path / "stale"
    stale.mkdir()
    Config.default("stale").save(stale / "vmgen.toml")
    compose = load_compose(root)
    state = ensure_state(compose)
    state.service_roots["stale"] = str(stale)
    from simp_vmgen.compose import save_state

    save_state(root, state)

    stopped: list[str] = []
    monkeypatch.setattr(
        compose_runtime.runtime,
        "stop",
        lambda args: stopped.append(Path(args.directory).name) or 0,
    )
    assert compose_runtime.compose_down(SimpleNamespace(directory=str(root), json=False)) == 0
    assert "stale" in stopped


BRIDGE_TEXT = """schema = 1

[project]
name = "bridge-runtime"

[networks.lan]
driver = "bridge"
subnet = "192.168.95.0/24"

[services.a]
project = "./a"

[services.a.networks.lan]
address = "192.168.95.10/24"

[services.b]
project = "./b"

[services.b.networks.lan]
address = "192.168.95.11/24"
"""


def _bridge_compose(tmp_path: Path) -> Path:
    for name in ("a", "b"):
        service = tmp_path / name
        service.mkdir()
        Config.default(name).save(service / "vmgen.toml")
    (tmp_path / "vmgen-compose.toml").write_text(BRIDGE_TEXT, encoding="utf-8")
    return tmp_path


def test_compose_up_requires_prepared_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bridge_compose(tmp_path)
    monkeypatch.setattr(compose_runtime, "bridge_exists", lambda name: False)
    calls: list[str] = []
    monkeypatch.setattr(
        compose_runtime.runtime, "up", lambda args: calls.append(Path(args.directory).name) or 0
    )
    with pytest.raises(Exception, match="network prepare"):
        compose_runtime.compose_up(
            SimpleNamespace(directory=str(root), no_build=True, timeout=None, json=False)
        )
    assert calls == []


def test_compose_down_keeps_owned_bridge_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bridge_compose(tmp_path)
    state = ensure_state(load_compose(root))
    bridge = state.bridges["lan"]
    write_member(root / "a", root, "a")
    monkeypatch.setattr(compose_runtime.runtime, "stop", lambda args: 0)

    assert compose_runtime.compose_down(SimpleNamespace(directory=str(root), json=False)) == 0
    remaining = load_state(root)
    assert remaining is not None
    assert remaining.owned_bridges == [bridge]
    assert remaining.networks == {}
    assert remaining.services == []
    assert not member_marker(root / "a").exists()


def test_compose_down_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _compose(tmp_path)
    ensure_state(load_compose(root))
    monkeypatch.setattr(compose_runtime.runtime, "stop", lambda args: 0)
    assert main(["-C", str(root), "compose", "down", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "project": "runtime-lab",
        "state": "down",
        "owned_bridges": [],
    }


def test_compose_down_uses_state_when_compose_file_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path)
    ensure_state(load_compose(root))
    write_member(root / "a", root, "a")
    (root / "vmgen-compose.toml").unlink()

    stopped: list[str] = []
    monkeypatch.setattr(
        compose_runtime.runtime,
        "stop",
        lambda args: stopped.append(Path(args.directory).name) or 0,
    )
    assert compose_runtime.compose_down(SimpleNamespace(directory=str(root), json=False)) == 0
    assert stopped == ["a", "b"]
    assert not state_path(root).exists()
    assert not member_marker(root / "a").exists()


def test_compose_down_uses_state_when_config_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _compose(tmp_path)
    ensure_state(load_compose(root))
    write_member(root / "a", root, "a")
    (root / "vmgen-compose.toml").write_text("not valid toml", encoding="utf-8")

    stopped: list[str] = []
    monkeypatch.setattr(
        compose_runtime.runtime,
        "stop",
        lambda args: stopped.append(Path(args.directory).name) or 0,
    )
    assert compose_runtime.compose_down(SimpleNamespace(directory=str(root), json=False)) == 0
    assert stopped == ["a", "b"]
    assert not state_path(root).exists()
    assert not member_marker(root / "a").exists()


def test_compose_stop_failure_keeps_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _compose(tmp_path)
    ensure_state(load_compose(root))

    def fail(args: SimpleNamespace) -> int:
        raise ProjectError("cannot stop")

    monkeypatch.setattr(compose_runtime.runtime, "stop", fail)
    assert compose_runtime.compose_down(SimpleNamespace(directory=str(root), json=False)) == 1
    assert state_path(root).is_file()


def test_compose_dry_run_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _compose(tmp_path)
    assert main(["-C", str(root), "-n", "compose", "up"]) == EXIT_USAGE
    assert "--dry-run is not supported" in capsys.readouterr().err


def test_compose_unknown_service_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _compose(tmp_path)
    assert main(["-C", str(root), "compose", "guest", "missing", "--", "true"]) == EXIT_ERROR
    assert "unknown service" in capsys.readouterr().err
