"""
wol_listener.py -- start an AppSandbox VM when a Wake-on-LAN packet for it arrives.

Moonlight wakes a streaming host by broadcasting a WoL "magic packet" carrying
the MAC address Sunshine reported -- for a VM on an External (bridged) network
that is the VM's own MAC. A powered-off VM has no switch port, so nothing
receives that packet. This script listens for those packets on the HOST and
starts the matching VM through the AppSandbox headless daemon.

Requirements (host):
  * AppSandbox running as the headless daemon (`appsandbox.exe --headless`,
    elevated). Pass --appsandbox-exe and the listener launches it for you.
  * The VM uses the External network mode (so it has a LAN MAC + DHCP lease).
  * Inbound UDP on the WoL ports allowed by the Windows firewall
    (install_wol_listener.ps1 adds the rule).

Usage:
  python wol_listener.py --vm Gaming
  python wol_listener.py --vm Gaming --appsandbox-exe "C:\\Program Files\\AppSandbox\\appsandbox.exe"
  python wol_listener.py --vm Gaming=02-AB-CD-EF-12-34      # explicit MAC

The VM's MAC is read from %ProgramData%\\AppSandbox\\vms.cfg unless given.
Stdlib only.
"""
import argparse
import logging
import os
import select
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# asb.py lives in tools/headless-api; also allow it to sit next to this script.
for p in (HERE, os.path.join(HERE, "..", "..", "headless-api")):
    if os.path.isfile(os.path.join(p, "asb.py")):
        sys.path.insert(0, os.path.abspath(p))
        break
import asb  # noqa: E402

# Ports Moonlight sends magic packets to (moonlight-common-c / moonlight-qt
# send to all of them), plus the classic WoL ports 7 and 9.
DEFAULT_PORTS = [7, 9, 47998, 47999, 48000, 48002, 48010]

NET_EXTERNAL = 2

log = logging.getLogger("wol")


# ---------------------------------------------------------------- magic packets

def normalize_mac(mac):
    """'02-ab-CD:ef.12 34' -> '02:AB:CD:EF:12:34'. Raises ValueError if invalid."""
    hexdigits = "".join(ch for ch in mac if ch not in "-:. ").upper()
    if len(hexdigits) != 12 or any(ch not in "0123456789ABCDEF" for ch in hexdigits):
        raise ValueError("invalid MAC address: %r" % mac)
    return ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))


def magic_packet_mac(data):
    """Return the target MAC of a WoL magic packet ('AA:BB:...') or None.

    A magic packet is 6 bytes of 0xFF followed by the target MAC repeated 16
    times, optionally followed by a 4- or 6-byte SecureOn password. Some senders
    prepend a header, so search for the sync stream rather than assuming offset 0.
    """
    sync = b"\xff" * 6
    start = data.find(sync)
    while start != -1:
        body = data[start + 6:]
        # A sync run longer than 6 bytes fails here and matches on a later
        # offset of the same run.
        if len(body) >= 96:
            mac = body[:6]
            if mac != b"\xff" * 6 and body[:96] == mac * 16:
                return ":".join("%02X" % b for b in mac)
        start = data.find(sync, start + 1)
    return None


def build_magic_packet(mac):
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


# ---------------------------------------------------------------- vms.cfg

def config_path():
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, "AppSandbox", "vms.cfg")


def read_vm_config(path=None):
    """Parse AppSandbox's vms.cfg -> {name: {"mac": str|None, "network_mode": int}}."""
    path = path or config_path()
    vms, cur = {}, None
    with open(path, encoding="utf-8-sig") as f:   # written with a UTF-8 BOM
        for line in f:
            line = line.strip()
            if line.startswith("["):
                cur = {} if line == "[VM]" else None
                continue
            if cur is None or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key == "Name":
                vms[val] = cur
            cur[key] = val
    out = {}
    for name, kv in vms.items():
        mac = kv.get("MacAddress")
        out[name] = {
            "mac": normalize_mac(mac) if mac else None,
            "network_mode": int(kv.get("NetworkMode", "0") or 0),
        }
    return out


def resolve_targets(specs, cfg_path=None):
    """--vm specs ('Name' or 'Name=MAC') -> {MAC: name}."""
    targets = {}
    cfg = None
    for spec in specs:
        name, _, mac = spec.partition("=")
        if mac:
            targets[normalize_mac(mac)] = name
            continue
        if cfg is None:
            try:
                cfg = read_vm_config(cfg_path)
            except FileNotFoundError:
                raise SystemExit("cannot read %s; pass the MAC explicitly: --vm %s=AA-BB-CC-DD-EE-FF"
                                 % (cfg_path or config_path(), name))
        info = cfg.get(name)
        if info is None:
            raise SystemExit("VM %r not found in %s (known: %s)"
                             % (name, cfg_path or config_path(), ", ".join(sorted(cfg)) or "none"))
        if not info["mac"]:
            raise SystemExit("VM %r has no MacAddress in vms.cfg yet -- start it once with "
                             "networking enabled, or pass --vm %s=<MAC>" % (name, name))
        if info["network_mode"] != NET_EXTERNAL:
            log.warning("VM %r is not on the External network; Moonlight on other "
                        "machines will not be able to reach it.", name)
        targets[info["mac"]] = name
    return targets


# ---------------------------------------------------------------- daemon

class Daemon:
    """Lazily (re)connects to the headless daemon, optionally launching it."""

    def __init__(self, exe=None):
        self.exe = exe
        self.lock = threading.Lock()
        self.client = None

    def _try_connect(self):
        try:
            return asb.connect()
        except Exception as e:  # stale/missing host.json, daemon not up yet
            log.debug("daemon connect failed: %s", e)
            return None

    def get(self, launch_timeout=90):
        with self.lock:
            if self.client is not None:
                try:
                    self.client.version()
                    return self.client
                except Exception:
                    self.client = None   # daemon restarted (new port/token)
            self.client = self._try_connect()
            if self.client is None and self.exe:
                log.info("launching AppSandbox daemon: %s --headless", self.exe)
                flags = 0
                if sys.platform == "win32":
                    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                subprocess.Popen([self.exe, "--headless"], creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
                deadline = time.time() + launch_timeout
                while self.client is None and time.time() < deadline:
                    time.sleep(2)
                    self.client = self._try_connect()
            if self.client is None:
                raise RuntimeError("AppSandbox headless daemon is not reachable")
            return self.client


# ---------------------------------------------------------------- waking

class Waker:
    def __init__(self, daemon, cooldown):
        self.daemon = daemon
        self.cooldown = cooldown
        self.busy = set()
        self.last_start = {}
        self.lock = threading.Lock()

    def wake(self, name, source):
        with self.lock:
            if name in self.busy:
                return
            if time.time() - self.last_start.get(name, 0) < self.cooldown:
                return
            self.busy.add(name)
        threading.Thread(target=self._wake, args=(name, source), daemon=True).start()

    def _wake(self, name, source):
        try:
            c = self.daemon.get()
            state = c.status(name)["state"]
            if state == "stopping":
                log.info("%s: WoL from %s while shutting down; waiting for it to stop", name, source)
                state = c.wait(name, {"stopped"}, timeout=300)["state"]
            if state != "stopped":
                log.debug("%s: WoL from %s ignored (state=%s)", name, source, state)
                return
            log.info("%s: WoL from %s -- starting VM", name, source)
            code, body = c.start(name)
            if code >= 300:
                log.error("%s: start failed: HTTP %s %s", name, code, body)
                return
            with self.lock:
                self.last_start[name] = time.time()
        except KeyError:
            log.error("%s: no such VM in the daemon", name)
        except Exception as e:
            log.error("%s: wake failed: %s", name, e)
        finally:
            with self.lock:
                self.busy.discard(name)


# ---------------------------------------------------------------- main loop

def open_sockets(ports, bind_addr):
    socks = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind((bind_addr, port))
        except OSError as e:
            log.warning("cannot listen on UDP %d (%s); skipping", port, e)
            s.close()
            continue
        socks.append(s)
    if not socks:
        raise SystemExit("could not bind any WoL port")
    log.info("listening on UDP %s", ", ".join(str(s.getsockname()[1]) for s in socks))
    return socks


def serve(targets, waker, ports, bind_addr="0.0.0.0"):
    socks = open_sockets(ports, bind_addr)
    for mac, name in targets.items():
        log.info("watching for %s -> VM %r", mac, name)
    while True:
        readable, _, _ = select.select(socks, [], [], 5.0)
        for s in readable:
            try:
                data, addr = s.recvfrom(2048)
            except OSError:
                continue   # e.g. WSAECONNRESET from an ICMP unreachable
            mac = magic_packet_mac(data)
            if mac is None:
                continue
            name = targets.get(mac)
            if name is None:
                log.debug("magic packet for unknown MAC %s from %s", mac, addr[0])
                continue
            waker.wake(name, addr[0])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vm", action="append", required=True, metavar="NAME[=MAC]",
                    help="VM to wake (repeatable). MAC is read from vms.cfg if omitted.")
    ap.add_argument("--appsandbox-exe", metavar="PATH",
                    help="launch `PATH --headless` if the daemon is not running")
    ap.add_argument("--ports", default=",".join(map(str, DEFAULT_PORTS)),
                    help="comma-separated UDP ports (default: %(default)s)")
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all)")
    ap.add_argument("--cooldown", type=int, default=60,
                    help="seconds to ignore further packets after starting a VM (default: %(default)s)")
    ap.add_argument("--config", metavar="PATH", help="path to vms.cfg (default: %s)" % config_path())
    ap.add_argument("--log", metavar="FILE", help="also log to FILE")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    handlers = []
    if sys.stderr is not None:   # None under pythonw.exe
        handlers.append(logging.StreamHandler())
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    targets = resolve_targets(args.vm, args.config)
    daemon = Daemon(args.appsandbox_exe)
    if args.appsandbox_exe:
        try:
            daemon.get()
            log.info("AppSandbox daemon is up")
        except Exception as e:
            log.warning("%s; will retry when a packet arrives", e)
    ports = [int(p) for p in args.ports.split(",") if p.strip()]
    try:
        serve(targets, Waker(daemon, args.cooldown), ports, args.bind)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
