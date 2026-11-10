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

The generated defaults use a `root` account with password `114514`, timezone
`Asia/Shanghai`, and SSH bound to `127.0.0.1`. Change these for shared hosts.

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

## Experimental networks

Extra network interfaces let a VM join a shared lab network. Each `[[network]]`
table adds one NIC after the normal user-mode management NIC:

```toml
[[network]]
name = "lan"
address = "192.168.77.10/24"
gateway = "192.168.77.1"          # optional
mcast = "239.192.77.1:4789"        # socket backend

[[network]]
name = "back"
dhcp = true
bridge = "br-lab"                  # bridge backend
```

- `name` must be a valid interface name: lowercase letter followed by lowercase
  letters, digits, `_` or `-`, at most 15 characters. `lo`, `eth0` and `mgmt`
  are reserved.
- Set exactly one backend: `mcast` or `bridge`.
- `mcast` is a QEMU socket UDP multicast endpoint. All VMs using the same
  endpoint share one Ethernet segment. The transport is restricted to
  `127.0.0.1`, so it stays on the host and requires no root privileges.
- `bridge` attaches the NIC to an existing Linux bridge. Creating the bridge and
  allowing QEMU's `qemu-bridge-helper` to use it are host setup steps; they are
  not done automatically. Compose projects can create and remove their own
  bridges through the explicit `compose network prepare/cleanup` commands.
- Use either a static `address` (IPv4 CIDR) or `dhcp = true`; they are mutually
  exclusive. IPv4 is supported; IPv6 is not configured yet.
- `gateway` is optional, must belong to the static subnet, and only one
  experimental network may set it. It becomes a default route and may override
  the management NIC's DHCP route; omit it unless the VM is meant to route
  through the lab network.
- `mac` may be set explicitly. When omitted, a stable locally administered MAC
  is derived from the project and network name. Network names, MACs, addresses
  and multicast endpoints must be unique within a project. Derived MACs assume
  project names are unique on the same segment; set `mac` explicitly if you
  clone a project.
- The normal management NIC is unchanged and still provides SSH and outbound
  access through QEMU user-mode networking. Experiment traffic uses the extra
  interfaces.
- Cloud-init guests receive a NoCloud `network-config` file that matches
  interfaces by MAC. It also keeps the management NIC on DHCP using a stable
  management MAC. The Alpine rootfs renames interfaces to their network name
  during init and configures them with ifupdown-ng. The busybox rootfs renames
  them and configures addresses with `ip`.
- Kernel builds enable `CONFIG_VIRTIO_NET`, `CONFIG_INET`, `CONFIG_PACKET` and
  `CONFIG_UNIX` by default. External kernel artifacts and
  `kernel.rootfs = "none"` are not modified: if the kernel lacks virtio-net or
  the matching modules, the extra NICs will not appear. Configure networking
  inside those images yourself.

Socket multicast is the rootless default and is suitable for most lab traffic
and capture exercises. A Linux bridge provides switch semantics (MAC learning
and a forwarding database) but needs a prepared bridge and host permissions.
Compose can orchestrate either backend; see [compose.md](compose.md).

## Provisioning

| Key | Meaning |
|---|---|
| `provision.timezone` | Guest timezone |
| `provision.package_sets` | Distro package sets such as `base`, `dev`, `kernel` |
| `provision.extra_packages` | Additional guest packages |
| `provision.extra_user_data` | Extra raw cloud-init user-data appended to the seed |

`provision.package_sets = []` explicitly disables the distro default package
set. Removing the key restores the default `["base"]`; an empty array is not the
same as omitting it.

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
Commit it to preserve reproducibility. `build` and kernel preparation can
rewrite it, so review the diff after those commands.

## Runtime layout

```text
.simp-vmgen/
├── generated.json          # generated-file hashes for regen
├── build.json              # config hash and artifact metadata
├── run/                    # pid, sockets, serial.log, qemu.log, overlay, seed
├── ssh/                    # generated SSH key
├── logs/                   # build, download and provisioning logs
└── kernel/
    ├── src/                # kernel source or git tree
    ├── build/              # out-of-tree kernel build
    ├── modules/            # installed modules
    ├── initramfs/          # unpacked busybox rootfs
    └── initramfs.cpio.gz
```

`build.json` records the configuration used to build the runtime artifacts.
`run` refuses artifacts that do not match the current `vmgen.toml`; run
`build` to refresh them. Changing `vm.disk` or the base image requires
`build --force`, which recreates the overlay and discards guest state.
