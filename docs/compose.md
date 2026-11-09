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

`up`, `status`, `wait`, `stop` and `down` support `--json` where shown above.
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
- If `down` cannot stop a service, it keeps the state file, registry entries and
  membership markers so the next `down` or `stop` can retry.
- A service whose process disappeared is reported as stopped; `up` starts it
  again and repeated `up`/`down` calls are idempotent.
- `up` starts services sequentially and does not wait for application-level
  health checks.

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
  compose network subnet and cannot be the network or broadcast address.

## Networks and endpoints

- Each compose network has one IPv4 subnet. Overlapping subnets are rejected.
- `driver = "socket"` uses the rootless QEMU socket multicast backend described
  in [configuration.md](configuration.md#experimental-networks).
- An explicit `mcast` is used as-is. Otherwise the first `compose show` derives
  a stable multicast group and UDP port and records it in
  `.simp-vmgen/compose/state.json`.
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

Bridge/TAP, `depends_on`, readiness ordering, application health checks and
cross-host networking are not implemented yet.

## Standalone safety

A service managed by compose is marked in
`<service>/.simp-vmgen/compose-member.json` when `compose up` activates it. A
normal `simp-vmgen run` in that directory refuses to start the service unless
`simp-vmgen compose ...` is driving it. `compose stop` keeps the marker;
`compose down` removes it.

`compose show` does allocate and persist endpoints. If you need a read-only
check, use `compose validate`.
