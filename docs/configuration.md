# Configuration reference

`simp-vmgen` reads `vmgen.toml` from the project root. The generated
`Makefile` is only a wrapper; edit `vmgen.toml` and run `make regen`.

## Minimum example

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

[ssh]
user = "root"

[[mount]]
source = "work"
target = "/work"

[[users]]
name = "root"
password = "114514"
```

## Project and distro

| Key | Meaning |
|---|---|
| `project.name` | Project and hostname base |
| `project.description` | Optional description |
| `distro.name` | `ubuntu` or `alpine` |
| `distro.release` | e.g. `24.04`, `3.22` |
| `distro.arch` | `x86_64` |
| `distro.image` | Optional image URL override |
| `distro.image_checksum` | Optional `sha256:...` / `sha512:...` |

## VM resources

| Key | Meaning |
|---|---|
| `vm.mem` | Guest RAM, e.g. `2G` |
| `vm.cpus` | vCPU count |
| `vm.disk` | qcow2 disk size |
| `vm.accel` | `auto`, `kvm` or `tcg` |
| `vm.display` | `none`, `gtk`, `sdl` or `vnc` |
| `vm.machine` | Optional machine override |
| `vm.snapshot` | Discard writes on exit |

## SSH and GDB

| Key | Meaning |
|---|---|
| `ssh.enabled` | Enable SSH forwarding and helpers |
| `ssh.host` | Host bind address, default `127.0.0.1` |
| `ssh.port` | Port number or `"auto"` |
| `ssh.user` | Default login account |
| `ssh.key` | Private key path, default `.simp-vmgen/ssh/id_ed25519` |
| `ssh.permit_root_login` | Allow root SSH login |
| `ssh.wait_timeout` | Default `wait` timeout |
| `gdb.enabled` | Expose the QEMU gdb stub |
| `gdb.port` | Port number or `"auto"` |
| `gdb.wait` | Add `-S` and wait for GDB |

## Users

```toml
[[users]]
name = "alice"
password = "secret"
shell = "bash"
sudo = true
groups = ["docker"]
ssh_keys = ["ssh-ed25519 AAAA..."]
```

- `name` must be a valid lowercase Unix user.
- `password` may be empty when the generated key or another key is used.
- `shell` can be a name (`bash`, `fish`, `sh`) or an absolute path.
- `ssh_keys` accepts literal public keys or paths to public key files.
- `ssh.user` must name one of the configured users.

## Mounts

```toml
[[mount]]
source = "work"
target = "/work"
readonly = false
```

- Relative sources resolve from the project root.
- Targets must be absolute.
- Each mount becomes a 9p share with a stable `host0`, `host1`, ... tag.
- `readonly = true` sets the 9p share and guest mount to read-only.

## Provisioning

| Key | Meaning |
|---|---|
| `provision.timezone` | Guest timezone |
| `provision.package_sets` | Distro package sets such as `base`, `dev`, `kernel` |
| `provision.extra_packages` | Additional guest packages |
| `provision.extra_user_data` | Extra raw cloud-init user-data appended to the seed |

## Kernel: build mode

```toml
[kernel]
enabled = true
mode = "build"
version = "6.12.5"          # or use commit/repo
config = "defconfig"        # target or path to a .config
fragments = ["debug.config"]
options = { CONFIG_KASAN = "y" }
targets = ["bzImage", "modules"]
make_args = ["LLVM=1"]
jobs = 0                    # 0 = nproc
rootfs = "busybox"
```

- `version` uses kernel.org tarballs and checksums.
- `commit` + `repo` fetches a pinned git commit.
- `make_args` passes arbitrary make variables/options.
- A VM-friendly set of 9p/virtio/serial options is applied before user
  fragments and options, so explicit user values win.
- Interactive `menuconfig` is intentionally not supported.

## Kernel: artifact mode

```toml
[kernel]
enabled = true
mode = "artifact"
artifact = "/boot/vmlinuz-linux"
artifact_type = "vmlinuz"   # bzImage | vmlinuz | vmlinux | Image
initrd = ""                 # empty = generated busybox initramfs
append = "loglevel=7"
rootfs = "busybox"
```

Artifact mode does not fetch or build kernel sources. It boots the supplied
kernel with either the configured initrd or a generated rootfs.

## Kernel rootfs plugins

`kernel.rootfs` selects a rootfs plugin:

| Value | Description |
|---|---|
| `busybox` | Minimal busybox initramfs |
| `alpine` | Alpine minirootfs provisioned with OpenRC and OpenSSH |
| `none` | Use `kernel.initrd` as-is |

Alpine rootfs sources, packages and templates are defined in
`src/simp_vmgen/rootfs/alpine/`. The plugin is auto-discovered, like distros.
`kernel rootfs` fetches and provisions the rootfs; `kernel initramfs` packages
it as `initramfs.cpio.gz`. Users, passwords, sudo, SSH keys, networking and 9p
mounts are generated from the same `vmgen.toml` configuration as cloud images.

Rootfs plugin metadata supports:

- `kind`: `initramfs`, `disk` or `9p` (only `initramfs` is implemented today)
- `source_url` / checksum
- `packages`
- `templates`
- `init`
- `apk_tools_url` and checksum for Alpine package installation

## Lock file

`vmgen.lock` records the pinned distro image and kernel source/artifact. It is
created by `new` and updated by `build`, `kernel fetch` and kernel preparation.
Commit it to preserve reproducibility.

## Runtime layout

```text
.simp-vmgen/
├── generated.json          # generated-file hashes for regen
├── run/                    # pid, sockets, logs, overlay, seed
├── ssh/                    # generated SSH key
└── kernel/
    ├── src/                # kernel source or git tree
    ├── build/              # out-of-tree kernel build
    ├── modules/            # installed modules
    ├── initramfs/          # unpacked busybox rootfs
    └── initramfs.cpio.gz
```
