"""Opt-in M0 experiment: four real Alpine guests, no production networking API.

Run with: uv run python tools/socket_lan_probe.py --directory /tmp/opencode/socket-m0
Requires QEMU, KVM/TCG, cloud-init ISO tools and network access for guest packages.
The directory must not exist. All guests are stopped in finally; disks/logs remain.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simp_vmgen import runtime
from simp_vmgen.cli import main
from simp_vmgen.config import Config


def probe(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    roots = [directory / f"node{i}" for i in range(1, 5)]
    original = runtime.build_argv
    # Reserve a port for this experiment. Multicast reuse is intentionally not
    # a general-purpose compose allocator; concurrent probes use different ports.
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    receiver.bind(("", 0))
    port = receiver.getsockname()[1]
    groups = ["239.192.77.1", "239.192.77.2"]
    receiver.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(groups[0]) + socket.inet_aton("127.0.0.1"),
    )
    receiver.setsockopt(socket.IPPROTO_IP, 8, 1)  # Linux IP_PKTINFO
    receiver.settimeout(10)
    results: dict[str, object] = {"port": port, "groups": groups}
    physical_receivers: list[tuple[str, socket.socket]] = []

    def args(root: Path, **kw: object) -> SimpleNamespace:
        return SimpleNamespace(directory=str(root), **kw)

    def guest(index: int, command: str, timeout: int = 60) -> str:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "simp_vmgen",
                "-C",
                str(roots[index - 1]),
                "guest",
                "--timeout",
                str(timeout),
                "--",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 15,
        )
        with (directory / "commands.log").open("a") as log:
            log.write(f"node{index}: {command}\n{proc.stdout}{proc.stderr}\n")
        if proc.returncode:
            raise RuntimeError(f"node{index}: {command}: {proc.stderr} {proc.stdout}")
        return proc.stdout

    def argv(*a: object, **kw: object) -> list[str]:
        command = original(*a, **kw)
        index = int(a[0].project.name.removeprefix("node"))
        group = groups[0 if index <= 3 else 1]
        command += [
            "-netdev",
            f"socket,id=lab,mcast={group}:{port},localaddr=127.0.0.1",
            "-device",
            f"virtio-net-pci,netdev=lab,mac=52:54:00:77:00:{index:02x}",
        ]
        # Observer also joins the second LAN, giving it two participants.
        if index == 3:
            command += [
                "-netdev",
                f"socket,id=other,mcast={groups[1]}:{port},localaddr=127.0.0.1",
                "-device",
                "virtio-net-pci,netdev=other,mac=52:54:00:78:00:03",
            ]
        (directory / f"argv-{index}.json").write_text(json.dumps(command, indent=2))
        return command

    def configure(index: int) -> None:
        guest(index, f"ip addr replace 192.168.77.{index}/24 dev eth1; ip link set eth1 up")
        if index == 3:
            guest(index, "ip addr replace 192.168.78.3/24 dev eth2; ip link set eth2 up")
        if index == 4:
            guest(index, "ip addr replace 192.168.78.4/24 dev eth1")

    try:
        addresses = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr"], text=True))
        for interface in addresses:
            if not (Path("/sys/class/net") / interface["ifname"] / "device").exists():
                continue
            for address in interface["addr_info"]:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                physical_receivers.append((interface["ifname"], sock))
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
                sock.setsockopt(socket.IPPROTO_IP, 49, 0)  # Linux IP_MULTICAST_ALL
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(groups[0]) + socket.inet_aton(address["local"]),
                )
                sock.setblocking(False)
        results["qemu"] = subprocess.check_output(
            ["qemu-system-x86_64", "--version"], text=True
        ).splitlines()[0]
        for root in roots:
            assert (
                main(["new", str(root), "--distro", "alpine", "--mem", "768M", "--cpus", "1"]) == 0
            )
            config = Config.load(root / "vmgen.toml")
            config.provision.package_sets = []
            config.provision.extra_packages = ["iperf3", "tcpdump", "iproute2"]
            config.save(root / "vmgen.toml")
            runtime.build(args(root, dry_run=False, force=False))
        with patch.object(runtime, "build_argv", argv):
            for index in (3, 1, 4, 2):
                runtime.run(args(roots[index - 1]))
            for index, root in enumerate(roots, 1):
                runtime.wait(args(root, timeout=300))
                configure(index)
            for src in (1, 2, 3):
                for dst in (1, 2, 3):
                    if src != dst:
                        guest(src, f"ping -c 3 -W 2 192.168.77.{dst}")
            results["mesh_ping"] = "6 directed pairs passed"
            guest(3, "ping -c 3 -W 2 192.168.78.4")
            guest(4, "ping -c 3 -W 2 192.168.78.3")
            guest(1, "! ping -c 2 -W 1 192.168.77.4")
            guest(4, "! ping -c 2 -W 1 192.168.77.1")
            results["lan_isolation"] = "second LAN connected; cross-group same-subnet ping blocked"
            for src in (1, 2, 3):
                for dst in (1, 2, 3):
                    if src == dst:
                        continue
                    guest(dst, "iperf3 -s -D")
                    for mode in ("", "-u -b 10M"):
                        output = guest(src, f"iperf3 -c 192.168.77.{dst} -t 2 {mode} -J")
                        data = json.loads(output)
                        assert "error" not in data, data
                        results[f"iperf-{src}-{dst}-{mode or 'tcp'}"] = data["end"]
                    guest(dst, "pkill iperf3")
            # tcpdump defaults to promiscuous mode; a third guest sees unicast.
            with concurrent.futures.ThreadPoolExecutor() as pool:
                capture = pool.submit(
                    guest,
                    3,
                    "timeout 20 tcpdump -l -n -i eth1 -c 4 "
                    "'arp or (icmp and host 192.168.77.1 and host 192.168.77.2)' "
                    "> /tmp/probe.capture 2>&1",
                )
                # Readiness barrier avoids relying on a guessed sleep duration.
                guest(
                    3,
                    "timeout 10 sh -c 'until grep -q listening /tmp/probe.capture; "
                    "do sleep .1; done'",
                )
                guest(1, "ip neigh flush dev eth1; ping -c 4 -W 2 192.168.77.2")
                capture.result()
            results["observer_capture"] = guest(3, "cat /tmp/probe.capture")
            assert "ARP" in results["observer_capture"]
            assert "ICMP" in results["observer_capture"]
            guest(1, "ping -c 20 -i .05 -W 2 -s 1472 192.168.77.2")
            guest(1, "ping -c 3 -W 2 -s 4000 192.168.77.2")
            results["mtu"] = "1472-byte payload and fragmented 4000-byte payload passed at MTU 1500"
            # Ancillary data identifies the actual receiving interface, not a route guess.
            packet, ancillary, _, source = receiver.recvmsg(65535, 128)
            interfaces = [
                socket.if_indextoname(struct.unpack("I", data[:4])[0])
                for level, kind, data in ancillary
                if level == socket.IPPROTO_IP and kind == 8
            ]
            assert interfaces == ["lo"], interfaces
            assert source[0] == "127.0.0.1", source
            results["host_receive"] = {
                "interfaces": interfaces,
                "source": source,
                "bytes": len(packet),
            }
            runtime.stop(args(roots[1]))
            guest(1, "ping -c 3 -W 2 192.168.77.3")
            runtime.run(args(roots[1]))
            runtime.wait(args(roots[1], timeout=300))
            configure(2)
            guest(2, "ping -c 3 -W 2 192.168.77.1")
            guest(3, "ping -c 3 -W 2 192.168.77.2")
            results["restart"] = "survivors communicate; restarted peer rejoins"
            for name, sock in physical_receivers:
                try:
                    packet = sock.recv(65535)
                except BlockingIOError:
                    continue
                raise AssertionError(f"multicast received on physical interface {name}: {packet!r}")
            results["physical_memberships"] = {
                "interfaces": [name for name, _ in physical_receivers],
                "received_packets": 0,
                "method": "per-interface multicast membership with IP_MULTICAST_ALL=0",
            }
            results["passed"] = True
    finally:
        receiver.close()
        for _, sock in physical_receivers:
            sock.close()
        errors = []
        for root in roots:
            if (root / "vmgen.toml").exists():
                try:
                    runtime.stop(args(root))
                except Exception as exc:
                    errors.append(f"{root}: {exc}")
        results["cleanup_errors"] = errors
        (directory / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        if errors:
            print("Cleanup errors:", errors, file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    probe(parser.parse_args().directory.resolve())
