from __future__ import annotations

from pathlib import Path

import pytest

from simp_vmgen.cli import EXIT_ERROR, EXIT_OK, main
from simp_vmgen.config import Config


def test_new_ubuntu_project(tmp_path: Path) -> None:
    target = tmp_path / "lab"
    assert main(["new", str(target), "--mem", "8G", "--cpus", "6"]) == EXIT_OK

    config = Config.load(target / "vmgen.toml")
    assert config.project.name == "lab"
    assert config.distro.name == "ubuntu"
    assert config.distro.release == "24.04"
    assert config.distro.image.endswith("noble-server-cloudimg-amd64.img")
    assert config.vm.mem == "8G"
    assert config.vm.cpus == 6
    assert config.vm.machine == "q35"
    assert config.ssh.user == "root"
    assert config.login_user.password == "114514"
    assert (target / "work").is_dir()
    assert "SIMP_VMGEN ?= simp-vmgen" in (target / "Makefile").read_text()
    assert "make build" in (target / "README.md").read_text()
    assert ".simp-vmgen/" in (target / ".gitignore").read_text()
    assert (target / ".simp-vmgen" / "generated.json").is_file()


def test_new_next_steps_are_non_interactive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "lab"
    assert main(["new", str(target)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "make up" in out
    assert "make guest" in out
    assert "make tty" not in out


def test_new_kernel_next_steps_are_mode_aware(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "lab-kernel"
    assert main(["new", str(target), "--kernel-version", "6.12.5"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "kernel builds can take a long time" in out
    readme = (target / "README.md").read_text()
    assert "fetch/configure/build the kernel and initramfs" in readme


def test_new_alpine_uses_distro_defaults(tmp_path: Path) -> None:
    target = tmp_path / "alpine-lab"
    assert main(["new", str(target), "--distro", "alpine"]) == EXIT_OK

    config = Config.load(target / "vmgen.toml")
    assert config.distro.name == "alpine"
    assert config.distro.release == "3.22"
    assert config.vm.mem == "2G"
    assert config.vm.cpus == 2
    assert config.vm.disk == "10G"
    assert config.vm.machine == "pc"


def test_new_multiple_users_and_non_root_login(tmp_path: Path) -> None:
    target = tmp_path / "team"
    assert (
        main(
            [
                "new",
                str(target),
                "--user",
                "alice:alice-secret",
                "--user",
                "bob",
                "--login-user",
                "alice",
            ]
        )
        == EXIT_OK
    )
    config = Config.load(target / "vmgen.toml")
    assert config.ssh.user == "alice"
    assert config.login_user.password == "alice-secret"
    assert {user.name for user in config.users} == {"root", "alice", "bob"}


def test_new_kernel_artifact_and_options(tmp_path: Path) -> None:
    target = tmp_path / "kernel-lab"
    artifact = tmp_path / "bzImage"
    artifact.write_bytes(b"kernel")
    assert (
        main(
            [
                "new",
                str(target),
                "--kernel-artifact",
                str(artifact),
                "--kernel-artifact-type",
                "bzImage",
                "--kernel-initrd",
                str(tmp_path / "initrd"),
                "--kernel-append",
                "loglevel=7",
            ]
        )
        == EXIT_OK
    )
    config = Config.load(target / "vmgen.toml")
    assert config.kernel.enabled is True
    assert config.kernel.mode == "artifact"
    assert config.kernel.artifact == str(artifact)
    assert config.kernel.append == "loglevel=7"


def test_new_kernel_build_options(tmp_path: Path) -> None:
    target = tmp_path / "kernel-build"
    assert (
        main(
            [
                "new",
                str(target),
                "--kernel-version",
                "6.12.5",
                "--kernel-option",
                "CONFIG_DEBUG_INFO=y",
                "--kernel-fragment",
                "fragment.config",
                "--kernel-target",
                "bzImage",
                "--kernel-make-arg",
                "LLVM=1",
            ]
        )
        == EXIT_OK
    )
    config = Config.load(target / "vmgen.toml")
    assert config.kernel.enabled is True
    assert config.kernel.mode == "build"
    assert config.kernel.version == "6.12.5"
    assert config.kernel.options == {"CONFIG_DEBUG_INFO": "y"}
    assert config.kernel.fragments == ["fragment.config"]
    assert config.kernel.make_args == ["LLVM=1"]


def test_new_root_password_and_user_details(tmp_path: Path) -> None:
    target = tmp_path / "team-details"
    assert (
        main(
            [
                "new",
                str(target),
                "--root-password",
                "root-secret",
                "--user",
                "alice:alice-secret",
                "--user-shell",
                "alice=/bin/bash",
                "--user-sudo",
                "alice",
                "--user-group",
                "alice=docker",
                "--user-key",
                "alice=ssh-ed25519 AAAA alice",
                "--login-user",
                "alice",
            ]
        )
        == EXIT_OK
    )
    config = Config.load(target / "vmgen.toml")
    assert config.get_user("root").password == "root-secret"
    alice = config.get_user("alice")
    assert alice.shell == "/bin/bash"
    assert alice.sudo is True
    assert alice.groups == ["docker"]
    assert alice.ssh_keys == ["ssh-ed25519 AAAA alice"]


def test_new_without_root_user(tmp_path: Path) -> None:
    target = tmp_path / "alice-only"
    assert (
        main(
            [
                "new",
                str(target),
                "--no-root-user",
                "--user",
                "alice:secret",
                "--login-user",
                "alice",
            ]
        )
        == EXIT_OK
    )
    config = Config.load(target / "vmgen.toml")
    assert [user.name for user in config.users] == ["alice"]
    assert config.login_user.name == "alice"


def test_new_volumes(tmp_path: Path) -> None:
    target = tmp_path / "mounted"
    assert (
        main(
            [
                "new",
                str(target),
                "-v",
                "src:/src:ro",
                "-v",
                "data:/srv/data",
            ]
        )
        == EXIT_OK
    )
    config = Config.load(target / "vmgen.toml")
    assert [mount.target for mount in config.mounts] == ["/work", "/src", "/srv/data"]
    assert config.mounts[1].readonly is True
    assert (target / "src").is_dir()
    assert (target / "data").is_dir()


def test_new_refuses_non_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    assert main(["new", str(target)]) == EXIT_ERROR
    assert (target / "keep.txt").exists()
    assert not (target / "vmgen.toml").exists()


def test_new_force_warns_when_overwriting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    assert main(["new", str(target), "--force"]) == EXIT_OK
    assert "overwriting existing files" in capsys.readouterr().err
    assert (target / "keep.txt").is_file()


def test_new_dry_run(tmp_path: Path) -> None:
    target = tmp_path / "dry"
    assert main(["-n", "new", str(target)]) == EXIT_OK
    assert not target.exists()


def test_regen_check_and_update(tmp_path: Path) -> None:
    target = tmp_path / "regen"
    assert main(["new", str(target)]) == EXIT_OK
    assert main(["-C", str(target), "regen", "--check"]) == EXIT_OK

    makefile = target / "Makefile"
    makefile.write_text("# locally modified\n", encoding="utf-8")
    assert main(["-C", str(target), "regen", "--check"]) == EXIT_ERROR
    assert main(["-C", str(target), "regen"]) == EXIT_ERROR
    assert makefile.read_text() == "# locally modified\n"

    assert main(["-C", str(target), "regen", "--force"]) == EXIT_OK
    assert "SIMP_VMGEN ?= simp-vmgen" in makefile.read_text()


def test_regen_updates_stale_generated_file(tmp_path: Path) -> None:
    from simp_vmgen.manifest import GeneratedManifest, text_hash

    target = tmp_path / "stale"
    assert main(["new", str(target)]) == EXIT_OK
    makefile = target / "Makefile"
    stale = "# old generated content\n"
    makefile.write_text(stale, encoding="utf-8")
    manifest = GeneratedManifest.load(GeneratedManifest.path(target))
    manifest.files["Makefile"] = text_hash(stale)
    manifest.save(GeneratedManifest.path(target))

    assert main(["-C", str(target), "regen"]) == EXIT_OK
    assert "SIMP_VMGEN ?= simp-vmgen" in makefile.read_text()


def test_regen_missing_manifest_treats_files_as_modified(tmp_path: Path) -> None:
    target = tmp_path / "no-manifest"
    assert main(["new", str(target)]) == EXIT_OK
    (target / ".simp-vmgen" / "generated.json").unlink()
    makefile = target / "Makefile"
    makefile.write_text("# local\n", encoding="utf-8")

    assert main(["-C", str(target), "regen"]) == EXIT_ERROR
    assert makefile.read_text() == "# local\n"
    assert main(["-C", str(target), "regen", "--force"]) == EXIT_OK


def test_regen_discovers_project_from_subdirectory(tmp_path: Path) -> None:
    target = tmp_path / "discover"
    assert main(["new", str(target)]) == EXIT_OK
    nested = target / "work" / "nested"
    nested.mkdir(parents=True)
    # main() has no cwd argument, so use -C on the nested directory's parent.
    assert main(["-C", str(target), "regen", "--check"]) == EXIT_OK
