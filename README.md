# simp-vmgen

Generate reproducible QEMU lab environments from a small TOML project file.

`simp-vmgen` is a thin, installable CLI. A generated project contains only
`vmgen.toml`, a `Makefile` that forwards to `simp-vmgen`, and a generated
`README.md`; all build and runtime logic lives in the installed tool.

## Status

Under active development. See [TODO.md](TODO.md) for the current progress.

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

Planned distro support: Ubuntu 24.04 and Alpine 3.22. Planned kernel rootfs
support starts with a static busybox initramfs.

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

## Diagnostics

```sh
simp-vmgen diagnose                 # base checks, or infer from vmgen.toml
simp-vmgen diagnose --profile kernel
simp-vmgen diagnose --json
```

`diagnose` checks QEMU, image tooling, SSH, cloud-init seed tools, kernel build
dependencies, `/dev/kvm`, cache permissions, disk space and configured ports.
It exits non-zero when a required tool is missing.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
```
