# Compose projects

A compose project describes several independent `simp-vmgen` projects and the
shared networks between them. It is a topology file; each service keeps its own
`vmgen.toml`, lock file, disk and kernel artifacts.

```text
network-lab/
├── vmgen-compose.toml
├── attacker/
│   └── vmgen.toml
├── target/
│   └── vmgen.toml
└── observer/
    └── vmgen.toml
```

```toml
schema = 1

[project]
name = "network-lab"

[networks.lan]
driver = "socket"             # currently the only driver
subnet = "192.168.77.0/24"
# mcast = "239.192.77.1:20000" # optional; allocated automatically

[services.attacker]
project = "./attacker"

[services.attacker.networks.lan]
address = "192.168.77.10/24"

[services.target]
project = "./target"

[services.target.networks.lan]
address = "192.168.77.20/24"
```

Switch the network backend without changing service addresses or MACs:

```toml
[networks.lan]
driver = "bridge"
subnet = "192.168.77.0/24"
# bridge = "br-existing"      # optional; otherwise compose owns a derived bridge
```

Run `sudo simp-vmgen compose network prepare` before the first bridge `up`, and
`sudo simp-vmgen compose network cleanup` after `down`.

## Commands

```sh
simp-vmgen compose validate
simp-vmgen compose show [--json]
simp-vmgen compose build [--force]
simp-vmgen compose up [--no-build] [--timeout SECONDS] [--json]
simp-vmgen compose status [--json] [--check]
simp-vmgen compose wait [--timeout SECONDS] [--json]
simp-vmgen compose guest SERVICE [--user NAME] [--timeout SECONDS] -- CMD
simp-vmgen compose logs SERVICE [-f] [--lines N] [--no-color]
simp-vmgen compose stop [--json]
simp-vmgen compose down [--json]
simp-vmgen compose network prepare
simp-vmgen compose network cleanup
simp-vmgen compose network status [--json]
```

- `validate` parses the compose file and every referenced service project. It
  does not allocate endpoints or write state.
- `show` resolves the effective topology, allocates socket endpoints if needed,
  persists them, and prints human or JSON output.
- `build` builds every service in declaration order.
- `up` builds if needed, starts every service in declaration order and waits for
  its readiness marker. Services that are already running are waited on instead
  of restarted.
- `status` reports each service independently. `--check` exits 3 unless every
  service is running.
- `wait` waits for the readiness marker of every service.
- `guest` and `logs` target one service; `logs -f` is interactive.
- `stop` stops every service but keeps endpoints, disks and compose membership
  markers.
- `down` stops every service, clears membership markers and releases the
  compose state and socket endpoints. Disks and guest overlays are preserved.
  Owned bridge devices are not deleted by unprivileged `down`; it keeps their
  state and prints the `network cleanup` command to run with `sudo`.
- `network prepare` creates bridges owned by this compose project. It needs
  root or `CAP_NET_ADMIN`.
- `network cleanup` deletes only bridges recorded as owned, after all services
  are stopped. It never deletes an external bridge.
- `network status` reports bridge existence, ownership, privilege state,
  `/dev/net/tun` and the QEMU bridge helper.

`up`, `status`, `wait`, `stop` and `down` support `--json` where shown above.
`up --json` emits a JSON error object on failure, including `state: "error"`,
an `error` message and per-service status. Missing compose/network
subcommands are usage errors and exit 2; operational failures exit 1.
`--dry-run` is intentionally rejected for compose commands; the lifecycle
mutates multiple services and does not have a meaningful single-preview mode.

Use `-C` to point at the compose directory or any directory below it. The
nearest directory containing `vmgen-compose.toml` is used.

## Lifecycle behavior

- Lifecycle commands call the existing single-VM runtime functions with a
  materialized `Config`; they do not shell out or duplicate build/run logic.
- A compose lock serializes `build`, `up`, `stop` and `down`. `status`, `wait`,
  `guest` and `logs` can observe a running compose project.
- If `up` fails partway through, services started by that invocation are stopped
  and their membership markers are cleared. Services that were already running
  before the invocation are left alone.
- If `up` is interrupted with Ctrl-C, services started by that invocation are
  stopped and their markers are cleared; previously running services are left
  alone.
- If `down` cannot stop a service, it keeps the state file, registry entries and
  membership markers so the next `down` or `stop` can retry.
- `down` can use the saved state when `vmgen-compose.toml` has been removed or
  is temporarily invalid, as long as the state file still records service roots.
- Bridge ownership is persisted in the compose state. `down` keeps state when
  owned bridges remain so `network cleanup` can find them; cleanup removes the
  state after the last owned bridge is gone.
- Privileged `network prepare/cleanup` restores state/lock ownership to
  `SUDO_UID`/`SUDO_GID` so later unprivileged compose commands keep working.
  It does not allocate socket endpoints or write a root-owned endpoint registry.
- A service whose process disappeared is reported as stopped; `up` starts it
  again and repeated `up`/`down` calls are idempotent.
- `up` starts services sequentially and does not wait for application-level
  health checks.

## Example

The repository includes a three-node attacker/target/observer lab:

```sh
simp-vmgen -C examples/network-lab compose validate
simp-vmgen -C examples/network-lab compose up --timeout 300
simp-vmgen -C examples/network-lab compose guest attacker -- \
    ip -brief addr
```

See [../examples/network-lab/README.md](../examples/network-lab/README.md) for
capture commands, interface discovery, traffic visibility between socket and
bridge backends, and bridge preparation.

## Service projects

- `project` is resolved relative to the compose file and must contain
  `vmgen.toml`.
- Every service project is loaded and validated, including its distro and
  release.
- A service project must not define its own `[[network]]` tables; compose owns
  the experimental NICs for managed services.
- Service project names must be unique, and service project directories cannot
  be referenced twice.
- Fixed SSH/GDB ports must be unique across services; `"auto"` remains the
  recommended setting.
- Service networks use static IPv4 addresses. Addresses must be inside the
  compose network subnet, must use the same prefix length as the subnet, and
  cannot be the network or broadcast address. Host addresses must be unique
  even if written with different prefixes.

## Networks and endpoints

- Each compose network has one IPv4 subnet. Overlapping subnets are rejected.
- `driver = "socket"` uses the rootless QEMU socket multicast backend described
  in [configuration.md](configuration.md#experimental-networks). An explicit
  `mcast` is used as-is; otherwise the first `compose show` derives a stable
  multicast group and UDP port and records it in
  `.simp-vmgen/compose/state.json`.
- `driver = "bridge"` uses a Linux bridge. With no `bridge` key, `network
  prepare` derives and creates an owned bridge. With `bridge = "name"`, the
  bridge must already exist and is never deleted by compose.
- `network prepare` refuses to claim a pre-existing bridge that it did not
  record as owned; set `bridge = "name"` to use such a bridge explicitly.
- QEMU needs `qemu-bridge-helper`, `/dev/net/tun` and the helper allowlist
  configured on the host. Run `compose network status` to inspect readiness;
  it reports `/etc/qemu/bridge.conf` entries and whether each bridge is allowed.
- Removing `bridge = "..."` from a network that previously used an external
  bridge makes compose derive a new owned bridge. The external bridge is never
  claimed or deleted.
- Endpoint ownership is tracked in a small registry under the user cache
  directory, guarded by a file lock. A project reuses its persisted endpoint;
  different projects are kept on different endpoints. Registry entries whose
  project state has disappeared are reclaimed.
- The state file contains runtime endpoint assignments, the service list and
  the resolved service roots. It is ignored by git and can be removed to force
  reallocation; `compose down` removes it after all services stop.
- MAC addresses are derived from the service project name and network name, or
  can be set explicitly. Derived MACs assume service project names are unique
  on the same segment.

## Traffic visibility

- Socket multicast behaves like a shared hub. A promiscuous observer can see
  unicast frames between other VMs, which is useful for capture and classroom
  network-attack exercises. It has no MAC-learning/forwarding-database
  behavior and is not an access-control boundary between host processes.
- A Linux bridge behaves like a switch with MAC learning. An observer normally
  sees broadcasts and ARP, but not unicast traffic between other ports unless
  you use an explicit attack, mirror port or similar technique.
- Every service keeps a separate user-mode management NIC. SSH and outbound
  package downloads use that path, not the lab LAN. A VM is not fully isolated
  from external networks just because it has an experiment NIC.
- Capture tools must exist inside the guest. Add them through
  `provision.extra_packages` or install them after boot.

`depends_on`, readiness ordering, application health checks and cross-host
networking are not implemented yet.

## Standalone safety

A service managed by compose is marked in
`<service>/.simp-vmgen/compose-member.json` when `compose up` activates it. A
normal `simp-vmgen run` in that directory refuses to start the service unless
`simp-vmgen compose ...` is driving it. `compose stop` keeps the marker;
`compose down` removes it.

`compose show` does allocate and persist endpoints. If you need a read-only
check, use `compose validate`.
