# Changelog

All notable changes to `simp-vmgen` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Experimental `[[network]]` NICs for single-VM projects, using rootless QEMU
  socket multicast or a Linux bridge.
- Compose projects (`vmgen-compose.toml`) for validating, building, running,
  inspecting, stopping and tearing down multiple VMs.
- `compose network prepare/cleanup/status` for explicitly privileged bridge
  creation, ownership tracking and diagnostics.
- An attacker/target/observer example under `examples/network-lab`.

### Changed

- `[[network]]` now requires exactly one of `mcast` or `bridge`.
- Compose-managed services are marked and refuse standalone `run`.
- Ubuntu cloud-init only runs `package_update` when packages are requested.

### Fixed

- Privileged `compose network prepare/cleanup` no longer leaves root-owned
  state/lock files or allocates socket endpoints in the root cache.
- Removing an external `bridge` setting no longer makes cleanup delete that
  external bridge.
- Compose validates service address prefixes and duplicate host addresses.
- `compose up` cleans up markers and started services on Ctrl-C.
- `compose down` can recover from a missing or invalid compose file using saved
  state.
- `compose network status` reports QEMU bridge allowlist entries.

### Notes

- Socket multicast is the default rootless backend. Bridge networking needs a
  prepared host, `qemu-bridge-helper` and privileged network preparation.
- Release/versioning for the compose work has not been decided yet.

## [0.2.0] - 2026-11-06

### Added

- Alpine minirootfs rootfs plugin: provisions users, passwords, SSH keys,
  OpenRC runlevels and packages with a pinned `apk.static`, then packages the
  result as a bootable initramfs.
- Discoverable rootfs plugin system (`busybox`, `alpine`) with `kernel rootfs`.
- Shell completion generation for bash, zsh and fish.
- `up`, `restart` and `run --wait` for one-shot start/restart workflows.
- Machine-readable output: `status --json`, `status --check`,
  `kernel status --json`, `run --json`, `wait --json`, and password redaction
  for `show --json` (with `--show-secrets` to opt out).
- `guest --timeout` and `guest --workdir`.
- `log --lines` and `log --no-color`; ANSI escapes are stripped when stdout is
  not a TTY.
- Build stamps and stale-artifact detection for seeds, overlays, kernels and
  initramfs images.
- Streamed build output with persistent logs under `.simp-vmgen/logs/`.
- Project-oriented Makefile help plus `kernel-rootfs` and `kernel-clean`.
- `-C/--directory` is accepted after the subcommand as well as before it.
- `ssh`, `tty` and `guest` can select any configured user with `--user`.
- Kernel development package checks in `diagnose`.
- `validate`-style distro checks in `show` and `diagnose`.

### Changed

- `make log` now prints recent lines and exits; use `make log-follow` for the
  old follow behavior.
- `-n/--dry-run` is rejected for commands that do not support it instead of
  being silently ignored.
- `status --check` exits with code 3 when the VM is stopped.
- Generated READMEs document interactive commands and agent-safe commands
  separately, and point at the released install command.

### Fixed

- Guest and SSH commands preserve quoting and remote exit codes; the generated
  `guest`/`lab` Makefile targets handle complex commands.
- Stale PID files are validated against the QEMU command line before stopping
  or reporting a VM as running.
- Runtime artifacts built from an older configuration are detected and refused.
- The Ubuntu readiness service is reliably enabled during provisioning.
- Unix socket paths are shortened for deeply nested project directories.
- Unknown distros and configuration errors are reported with clearer paths.
- Destructive `new --force` and `clean --all` operations warn about what is
  removed.
- `run --dry-run` works before artifacts exist, and `kernel build --dry-run`
  previews configuration commands.
- Dry-run mode no longer changes state for supported commands.

### Upgrade notes

- Generated files and the cloud-init seed embed the tool version. In a project
  created with 0.1.0, run `simp-vmgen regen` and then `simp-vmgen build` after
  upgrading.
- Existing VMs must be rebuilt because the seed `instance-id` changes; guest
  state in the overlay is preserved unless `build --force` is used.

## [0.1.0] - 2026-11-04

Initial release: Ubuntu and Alpine cloud-image labs, cloud-init provisioning,
SSH, serial console, 9p mounts, kernel source builds, kernel artifacts, and a
busybox initramfs rootfs.
