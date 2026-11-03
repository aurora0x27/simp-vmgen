from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from simp_vmgen.cli import build_parser, main
from simp_vmgen.config import Config
from simp_vmgen.project import config_from_args
from simp_vmgen.wizard import WizardCancelled, run_wizard


def _answers(values: list[str]) -> Iterator[str]:
    yield from values


ANSWERS = [
    "",  # project name
    "ubuntu",
    "24.04",
    "x86_64",
    "4G",
    "4",
    "30G",
    "auto",
    "auto",
    "",  # ssh key
    "y",  # create root
    "114514",
    "alice",
    "secret",
    "bash",
    "y",  # sudo
    "docker",
    "",
    "",  # finish users
    "alice",  # login user
    "work:/work",
    "base",
    "",
    "n",  # kernel
    "n",  # gdb
    "auto",
    "y",  # confirm
]


def _args(target: Path) -> object:
    return build_parser().parse_args(["new", str(target), "-i"])


def test_wizard_collects_multiple_users(tmp_path: Path) -> None:
    target = tmp_path / "wizard-lab"
    args = _args(target)
    result = run_wizard(
        args,
        target,
        input_fn=lambda _prompt: next(answer_iter),
        output_fn=lambda _line: None,
    )
    config = config_from_args(result, target)
    assert config.project.name == "wizard-lab"
    assert config.ssh.user == "alice"
    assert config.get_user("alice").sudo is True
    assert config.get_user("alice").groups == ["docker"]
    assert config.get_user("alice").shell == "bash"
    assert config.get_user("root").password == "114514"


answer_iter: Iterator[str]


@pytest.fixture(autouse=True)
def _reset_answers() -> Iterator[None]:
    global answer_iter
    answer_iter = _answers(ANSWERS)
    yield


def test_wizard_cancel() -> None:
    target = Path("/tmp/cancel-lab")
    args = _args(target)
    with pytest.raises(WizardCancelled):
        run_wizard(
            args,
            target,
            input_fn=lambda _prompt: "q",
            output_fn=lambda _line: None,
        )


def test_new_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "interactive-lab"
    global answer_iter
    answer_iter = _answers(ANSWERS)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answer_iter))

    assert main(["new", str(target), "-i"]) == 0
    config = Config.load(target / "vmgen.toml")
    assert config.project.name == "interactive-lab"
    assert config.ssh.user == "alice"


def test_new_interactive_requires_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "no-tty"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["new", str(target), "-i"]) == 1
    assert not target.exists()
