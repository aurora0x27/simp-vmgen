# simp-vmgen TODO

Progress is tracked one work item at a time. After each item the implementation
stops and a commit message is provided.

## M1 - Project skeleton

- [x] M1.1 Initialize the uv project, package layout, CLI entry point and test setup
- [x] M1.2 Add the configuration model, TOML loading/saving and validation
- [x] M1.3 Add distro discovery and the Ubuntu/Alpine metadata format
- [x] M1.4 Implement `new` (non-interactive) and `regen --check`
- [x] M1.5 Implement `diagnose`

## M2 - Cloud-image environments

- [x] M2.1 Image cache, checksum verification and `vmgen.lock`
- [x] M2.2 Cloud-init seed generation for Ubuntu
- [x] M2.3 QEMU argument builder and `build` / `run` / `stop` / `status`
- [x] M2.4 SSH key handling, `ssh` / `tty` / `guest` / `wait`
- [x] M2.5 `serial` / `console` / `log` / `clean`
- [x] M2.6 User management: root login toggle, root password, multiple users,
      per-user password/SSH keys/shell/sudo
- [x] M2.7 Alpine cloud-image support (OpenRC provisioning)
- [x] M2.8 Volume mounts (`-v SRC:DST[:ro]`) for cloud-init distros

## M3 - Interactive workflow

- [x] M3.1 `-i` wizard with defaults, sections, summary and cancel
- [x] M3.2 `regen` conflict detection and `--force`
- [x] M3.3 Generated README, `.gitignore` and `show --json`

## M4 - Kernel experiments

- [x] M4.1 Kernel source cache, fetch and checksum verification
- [x] M4.2 Kconfig handling (`defconfig` + fragments)
- [x] M4.3 Kernel build (`bzImage`, `vmlinux`, modules)
- [ ] M4.4 Busybox rootfs plugin and initramfs generation
- [ ] M4.5 Kernel boot arguments, GDB stub and `sim-vmgen kernel` commands
- [ ] M4.6 Volume mounts inside the busybox init script

## M5 - Polish

- [ ] M5.1 Golden tests for Ubuntu/Alpine render output
- [ ] M5.2 QEMU argv snapshot tests
- [ ] M5.3 Slow integration boot tests
- [ ] M5.4 Documentation and `uv tool install` smoke test
