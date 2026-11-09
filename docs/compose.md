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
simp-vmgen compose show
simp-vmgen compose show --json
```

- `validate` parses the compose file and every referenced service project. It
  does not allocate endpoints or write state.
- `show` resolves the effective topology, allocates socket endpoints if needed,
  persists them, and prints human or JSON output.

Use `-C` to point at the compose directory or any directory below it. The
nearest directory containing `vmgen-compose.toml` is used.

Lifecycle commands (`build`, `up`, `status`, `stop`, `down`) are not implemented
yet. This phase only provides parsing, validation, topology output and endpoint
state.

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
- The state file contains only runtime endpoint assignments and the service
  list. It is ignored by git and can be removed to force reallocation.
- MAC addresses are derived from the service project name and network name, or
  can be set explicitly. Derived MACs assume service project names are unique
  on the same segment.

Bridge/TAP, `depends_on`, readiness ordering, application health checks and
cross-host networking are not implemented yet.

## Standalone safety

A service managed by compose is marked in
`<service>/.simp-vmgen/compose-member.json` when compose activates it. A normal
`simp-vmgen run` in that directory refuses to start the service unless
`simp-vmgen compose ...` is driving it. The marker handling is in place for the
lifecycle implementation; `compose show` itself does not mark or start services.

`compose show` does allocate and persist endpoints. If you need a read-only
check, use `compose validate`.
