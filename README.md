# simp-vmgen

Generate reproducible QEMU lab environments from a small TOML project file.

`simp-vmgen` is a thin, installable CLI. A generated project contains only
`vmgen.toml`, a `Makefile` that forwards to `simp-vmgen`, and a generated
`README.md`; all build and runtime logic lives in the installed tool.

It intentionally covers simple workflows:

- cloud-image VMs with cloud-init, SSH, serial and 9p shares
- Ubuntu and Alpine guests
- kernel source fetch/build from a version or commit
- booting an existing `bzImage` / `vmlinuz` / `vmlinux`
- a busybox initramfs rootfs for kernel experiments

It is not a kernel development environment, a multi-VM orchestrator, or a
replacement for tools such as `virtme-ng`.

## Status

Usable for cloud-image labs, Alpine/Ubuntu experiments, custom kernel builds
and existing kernel artifacts. See [AGENTS.md](AGENTS.md) for architecture and
development guidance, and [docs/configuration.md](docs/configuration.md) for
the full config reference.

## Install

```sh
uv tool install --editable .
# or, for local development:
uv run simp-vmgen --help
```

## CLI overview

```sh
simp-vmgen new lab-ebpf --distro ubuntu --release 24.04 \
    --mem 4G --cpus 4 -v work:/work
simp-vmgen new lab-kernel -i
simp-vmgen -C lab-ebpf build
simp-vmgen -C lab-ebpf run
simp-vmgen -C lab-ebpf tty
simp-vmgen diagnose
```

Distro support: Ubuntu 24.04 and Alpine 3.22. Kernel rootfs support uses a
static busybox initramfs by default.

### CLI reference

| Command | Purpose |
|---|---|
| `new` | Create a project, optionally with `-i` |
| `regen` | Re-render thin project files; `--check`, `--force` |
| `show` | Show resolved config; `--json` |
| `diagnose` | Check host dependencies |
| `build` | Prepare image/seed or kernel/initramfs |
| `run` | Start QEMU (`--foreground`, `--snapshot`) |
| `wait` | Wait for the readiness marker |
| `tty` / `ssh` / `guest` | SSH login or run a guest command |
| `serial` / `console` | Attach to serial or QEMU monitor |
| `log` | Show or follow the serial log |
| `status` / `stop` / `clean` | Inspect, stop or remove runtime state |
| `kernel fetch/config/build/initramfs/status/clean` | Kernel workflow |
| `gdb` | Connect to the QEMU gdb stub |

### Project layout

```text
lab/
├── vmgen.toml
├── vmgen.lock
├── Makefile
├── README.md
├── .gitignore
├── work/
└── .simp-vmgen/       # ignored runtime state
```


## Configuration

`vmgen.toml` is the source of truth. The default is a root login with the
password `114514`, plus a `work/` directory mounted at `/work`:

```toml
schema = 1

[project]
name = "lab"

[distro]
name = "ubuntu"
release = "24.04"
arch = "x86_64"

[vm]
mem = "4G"
cpus = 4
disk = "30G"
accel = "auto"
display = "none"

[ssh]
enabled = true
host = "127.0.0.1"
port = "auto"
user = "root"          # change to another user for non-root login
permit_root_login = true
wait_timeout = 900

[[mount]]
source = "work"
target = "/work"

[[users]]
name = "root"
password = "114514"
```

Additional users use the same `[[users]]` table:

```toml
[ssh]
user = "alice"

[[users]]
name = "root"
password = "114514"

[[users]]
name = "alice"
password = "alice-secret"
shell = "bash"
sudo = true
groups = ["docker"]
ssh_keys = ["ssh-ed25519 AAAA..."]
```

`ssh.user` selects the account used by `simp-vmgen ssh/tty/guest`. Set it to a
non-root user for a non-root default login; root login can also be disabled
with `permit_root_login = false` (then `ssh.user` must not be `root`).

## Volumes

Docker-style volumes may be added when creating a project or edited in
`vmgen.toml`:

```sh
simp-vmgen new lab -v work:/work -v ~/src:/src:ro
```

```toml
[[mount]]
source = "work"
target = "/work"

[[mount]]
source = "~/src"
target = "/src"
readonly = true
```

Relative sources are resolved from the project root. Each mount becomes a 9p
share (`host0`, `host1`, ...) and is mounted in the guest by cloud-init.

## Inspecting a project

```sh
simp-vmgen -C lab show
simp-vmgen -C lab show --json
```

`show` prints the resolved configuration, lock information and runtime paths.
`--json` is intended for scripts.

## Kernel experiments

Two simple modes are supported:

```sh
# Build a kernel from a version or git commit
simp-vmgen new lab-kernel --kernel-version 6.12.5
simp-vmgen -C lab-kernel kernel fetch
simp-vmgen -C lab-kernel kernel config
simp-vmgen -C lab-kernel kernel build
simp-vmgen -C lab-kernel kernel initramfs
simp-vmgen -C lab-kernel run

# Or boot an existing artifact with a busybox initramfs
simp-vmgen new lab-artifact --kernel-artifact /boot/vmlinuz-linux \
    --kernel-artifact-type vmlinuz
simp-vmgen -C lab-artifact build
simp-vmgen -C lab-artifact run
```

`kernel.config` can be a defconfig target or a `.config` file. Extra Kconfig
fragments, `CONFIG_*` options, make targets and arbitrary make arguments are
configured in `[kernel]` in `vmgen.toml`. Set `kernel.initrd` to use your own
initrd instead of the generated busybox one.

## Diagnostics

```sh
simp-vmgen diagnose                 # base checks, or infer from vmgen.toml
simp-vmgen diagnose --profile kernel
simp-vmgen diagnose --json
```

`diagnose` checks QEMU, image tooling, SSH, cloud-init seed tools, kernel build
dependencies, `/dev/kvm`, cache permissions, disk space and configured ports.
It exits non-zero when a required tool is missing.

## Troubleshooting

- **`wait` times out**: run `simp-vmgen log -f` and inspect
  `.simp-vmgen/run/serial.log`; the readiness marker is
  `SIMP_VMGEN_READY`.
- **Port already in use**: use `--ssh-port auto` / `--gdb-port auto`, or stop
  the other VM.
- **Slow boot**: check `diagnose`; without `/dev/kvm` QEMU falls back to TCG.
- **`regen` reports a conflict**: the file was edited locally. Move the change
  into `vmgen.toml` or run `simp-vmgen regen --force`.
- **Kernel artifact cannot mount `/work`**: the supplied kernel needs 9p
  support. Source builds enable `CONFIG_9P_FS` and virtio by default; for an
  external kernel, provide a matching initrd/modules or use a kernel built with
  those options.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv run ruff format --check .
```

Opt-in integration tests:

```sh
SIMP_VMGEN_SLOW=1 uv run pytest -m slow      # boots real QEMU guests
SIMP_VMGEN_INSTALL_TEST=1 uv run pytest -m install  # uv tool install smoke test
```
