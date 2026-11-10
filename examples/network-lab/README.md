# Network lab example

A three-node lab on one shared experimental LAN:

```text
attacker (192.168.77.10)
target   (192.168.77.20)
observer (192.168.77.30)
```

All three services are independent `simp-vmgen` projects. The compose file
only adds the shared network and lifecycle. The management NIC of each VM
remains on QEMU user-mode networking for SSH and outbound package downloads.

## Start

```sh
simp-vmgen compose validate
simp-vmgen compose show
simp-vmgen compose up --timeout 300
simp-vmgen compose status --json
```

`compose show --json` prints the MAC and multicast endpoint of every service.
The experimental interface name inside a guest depends on the guest and its
cloud-init renderer; do not assume `eth1`:

```sh
simp-vmgen compose guest attacker -- ip -brief addr
simp-vmgen compose guest target -- ip -brief addr
simp-vmgen compose guest observer -- ip -brief addr
```

Match the interface whose MAC equals the one shown by `compose show --json`.
The management interface normally has a `10.0.2.x` address; the lab interface
has `192.168.77.x`.

## Observe traffic

The default socket backend behaves like a shared hub. The observer can use
promiscuous capture to see unicast traffic between the other two guests:

```sh
# Terminal 1: capture for 20 seconds
simp-vmgen compose guest observer -- \
    timeout 20 tcpdump -n -i <lab-interface>

# Terminal 2: generate traffic
simp-vmgen compose guest attacker -- ping -c 3 192.168.77.20
simp-vmgen compose guest attacker -- sh -c \
    'nc -u -w1 192.168.77.20 9999 <<< "hello"'
```

`tcpdump` enables promiscuous mode by default. Expect ARP, ICMP and the UDP
packets to appear on the observer. This is a hub-like medium, not a switched
network: it is useful for packet analysis and classroom attack simulations, but
it does not reproduce bridge MAC learning.

Swap the network driver to use a Linux bridge instead:

```toml
[networks.lan]
driver = "bridge"
subnet = "192.168.77.0/24"
```

```sh
sudo simp-vmgen compose network prepare
simp-vmgen compose up --timeout 300
simp-vmgen compose network status
```

A bridge is a switch: the observer normally sees broadcasts/ARP but not
unicast traffic between attacker and target. To observe or inject unicast
traffic, use an explicit ARP-spoofing exercise, a mirror port, or keep the
socket backend.

Bridge setup requires `iproute2`, `/dev/net/tun`, a configured
`qemu-bridge-helper` allowlist and root or `CAP_NET_ADMIN` for
`network prepare/cleanup`. The tool never runs `sudo` for you and never deletes
an external bridge.

## Attack-simulation notes

The lab is intentionally a flat, unswitched network. Typical exercises are:

- ARP request/reply observation and ARP cache manipulation;
- ICMP/UDP/TCP traffic generation and capture;
- protocol fuzzing or scanning within the lab subnet.

The observer only receives traffic if the medium allows it. On the socket
backend, a promiscuous observer can see the shared medium; on a bridge, use
explicit attack or mirroring techniques. Add tools with
`provision.extra_packages` or install them inside the guests.

This example uses the local-only defaults (`root`/`114514`, SSH bound to
`127.0.0.1`). Do not expose these VMs to an untrusted network.

## Stop and clean up

```sh
simp-vmgen compose logs observer --lines 80
simp-vmgen compose stop
simp-vmgen compose down
sudo simp-vmgen compose network cleanup   # bridge backend only
```

`stop` keeps disks and endpoints. `down` clears markers and releases socket
state; owned bridges remain until the privileged cleanup command.
