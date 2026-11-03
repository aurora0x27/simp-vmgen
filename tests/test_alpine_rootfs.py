from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from simp_vmgen import alpine
from simp_vmgen.config import Config, KernelConfig, UserConfig
from simp_vmgen.layout import ProjectLayout
from simp_vmgen.rootfs import load_rootfs
from simp_vmgen.rootfs_source import RootfsSourceInfo


def _fake_rootfs(tmp_path: Path) -> Path:
    root = tmp_path / "rootfs"
    (root / "etc" / "init.d").mkdir(parents=True)
    (root / "etc" / "network").mkdir(parents=True)
    (root / "etc" / "skel").mkdir(parents=True)
    (root / "usr" / "local" / "bin").mkdir(parents=True)
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\nsshd:x:22:22:sshd:/dev/null:/sbin/nologin\n"
    )
    (root / "etc" / "group").write_text("root:x:0:\nsshd:x:22:\n")
    (root / "etc" / "shadow").write_text("root:*:::::::\nsshd:!:::::::\n")
    (root / "etc" / "inittab").write_text(
        "::sysinit:/sbin/openrc sysinit\n"
        "::wait:/sbin/openrc default\n"
        "#ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100\n"
    )
    (root / "etc" / "skel" / ".profile").write_text("# profile\n")
    (root / "etc" / "ssh").mkdir()
    (root / "etc" / "ssh" / "sshd_config").write_text("Include /etc/ssh/sshd_config.d/*.conf\n")
    for service in (
        "devfs",
        "procfs",
        "sysfs",
        "mtab",
        "bootmisc",
        "hostname",
        "localmount",
        "modules",
        "sysctl",
        "networking",
        "sshd",
        "local",
    ):
        (root / "etc" / "init.d" / service).write_text("#!/sbin/openrc-run\n")
    return root


def _config(tmp_path: Path) -> Config:
    (tmp_path / "work").mkdir()
    config = Config.default("lab")
    config.distro.name = "alpine"
    config.distro.release = "3.22"
    config.ssh.user = "alice"
    config.users = [
        UserConfig("root", password="root-secret"),
        UserConfig(
            "alice",
            password="alice-secret",
            shell="bash",
            sudo=True,
            groups=["docker"],
            ssh_keys=["ssh-ed25519 AAAA alice"],
        ),
    ]
    config.provision.package_sets = []
    config.kernel = KernelConfig(enabled=True, version="6.1.0", rootfs="alpine")
    config.validate()
    return config


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl missing")
def test_prepare_alpine_rootfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    layout = ProjectLayout(tmp_path, config)
    root = _fake_rootfs(tmp_path)
    info = RootfsSourceInfo(
        spec=load_rootfs("alpine"),
        archive=tmp_path / "mini.tar.gz",
        root_dir=root,
        checksum="sha256:" + "a" * 64,
    )
    monkeypatch.setattr(alpine, "fetch_rootfs", lambda *args, **kwargs: info)
    monkeypatch.setattr(alpine, "install_packages", lambda *args, **kwargs: None)
    modules = layout.kernel_dir / "modules" / "lib" / "modules" / "6.1.0"
    modules.mkdir(parents=True)
    (modules / "modules.dep").write_text("")

    alpine.prepare_alpine_rootfs(
        config,
        tmp_path,
        layout,
        generated_public_key="ssh-ed25519 AAAA generated",
    )

    passwd = (root / "etc/passwd").read_text()
    assert "alice:x:1000:1000::/home/alice:/bin/bash" in passwd
    shadow = (root / "etc/shadow").read_text()
    assert "alice:$6$" in shadow
    keys = (root / "home/alice/.ssh/authorized_keys").read_text()
    assert "ssh-ed25519 AAAA alice" in keys
    assert "ssh-ed25519 AAAA generated" in keys
    assert "docker" in (root / "etc/group").read_text()
    assert "alice ALL=(ALL) NOPASSWD:ALL" in (root / "etc/sudoers.d/90-simp-vmgen").read_text()
    sshd = (root / "etc/ssh/sshd_config.d/01-simp-vmgen.conf").read_text()
    assert "PermitRootLogin yes" in sshd
    assert "PasswordAuthentication yes" in sshd
    init = root / "init"
    assert init.stat().st_mode & 0o111
    assert "/work" in init.read_text()
    assert "exec /sbin/init" in init.read_text()
    assert (root / "etc/runlevels/default/sshd").is_symlink()
    assert (root / "etc/runlevels/default/simp-vmgen-ready").is_symlink()
    assert "ttyS0::respawn" in (root / "etc/inittab").read_text()
    assert (root / "lib/modules/6.1.0/modules.dep").is_file()
    assert (root / "home/alice/.profile").is_file()
