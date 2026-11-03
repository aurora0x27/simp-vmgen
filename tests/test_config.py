from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.config import Config, MountConfig
from simp_vmgen.errors import ConfigError

MULTI_USER = """
schema = 1

[project]
name = "lab-team"

[distro]
name = "alpine"
release = "3.22"
arch = "x86_64"

[vm]
mem = "2G"
cpus = 2
disk = "10G"

[ssh]
user = "alice"
port = 2222

[[users]]
name = "alice"
password = "alice-secret"
shell = "bash"
sudo = true
groups = ["wheel", "docker"]
ssh_keys = ["ssh-ed25519 AAAA test"]

[[users]]
name = "bob"

[[mount]]
source = "work"
target = "/work"

[[mount]]
source = "/srv/data"
target = "/data"
readonly = true
"""


def test_default_config_is_root_login() -> None:
    config = Config.default("Week6")
    config.validate()

    assert config.project.name == "Week6"
    assert config.project.hostname == "week6"
    assert config.ssh.user == "root"
    assert config.login_user.name == "root"
    assert config.login_user.password == "114514"
    assert config.mounts == [MountConfig("work", "/work")]


def test_default_config_roundtrip() -> None:
    config = Config.default("lab")
    loaded = Config.loads(config.dumps())
    assert loaded == config


def test_multiple_users_and_non_root_login() -> None:
    config = Config.loads(MULTI_USER)

    assert config.login_user.name == "alice"
    assert config.login_user.sudo is True
    assert config.login_user.groups == ["wheel", "docker"]
    assert config.get_user("bob").password == ""
    assert [mount.target for mount in config.mounts] == ["/work", "/data"]
    assert config.mounts[1].readonly is True


def test_empty_mount_list_roundtrip() -> None:
    config = Config.loads(
        """
schema = 1
mount = []
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
"""
    )
    assert config.mounts == []
    assert Config.loads(config.dumps()).mounts == []


def test_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "vmgen.toml"
    config = Config.loads(MULTI_USER)
    config.save(path)
    assert path.exists()
    assert Config.load(path) == config


def test_mount_parse() -> None:
    assert MountConfig.parse("work:/work") == MountConfig("work", "/work")
    assert MountConfig.parse("./src:/src:ro") == MountConfig("./src", "/src", readonly=True)
    assert MountConfig.parse("./src:/src:rw") == MountConfig("./src", "/src")
    with pytest.raises(ConfigError, match="absolute"):
        MountConfig.parse("work:work")
    with pytest.raises(ConfigError, match="mode"):
        MountConfig.parse("work:/work:shared")
    with pytest.raises(ConfigError, match="source"):
        MountConfig.parse(":/work")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[[users]]
name = "root"
[[users]]
name = "root"
""",
            "duplicate user",
        ),
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[ssh]
user = "missing"
[[users]]
name = "root"
""",
            "not defined",
        ),
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[[mount]]
source = "work"
target = "relative"
""",
            "absolute",
        ),
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[vm]
cpus = 0
""",
            "cpus",
        ),
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[vm]
mem = "four gigabytes"
""",
            "invalid size",
        ),
        (
            """
schema = 2
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
""",
            "unsupported schema",
        ),
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[ssh]
user = "root"
permit_root_login = false
""",
            "permit_root_login",
        ),
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[kernel]
enabled = true
""",
            "version or source",
        ),
        (
            """
schema = 1
[project]
name = "lab"
[distro]
name = "ubuntu"
release = "24.04"
[unknown]
value = 1
""",
            "unknown key",
        ),
    ],
)
def test_validation_errors(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Config.loads(text)


def test_root_login_cannot_be_disabled_for_root_user() -> None:
    config = Config.default("lab")
    config.ssh.permit_root_login = False
    with pytest.raises(ConfigError, match="permit_root_login"):
        config.validate()


def test_login_user_needs_authentication() -> None:
    config = Config.default("lab")
    config.ssh.key = ""
    config.login_user.password = ""
    with pytest.raises(ConfigError, match="needs a password"):
        config.validate()


def test_users_can_be_explicitly_listed_without_root() -> None:
    config = Config.loads(
        """
schema = 1
[project]
name = "lab"
[distro]
name = "alpine"
release = "3.22"
[ssh]
user = "alice"
[[users]]
name = "alice"
password = "secret"
"""
    )
    assert [user.name for user in config.users] == ["alice"]
    assert config.login_user.name == "alice"
