from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

import pytest

from simp_vmgen.config import Config, KernelConfig
from simp_vmgen.initramfs import build_initramfs
from simp_vmgen.layout import ProjectLayout


@pytest.mark.skipif(shutil.which("cpio") is None, reason="cpio missing")
def test_build_initramfs(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    config = Config.default("lab")
    config.kernel = KernelConfig(enabled=True, version="6.1.0")
    config.validate()
    layout = ProjectLayout(tmp_path, config)

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

    archive = gzip.decompress(output.read_bytes())
    result = subprocess.run(
        ["cpio", "-it"], input=archive, capture_output=True, check=False
    )
    listing = result.stdout.decode()
    assert "init" in listing
    assert "bin/busybox" in listing
    assert "etc/passwd" in listing


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
