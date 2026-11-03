from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.distros import (
    available_distros,
    load_distro,
    load_distro_from_path,
    load_distro_from_text,
)
from simp_vmgen.errors import DistroError


def test_available_distros() -> None:
    assert available_distros() == ["alpine", "ubuntu"]


def test_ubuntu_metadata() -> None:
    distro = load_distro("ubuntu")
    assert distro.name == "ubuntu"
    assert distro.init_system == "systemd"
    assert distro.package_manager == "apt"
    assert distro.default_release == "24.04"

    release = distro.release()
    assert release.version == "24.04"
    assert release.machine == "q35"
    assert distro.image_url(release, "x86_64").endswith(
        "noble-server-cloudimg-amd64.img"
    )
    assert release.checksum_url_for("x86_64").endswith("/SHA256SUMS")


def test_alpine_metadata() -> None:
    distro = load_distro("alpine")
    assert distro.init_system == "openrc"
    assert distro.package_manager == "apk"
    assert distro.ssh_service == "sshd"
    assert distro.release().machine == "pc"

    image = distro.release().image_url_for("x86_64")
    assert "nocloud_alpine-3.22.0-x86_64-bios-cloudinit-r0.qcow2" in image


def test_packages_are_merged_and_deduplicated() -> None:
    distro = load_distro("ubuntu")
    packages = distro.packages(["base", "dev"], ["clang", "zsh"])
    assert "fish" in packages
    assert "gdb" in packages
    assert "zsh" in packages
    assert packages.count("clang") == 1
    assert len(packages) == len(set(packages))


def test_unknown_release_and_package_set() -> None:
    distro = load_distro("ubuntu")
    with pytest.raises(DistroError, match="no release"):
        distro.release("99.04")
    with pytest.raises(DistroError, match="no package set"):
        distro.packages(["missing"])


def test_unknown_distro() -> None:
    with pytest.raises(DistroError, match="unknown distro"):
        load_distro("debian")


def test_load_distro_from_path(tmp_path: Path) -> None:
    path = tmp_path / "custom"
    path.mkdir()
    (path / "distro.toml").write_text(
        """
name = "custom"
display_name = "Custom"
architectures = ["x86_64"]
default_release = "1.0"
package_manager = "apt"
[package_sets]
base = ["bash"]
[[release]]
version = "1.0"
image_url = "https://example.invalid/custom-{arch}.qcow2"
""",
        encoding="utf-8",
    )
    distro = load_distro_from_path(path)
    assert distro.name == "custom"
    assert distro.release().image_url_for("x86_64") == (
        "https://example.invalid/custom-x86_64.qcow2"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            """
name = "bad"
architectures = ["x86_64"]
default_release = "2.0"
package_manager = "apt"
[package_sets]
base = ["bash"]
[[release]]
version = "1.0"
image_url = "https://example.invalid/img.qcow2"
""",
            "default_release",
        ),
        (
            """
name = "bad"
architectures = ["x86_64"]
default_release = "1.0"
package_manager = "apt"
[package_sets]
base = ["bash"]
[[release]]
version = "1.0"
image_url = "not-a-url"
""",
            "http",
        ),
        (
            """
name = "bad"
architectures = ["x86_64"]
default_release = "1.0"
package_manager = "apt"
[package_sets]
base = ["bash"]
[[release]]
version = "1.0"
image_url = "https://example.invalid/img.qcow2"
image_checksum = "deadbeef"
""",
            "sha256",
        ),
        (
            """
name = "bad"
architectures = ["x86_64"]
default_release = "1.0"
package_manager = "apt"
[package_sets]
dev = ["bash"]
[[release]]
version = "1.0"
image_url = "https://example.invalid/img.qcow2"
""",
            "base",
        ),
        (
            """
name = "bad"
architectures = ["x86_64"]
default_release = "1.0"
package_manager = "apt"
unknown = true
[package_sets]
base = ["bash"]
[[release]]
version = "1.0"
image_url = "https://example.invalid/img.qcow2"
""",
            "unknown key",
        ),
    ],
)
def test_metadata_validation(text: str, message: str) -> None:
    with pytest.raises(DistroError, match=message):
        load_distro_from_text(text)
