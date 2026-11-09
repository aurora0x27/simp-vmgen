# M0: rootless QEMU socket LAN experiment

## Decision

The socket multicast backend passes the initial feasibility gate on the tested
Linux/QEMU host. Proceed with a rootless socket backend for compose, retaining
Linux bridge/TAP as the alternative for switching semantics. This is an
experiment, not a supported compose API or a minimum-QEMU-version declaration.

The probe changes QEMU argv only inside its own process using a temporary patch;
production configuration, runtime and guest templates are unchanged.

## Reproduce

From a source checkout:

```sh
uv run python tools/socket_lan_probe.py --directory /tmp/opencode/socket-m0
```

Use a new directory for each run. Requires Linux, `ip`, QEMU, cloud-init seed
tooling and the normal simp-vmgen cloud-image prerequisites. The Alpine guests
install `iproute2`, `tcpdump` and `iperf3`, requiring mirror access through their
separate user-mode management NICs. No host root privileges are used. Allow
approximately 3 GiB of guest memory plus QEMU overhead and image/overlay space.

The tool retains disks and diagnostics in the supplied directory and stops all
four VMs in `finally`. Inspect `cleanup_errors` in `results.json`; a successful
traffic check does not excuse a cleanup error. Logs can contain generated lab
credentials and should not be committed. The script is not installed in wheels.

Artifacts:

- `argv-*.json`: actual QEMU commands;
- `commands.log`: guest commands and their output;
- `results.json`: measurements and assertions completed before cleanup;
- per-project serial logs, overlays and configuration.

## Topology and parameters

```text
LAN A: node1 -- node2 -- node3 (observer)
LAN B:                  node3 -- node4
```

The links denote shared media, not point-to-point cables. Node3 has an interface
on each LAN but is not configured to bridge/route between them. Node4 additionally
has an address in LAN A's IP subnet to test that matching IP subnets alone do not
cross the multicast group boundary.

Each VM has a management NIC plus an experimental NIC (node3 has two).
Example experimental arguments:

```text
-netdev socket,id=lab,mcast=239.192.77.1:PORT,localaddr=127.0.0.1
-device virtio-net-pci,netdev=lab,mac=52:54:00:77:00:01
```

LAN B uses `239.192.77.2` with the **same UDP port**, testing group separation.
The script reserves a dynamically selected UDP port for this run; this is not
the production compose allocation/locking design. Guests are started in order
3, 1, 4, 2. Static addresses are applied over SSH after provisioning; future
production code must match interfaces by MAC rather than assume `eth1/eth2`.

## Observed results (2026-11-10)

Host: Linux, UID 1000, QEMU 11.1.3, KVM. Four Alpine 3.22 cloud-image guests,
each with 768 MiB RAM and one vCPU. Two complete runs passed; the final run added
per-physical-interface multicast observation.

| Check | Final run result |
|---|---|
| LAN A connectivity | All six directed pairs passed ARP/ping, TCP and UDP |
| LAN B connectivity | node3/node4 ping passed in both directions |
| Cross-group isolation | node1/node4 ping failed as expected despite overlapping IP subnet |
| Observer capture | node3 saw node1/node2 ARP request/reply and ICMP request/reply |
| TCP load | Six sequential 2-second iperf3 streams: receiver throughput 1.038–1.169 Gbit/s |
| UDP load | Six sequential 2-second streams at configured 10 Mbit/s: 0% reported packet loss |
| MTU | 20 pings with 1472-byte payload passed at MTU 1500 |
| Fragmentation | Three pings with 4000-byte payload passed; not a jumbo-frame test |
| Restart | node1/node3 communicated while node2 was stopped; node2 rejoined after restart |
| Cleanup | All four VMs stopped, no cleanup errors |

These short sequential runs establish functionality, not a sustained-load or
multi-tenant performance guarantee. First-run TCP results were 0.815–1.073 Gbit/s;
throughput is sensitive to host scheduling and workload.

### Host interface evidence

An unprivileged multicast receiver used Linux `IP_PKTINFO` ancillary data to
observe a QEMU packet arriving on **lo**, with source address **127.0.0.1**.
Additional receivers joined the same group/port specifically on physical IPv4
interfaces, with `IP_MULTICAST_ALL=0`; the active physical interface (`wlo1`)
received **zero experiment datagrams** during the final run.

This is interface-specific UDP observation, **not a privileged AF_PACKET capture
or external wire capture**. It supports the loopback-only design on this host;
it does not establish isolation under every host routing/firewall configuration.
Host traffic unrelated to the experiment, including management NAT package
downloads and multicast membership control messages, is not claimed absent.

## Constraints carried into M1/M2

- Multicast acts like a shared hub: a promiscuous third guest sees unicast frames.
  Use bridge/TAP when MAC learning/FDB behavior is part of the experiment.
- Group/port separation is not authentication or an access-control boundary
  against other host processes. Do not advertise tenant security isolation.
- Production allocation must coordinate endpoint ownership and concurrent runs;
  UDP reuse makes a simple free-port probe insufficient.
- Management NAT remains an independent egress path. An isolated experiment NIC
  does not make the entire guest isolated from external networks.
- Only Alpine cloud guests on this QEMU version were tested here. Ubuntu,
  busybox and custom-kernel networking belong to M1 and later integration tests.
- No compose commands or configuration schema are implemented by M0.
