# AGENTS.md

Guidance for developers and coding agents working on `simp-vmgen`.

## What this project is

`simp-vmgen` generates reproducible QEMU lab environments from a small
`vmgen.toml` file.

The tool is intentionally a **thin wrapper**: generated projects contain a
`Makefile` and documentation, but all build/runtime logic lives in the installed
`simp-vmgen` package. This keeps generated labs small and lets fixes ship by
upgrading the tool.

### Scope

Supported:

- Ubuntu 24.04 and Alpine 3.22 cloud images
- cloud-init provisioning, SSH, serial console, 9p volumes
- multiple experimental NICs on rootless QEMU socket LANs or Linux bridges
- compose projects for building and running multiple VMs together
- multiple users, root/password/key policy
- kernel source fetch from a version or git commit
- non-interactive kernel configuration and builds
- booting an existing `bzImage`, `vmlinuz` or `vmlinux`
- a minimal busybox initramfs rootfs

Out of scope:

- interactive kernel development workflows (`menuconfig`, patches, IDE setup)
- multi-VM scheduling beyond compose (`depends_on`, cross-host, app health)
- non-x86 architectures
- automatic module/initrd synthesis for externally supplied kernels
- replacing tools such as `virtme-ng`

## Quick start

```sh
uv sync
uv run simp-vmgen --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Install as a user tool:

```sh
uv tool install .
```

## Architecture map

```text
src/simp_vmgen/
├── cli.py            argparse CLI and dispatch
├── config.py         vmgen.toml dataclasses, validation, serialization
├── project.py        new / regen / show
├── wizard.py         interactive `new -i`
├── paths.py          project discovery
├── render.py         generated Makefile/README/.gitignore
├── manifest.py       generated-file hashes for regen conflicts
├── lock.py           vmgen.lock (image + kernel pins)
├── cache.py          global cache, resumable downloads, checksums
├── distros/          distro metadata and cloud-init templates
│   ├── base.py       distro loader/validator
│   ├── ubuntu/
│   └── alpine/
├── seed.py           cloud-init rendering and NoCloud ISO creation
├── users.py          user/SSH-key provisioning data
├── mounts.py         resolved 9p mounts
├── network.py        resolved experimental NICs and cloud-init network config
├── compose.py        compose topology, endpoint registry and service state
├── compose_runtime.py compose lifecycle over the single-VM runtime
├── compose_network.py privileged bridge prepare/cleanup/status
├── sshkeys.py        key generation and askpass helper
├── net.py            free-port allocation
├── layout.py         `.simp-vmgen/` paths
├── state.py          allocated ports and running PID
├── stamp.py          build stamps for stale artifact detection
├── qemu.py           QEMU argv construction
├── runtime.py        build/run/stop/status/ssh/wait/serial/console/log/clean
├── runner.py         streaming subprocesses and persistent build logs
├── attach.py         serial/monitor socket proxy and log tailing
├── kernel_source.py  tarball/git source acquisition
├── kernel.py         Kconfig, build, artifact resolution, kernel status
├── initramfs.py      busybox/Alpine initramfs builder
├── alpine.py         Alpine rootfs provisioning and package installation
├── rootfs/           discoverable rootfs plugins (busybox, alpine)
├── rootfs_source.py  rootfs archive fetch/extract and lock records
├── diagnose.py       host dependency checks
├── errors.py         expected user-facing exceptions
└── templates/        generated project and initramfs templates
```

Generated project layout:

```text
lab/
├── vmgen.toml
├── vmgen.lock
├── Makefile
├── README.md
├── .gitignore
├── work/
└── .simp-vmgen/
    ├── generated.json
    ├── run/
    ├── ssh/
    └── kernel/
```

## Core invariants

- `vmgen.toml` is the source of truth.
- Generated files are thin wrappers; do not move lifecycle logic into them.
- `vmgen.lock` pins remote artifacts. Update it when source resolution changes.
- `.simp-vmgen/generated.json` records generated-file hashes. `regen` must not
  silently overwrite locally modified files; require `--force`.
- `.simp-vmgen/` is ignored runtime state and must never be required for source
  control.
- Keep default behavior non-interactive. Interactive behavior belongs in the
  wizard.

### Config invariants

- `schema = 1`.
- `ssh.user` must exist in `[[users]]`.
- `ssh.permit_root_login = false` is invalid when `ssh.user = "root"`.
- Mount targets must be absolute and unique; source paths containing commas are
  rejected because QEMU `-virtfs` uses commas as separators.
- `provision.package_sets = []` is meaningful and must survive TOML round-trips.
  Do not restore the default `["base"]` on load.
- `kernel.mode = "artifact"` is inferred when `kernel.artifact` is set.
- Kernel `version` and `commit` are mutually exclusive.
- Kernel `make_args` are passed through to every relevant `make` invocation.

### Compose invariants

- `vmgen-compose.toml` references independent service projects; compose owns
  their experimental NICs. A service `vmgen.toml` must not define `[[network]]`
  when referenced by compose.
- Compose state lives under `<compose>/.simp-vmgen/compose/state.json`.
  Socket endpoints are also registered in the user cache with a file lock;
  bridge ownership is recorded in compose state.
- Mutating lifecycle commands (`build`, `up`, `stop`, `down`) hold the compose
  lock. `up` writes per-service membership markers; standalone `run` refuses to
  start a marked service.
- `down` releases socket state and clears markers, but owned bridges remain
  until `compose network cleanup`. External bridges are never deleted, and a
  derived bridge name is only reused when the previous state recorded it as
  owned.
- `down` must recover from a missing/invalid compose file by using saved state
  service roots.
- Compose service addresses must use the compose network prefix; duplicate host
  addresses are rejected regardless of the written prefix.
- Bridge preparation is explicit and privileged; the CLI never invokes `sudo`
  automatically. Privileged network commands must not allocate socket endpoints
  and must restore state/lock ownership to `SUDO_UID`/`SUDO_GID`.
- The single-VM runtime accepts an internal `config_override` used by compose.
  It must remain equivalent to loading the same effective config from disk.

## Generated project behavior

`new` writes:

- `vmgen.toml`
- `vmgen.lock`
- `Makefile`
- `README.md`
- `.gitignore`
- relative mount source directories
- `.simp-vmgen/generated.json`

`regen`:

- re-renders from `vmgen.toml`
- updates files that still match their generated hash
- preserves locally edited files and reports conflicts
- `--check` reports drift without writing
- `--force` overwrites conflicts

The generated `Makefile` only forwards to `simp-vmgen`; keep it that way.

## Key runtime design

- Build artifacts live under `.simp-vmgen/run/` (cloud image) and
  `.simp-vmgen/kernel/` (kernel).
- `.simp-vmgen/build.json` records the config hash and artifact metadata.
  `run` refuses artifacts built from a different configuration; `build` refreshes
  them. Changing `vm.disk` or the base image requires `build --force` because it
  recreates the overlay and discards guest state.
- Long-running commands stream their output and append it to files under
  `.simp-vmgen/logs/` (kernel build/config/source, Alpine packages). Failures
  include the log path.
- Port allocation happens at `run` time; fixed ports fail if busy, `"auto"`
  finds a free port.
- QEMU runs daemonized by default; foreground mode saves state too.
- Serial and monitor use Unix sockets.
- `wait` polls the serial log for the `SIMP_VMGEN_READY` marker. Do not treat
  early SSH success as readiness; cloud-init/provisioning may still be running.
- Cloud images: qcow2 overlay over the cached base image + NoCloud seed ISO.
- Kernel mode: `-kernel` + `-initrd` + `-append`, no cloud drives.
- Alpine provisioning runs in a detached session because OpenRC's `cloud-final`
  service has a short timeout. Provisioning output is in
  `/var/log/simp-vmgen-provision.log` inside the guest.

## Kernel workflow

### Build mode

- `version`: kernel.org tarball + `sha256sums.asc` verification.
- `commit` + `repo`: git fetch, falling back to a full fetch if shallow SHA
  fetch is unsupported.
- Builds are out-of-tree (`make O=.simp-vmgen/kernel/build`).
- `config` may be a `defconfig` target or a `.config` file.
- `fragments`, `options`, `targets`, `make_args`, `jobs` are configurable.
- VM-friendly Kconfig defaults (9p, virtio, devtmpfs, serial, gzip initramfs)
  are applied before user fragments/options. User values win.
- Installed modules are copied into the busybox initramfs when present.

### Artifact mode

- `artifact` + `artifact_type` are required.
- `initrd` overrides the generated busybox initramfs.
- `append` is appended to `console=ttyS0 panic=-1` (and `rdinit=/init` when
  using the generated initramfs).
- External kernels need 9p support if mounts are configured.

### Source/rootfs limitations

- Rootfs plugins are auto-discovered from `src/simp_vmgen/rootfs/<name>/`.
- `busybox` and `alpine` rootfs plugins exist today; both are initramfs based.
- The Alpine plugin installs packages with a pinned `apk.static`, writes users,
  passwords, SSH keys and OpenRC runlevels, and packages the minirootfs.
- `vmlinux` uses QEMU's `-kernel` path; verify it with the target QEMU version.
- Do not add interactive `menuconfig` to the build pipeline. Generate config
  non-interactively instead.

## Adding a distro

1. Create `src/simp_vmgen/distros/<name>/distro.toml`.
2. Add `templates/user-data.j2` and `templates/meta-data.j2` under the same
   directory.
3. Register no Python code: distros are auto-discovered from the package.
4. Define at least:
   - `name`, `display_name`
   - `architectures`
   - `default_release`, `package_manager`, `init_system`
   - `default_shell`, `ssh_service`, `templates`
   - `package_sets.base`
   - one `[[release]]` with image URL/checksum URL and defaults
5. Add a golden fixture under `tests/golden/<name>/` and regenerate it from
   the same fixture config.

`uv_build` includes non-Python package files; verify the wheel contains new
metadata/templates with `uv build`.

## Testing

Default suite:

```sh
uv run pytest
```

Optional integration tests:

```sh
SIMP_VMGEN_SLOW=1 uv run pytest -m slow
SIMP_VMGEN_INSTALL_TEST=1 uv run pytest -m install
```

- Golden tests: `tests/golden/ubuntu`, `tests/golden/alpine`
- QEMU argv snapshots: `tests/golden/qemu`
- Slow tests boot real Alpine guests and a host kernel artifact.
- Install test builds a wheel and installs it into an isolated `uv tool` dir.

When templates or QEMU argv change, update the corresponding golden files.
Keep golden fixture configs stable; otherwise diffs become noisy.

## Development conventions

- Python 3.11+.
- Use `uv` for environments and dependency changes.
- Format with `ruff format`; lint with `ruff check`; type-check `src` with mypy.
- Tests use pytest.
- Prefer small, focused commits.
- Do not push unless explicitly asked.
- User-facing failures should raise `SimpVmgenError` subclasses and be rendered
  by `cli.main`; do not print tracebacks for expected errors.
- Avoid putting credentials in generated shell scripts. SSH keys are generated
  locally; password fallback uses a short-lived askpass helper.
- Keep the generated project self-documenting: update
  `templates/README.md.j2` when behavior changes.

## Agent scratch space

- `.agent/` is a gitignored local scratch directory for agent notes, audits,
  plans and handoff context. It is not part of the project.
- Multiple agents working in the same checkout share the same `.agent/`.
  Each git worktree gets its own `.agent/`; do not assume it is shared across
  worktrees.
- Keep durable decisions and user-facing documentation in `AGENTS.md`,
  `README.md` or `docs/`, not only in `.agent/`.
- Do not put credentials or private keys in `.agent/`; ignored files are not
  secure by definition.

## Common pitfalls

- Cloud-init seed changes alter the computed `instance-id`; rebuild the seed.
- `regen` conflicts mean a generated file was edited locally.
- Removing `provision.package_sets` from TOML restores the default `["base"]`;
  an explicit empty array is different.
- Alpine package installs can outlive `cloud-final`; keep readiness in the
  detached provisioning script.
- QEMU `-virtfs` is sensitive to commas in host paths.
- `/dev/kvm` availability changes the generated argv (`kvm/host` vs `tcg/max`).
- The cache is global (`$XDG_CACHE_HOME/simp-vmgen`); tests must not depend on
  a cold network download.

## Current state

The original single-VM plan and compose M0–M5 are implemented. Compose includes
socket and bridge networking, lifecycle commands, privileged bridge management
and the `examples/network-lab` topology. The compose work is unreleased; the
latest release is 0.2.0. There is no tracked TODO file; agent scratch context
lives in `.agent/` (gitignored). Treat this document, the README,
`docs/configuration.md`, `docs/compose.md`, and the tests as the maintained
source of truth.
