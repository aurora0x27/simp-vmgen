from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from simp_vmgen.config import Config, MountConfig, UserConfig
from simp_vmgen.distros import load_distro
from simp_vmgen.errors import ProjectError
from simp_vmgen.seed import render_cloud_init
from simp_vmgen.users import resolve_ssh_keys


def _config(root: Path) -> Config:
    (root / "work").mkdir()
    (root / "src").mkdir()
    config = Config.default("lab")
    config.users = [
        UserConfig("root", password="root-pw"),
        UserConfig(
            "alice",
            password="alice-pw",
            shell="bash",
            sudo=True,
            groups=["sudo", "docker"],
            ssh_keys=["ssh-ed25519 AAAA alice"],
        ),
    ]
    config.ssh.user = "alice"
    config.mounts = [
        MountConfig("work", "/work"),
        MountConfig("src", "/src", readonly=True),
    ]
    config.validate()
    return config


def test_render_ubuntu_user_data(tmp_path: Path) -> None:
    config = _config(tmp_path)
    distro = load_distro("ubuntu")
    run_dir = tmp_path / ".simp-vmgen" / "run"

    artifacts = render_cloud_init(
        config,
        distro,
        tmp_path,
        run_dir,
        generated_public_key="ssh-ed25519 AAAA generated",
        make_iso=False,
    )

    data = yaml.safe_load(artifacts.user_data.read_text())
    assert data["hostname"] == "lab"
    assert data["ssh_pwauth"] is True
    assert [user["name"] for user in data["users"]] == ["root", "alice"]
    alice = data["users"][1]
    assert alice["shell"] == "/usr/bin/bash"
    assert alice["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    assert alice["groups"] == ["sudo", "docker"]
    assert alice["ssh_authorized_keys"] == [
        "ssh-ed25519 AAAA alice",
        "ssh-ed25519 AAAA generated",
    ]
    assert {item["name"] for item in data["chpasswd"]["users"]} == {"root", "alice"}
    assert "fish" in data["packages"]

    files = {item["path"]: item for item in data["write_files"]}
    sshd = files["/etc/ssh/sshd_config.d/01-simp-vmgen.conf"]["content"]
    assert "PermitRootLogin yes" in sshd
    assert "PasswordAuthentication yes" in sshd
    mount_script = files["/usr/local/sbin/simp-vmgen-mounts"]["content"]
    assert "host0 /work" in mount_script
    assert "host1 /src" in mount_script
    assert "trans=virtio,version=9p2000.L,ro host1 /src" in mount_script

    meta = yaml.safe_load(artifacts.meta_data.read_text())
    assert meta["instance-id"] == artifacts.instance_id
    assert meta["local-hostname"] == "lab"


def test_render_alpine_openrc_user_data(tmp_path: Path) -> None:
    config = _config(tmp_path)
    distro = load_distro("alpine")
    artifacts = render_cloud_init(
        config,
        distro,
        tmp_path,
        tmp_path / "run",
        generated_public_key="ssh-ed25519 AAAA generated",
        make_iso=False,
    )
    data = yaml.safe_load(artifacts.user_data.read_text())
    assert data["hostname"] == "lab"
    assert [user["name"] for user in data["users"]] == ["root", "alice"]
    assert data["users"][1]["shell"] == "/bin/bash"
    assert "packages" not in data

    files = {item["path"]: item for item in data["write_files"]}
    sshd = files["/usr/local/sbin/simp-vmgen-ssh"]["content"]
    assert "PermitRootLogin yes" in sshd
    assert "PasswordAuthentication yes" in sshd
    assert files["/etc/init.d/simp-vmgen-ready"]["content"].startswith(
        "#!/sbin/openrc-run"
    )

    runcmd = "\n".join(data["runcmd"])
    assert "apk add " in runcmd
    assert "build-base" not in runcmd
    assert "rc-update add sshd default" in runcmd
    assert "ttyS0::respawn" in runcmd
    assert "rc-update add simp-vmgen-ready default" in runcmd
    assert "deluser --remove-home alpine" in runcmd


def test_alpine_keeps_alpine_user_if_configured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.users.append(UserConfig("alpine", password="alpine-pw"))
    config.ssh.user = "alice"
    config.validate()
    artifacts = render_cloud_init(
        config,
        load_distro("alpine"),
        tmp_path,
        tmp_path / "run",
        make_iso=False,
    )
    assert "deluser --remove-home alpine" not in artifacts.user_data.read_text()


def test_root_login_can_be_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ssh.permit_root_login = False
    config.validate()
    distro = load_distro("ubuntu")
    artifacts = render_cloud_init(
        config, distro, tmp_path, tmp_path / "run", make_iso=False
    )
    data = yaml.safe_load(artifacts.user_data.read_text())
    files = {item["path"]: item for item in data["write_files"]}
    sshd = files["/etc/ssh/sshd_config.d/01-simp-vmgen.conf"]["content"]
    assert "PermitRootLogin no" in sshd


def test_key_only_login_omits_password_module(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for user in config.users:
        user.password = ""
    config.get_user("alice").ssh_keys = []
    config.ssh.user = "alice"
    config.validate()
    artifacts = render_cloud_init(
        config,
        load_distro("ubuntu"),
        tmp_path,
        tmp_path / "run",
        generated_public_key="ssh-ed25519 AAAA generated",
        make_iso=False,
    )
    data = yaml.safe_load(artifacts.user_data.read_text())
    assert data["ssh_pwauth"] is False
    assert "chpasswd" not in data
    assert data["users"][1]["ssh_authorized_keys"] == ["ssh-ed25519 AAAA generated"]


def test_instance_id_changes_with_user_data(tmp_path: Path) -> None:
    config = _config(tmp_path)
    distro = load_distro("ubuntu")
    first = render_cloud_init(
        config, distro, tmp_path, tmp_path / "run1", make_iso=False
    )
    config.users[1].password = "changed"
    second = render_cloud_init(
        config, distro, tmp_path, tmp_path / "run2", make_iso=False
    )
    assert first.instance_id != second.instance_id


def test_extra_user_data_is_appended(tmp_path: Path) -> None:
    config = _config(tmp_path)
    extra = tmp_path / "extra.yaml"
    extra.write_text("runcmd:\n  - echo extra\n", encoding="utf-8")
    config.provision.extra_user_data = str(extra)
    distro = load_distro("ubuntu")
    artifacts = render_cloud_init(
        config, distro, tmp_path, tmp_path / "run", make_iso=False
    )
    assert "runcmd:\n  - echo extra" in artifacts.user_data.read_text()


def test_resolve_ssh_keys(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA file\n", encoding="utf-8")
    keys = resolve_ssh_keys(
        ["ssh-ed25519 AAAA literal", str(key), "ssh-ed25519 AAAA literal"],
        tmp_path,
    )
    assert keys == ["ssh-ed25519 AAAA literal", "ssh-ed25519 AAAA file"]


def test_resolve_ssh_keys_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="cannot read"):
        resolve_ssh_keys([str(tmp_path / "missing.pub")], tmp_path)


@pytest.mark.skipif(
    not any(shutil.which(tool) for tool in ("xorriso", "cloud-localds", "genisoimage")),
    reason="no cloud-init seed tool",
)
def test_build_seed_iso(tmp_path: Path) -> None:
    config = _config(tmp_path)
    distro = load_distro("ubuntu")
    artifacts = render_cloud_init(
        config, distro, tmp_path, tmp_path / "run", make_iso=True
    )
    assert artifacts.seed_iso is not None
    assert artifacts.seed_iso.is_file()
    assert artifacts.seed_iso.stat().st_size > 0
