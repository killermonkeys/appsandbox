"""
wol_relay.py -- forward Wake-on-LAN for a VM to the PC that hosts it.

A sleeping PC's network card only wakes for a magic packet carrying ITS OWN
MAC. Moonlight sends one for the VM's MAC (that is what Sunshine inside the VM
reports), so the host never wakes. Run this on any always-on machine on the
same LAN (Raspberry Pi, NAS, router with Python): when it sees a magic packet
for the VM's MAC it broadcasts one for the host's MAC. Once the host is awake,
wol_listener.py on the host catches Moonlight's next packet and starts the VM.

Usage:
  python3 wol_relay.py --vm-mac 02-AB-CD-EF-12-34 --host-mac 9C-6B-00-11-22-33

Stdlib only; runs on Linux, macOS or Windows. Ports below 1024 (7, 9) need root
on Linux -- use --ports to drop them, or setcap/sudo.
"""
import argparse
import logging
import select
import socket
import time

DEFAULT_PORTS = [7, 9, 47998, 47999, 48000, 48002, 48010]

log = logging.getLogger("wol-relay")


def normalize_mac(mac):
    hexdigits = "".join(ch for ch in mac if ch not in "-:. ").upper()
    if len(hexdigits) != 12 or any(ch not in "0123456789ABCDEF" for ch in hexdigits):
        raise ValueError("invalid MAC address: %r" % mac)
    return ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))


def magic_packet_mac(data):
    sync = b"\xff" * 6
    start = data.find(sync)
    while start != -1:
        body = data[start + 6:]
        if len(body) >= 96:
            mac = body[:6]
            if mac != b"\xff" * 6 and body[:96] == mac * 16:
                return ":".join("%02X" % b for b in mac)
        start = data.find(sync, start + 1)
    return None


def build_magic_packet(mac):
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


def main(argv=None):
    ap = argparse.ArgumentParser(description="Relay WoL for a VM to its host PC.")
    ap.add_argument("--vm-mac", action="append", required=True,
                    help="MAC Moonlight wakes (the VM's). Repeatable.")
    ap.add_argument("--host-mac", required=True, help="MAC of the host PC's physical NIC")
    ap.add_argument("--broadcast", default="255.255.255.255",
                    help="where to send the host's magic packet (default: %(default)s; "
                         "a subnet broadcast like 192.168.1.255 is more reliable on multi-NIC boxes)")
    ap.add_argument("--ports", default=",".join(map(str, DEFAULT_PORTS)))
    ap.add_argument("--cooldown", type=int, default=10, help="seconds between relayed wakes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    vm_macs = {normalize_mac(m) for m in args.vm_mac}
    host_packet = build_magic_packet(args.host_mac)

    socks = []
    for port in (int(p) for p in args.ports.split(",") if p.strip()):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            log.warning("cannot listen on UDP %d (%s); skipping", port, e)
            s.close()
            continue
        socks.append(s)
    if not socks:
        raise SystemExit("could not bind any port")
    log.info("relaying %s -> %s", ", ".join(sorted(vm_macs)), normalize_mac(args.host_mac))

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    last = 0.0
    while True:
        readable, _, _ = select.select(socks, [], [], 5.0)
        for s in readable:
            try:
                data, addr = s.recvfrom(2048)
            except OSError:
                continue
            mac = magic_packet_mac(data)
            if mac not in vm_macs or time.time() - last < args.cooldown:
                continue
            last = time.time()
            log.info("WoL for %s from %s -> waking host", mac, addr[0])
            for port in (7, 9):
                out.sendto(host_packet, (args.broadcast, port))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
