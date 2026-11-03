from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

import pytest

from simp_vmgen.config import Config, KernelConfig, MountConfig
from simp_vmgen.initramfs import build_initramfs
from simp_vmgen.layout import ProjectLayout


@pytest.mark.skipif(shutil.which("cpio") is None, reason="cpio missing")
def test_build_initramfs(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "src").mkdir()
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.1.0")
    config.mounts.append(MountConfig("src", "/src", readonly=True))
    config.validate()
    layout = ProjectLayout(tmp_path, config)
    modules = layout.kernel_dir / "modules" / "lib" / "modules" / "6.1.0"
    modules.mkdir(parents=True)
    (modules / "modules.dep").write_text("")

    output = build_initramfs(
        config,
        layout,
        busybox_path=Path("/bin/true"),
        install_applets=False,
    )
    assert output.is_file()
    assert (layout.kernel_dir / "initramfs" / "init").is_file()
    assert (layout.kernel_dir / "initramfs" / "etc" / "passwd").is_file()
    init = (layout.kernel_dir / "initramfs" / "init").read_text()
    assert "SIMP_VMGEN_READY" in init
    assert "host0" in init
    assert "/work" in init
    assert "version=9p2000.L,ro host1 /src" in init
    assert (layout.kernel_dir / "initramfs" / "lib" / "modules" / "6.1.0" / "modules.dep").is_file()

    archive = gzip.decompress(output.read_bytes())
    result = subprocess.run(
        ["cpio", "-it"], input=archive, capture_output=True, check=False
    )
    listing = result.stdout.decode()
    assert "init" in listing
    assert "bin/busybox" in listing
    assert "etc/passwd" in listing


def test_initramfs_applet_symlinks_are_relative(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    fake = tmp_path / "fake-busybox"
    fake.write_text("#!/bin/sh\necho sh\necho mount\n")
    fake.chmod(0o755)
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.1.0")
    config.validate()
    layout = ProjectLayout(tmp_path, config)
    build_initramfs(config, layout, busybox_path=fake, install_applets=True)
    sh = layout.kernel_dir / "initramfs" / "bin" / "sh"
    assert sh.is_symlink()
    assert sh.readlink() == Path("busybox")


def test_initramfs_dry_run(tmp_path: Path) -> None:
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.1.0")
    config.validate()
    layout = ProjectLayout(tmp_path, config)
    output = build_initramfs(config, layout, dry_run=True)
    assert output == layout.kernel_dir / "initramfs.cpio.gz"
    assert not output.exists()


def test_initramfs_none_rootfs(tmp_path: Path) -> None:
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.1.0", rootfs="none")
    config.validate()
    layout = ProjectLayout(tmp_path, config)
    from simp_vmgen.errors import ProjectError

    with pytest.raises(ProjectError, match="rootfs"):
        build_initramfs(config, layout)
