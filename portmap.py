"""Automatic router port forwarding, standard library only.

IPv4 (NAT):  PCP (RFC 6887)  ->  NAT-PMP (RFC 6886)  ->  UPnP IGD, first one the router speaks.

The router may hand out a different external port than the one asked for (it is busy, or the router
insists); that port is accepted and exposed in PortMapper.ext so the rest of the client can advertise
it to trackers, the DHT and other peers. UDP is steered to the same external number as TCP so that
uTP peers (which assume one port number) can find us. Mappings are renewed before they expire and
removed again on exit.
"""
import ipaddress
import os
import random
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque

import upnp as upnp_mod

LEASE = 7200        # seconds requested from PCP / NAT-PMP routers
MAP_TIMEOUTS = (0.25, 0.5, 1.0, 2.0, 4.0)   # NAT-PMP retransmission schedule (RFC 6886: 250 ms, doubling)


class PortMapError(Exception):
    def __init__(self, msg, temporary=False):
        super().__init__(msg)
        self.temporary = temporary   # worth retrying soon (router busy / no WAN link yet)


class _Epoch:
    """Router uptime tracking (RFC 6886 section 3.6, also used by PCP): if the router's 'seconds since start of
    epoch' goes backwards or jumps, it rebooted and has forgotten our mappings."""

    def __init__(self):
        self.last = None

    def check(self, epoch, now=None):
        now = time.time() if now is None else now
        prev, self.last = self.last, (epoch, now)
        if prev is None:
            return False
        c_delta, s_delta = now - prev[1], epoch - prev[0]
        return s_delta < 0 or c_delta + 2 < s_delta * 7 / 8 or c_delta * 7 / 8 > s_delta + 2


# --------------------------------------------------------------------------
# Finding default gateways
# --------------------------------------------------------------------------
def _parse_proc_route(text):
    out = []
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) >= 8 and p[1] == "00000000" and int(p[3], 16) & 2:  # default route via a gateway
            out.append((int(p[6]), socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))))
    return out


def _parse_win_route(text):
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 5 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0":
            try:
                ipaddress.IPv4Address(p[2])  # skips "On-link"
            except ValueError:
                continue
            out.append((int(p[4]) if p[4].isdigit() else 9999, p[2]))
    return out


def _parse_bsd_route(text):
    return [(0, line.split(":", 1)[1].strip()) for line in text.splitlines()
            if line.strip().startswith("gateway:")]


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=6,
                          creationflags=0x08000000 if sys.platform == "win32" else 0).stdout  # no console window


def default_gateways():
    """IPv4 default gateways, best first. Empty if they cannot be determined."""
    found = []
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/net/route") as f:
                found = _parse_proc_route(f.read())
        elif sys.platform == "win32":
            found = _parse_win_route(_run(["route", "print", "-4", "0.0.0.0"]))
        else:
            found = _parse_bsd_route(_run(["route", "-n", "get", "default"]))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    gws = []
    for _, ip in sorted(found):
        if ip not in gws:
            gws.append(ip)
    return gws


def guess_gateways():
    """When the OS will not say what the gateway is: the .1 address of each local /24, which is where
    the vast majority of home routers live."""
    out = []
    for ip in upnp_mod.local_ipv4s():
        parts = ip.split(".")
        guess = ".".join(parts[:3] + ["1"])
        if len(parts) == 4 and guess != ip and guess not in out:
            out.append(guess)
    return out


def _udp_exchange(addr, packet, timeouts=(0.25, 0.5, 1.0), bind=None):
    """Send `packet` to addr (an IPv4 (host, port) pair), retransmitting with back-off; return the
    first reply that comes from that address (or None). `bind` pins the source address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if bind:
            sock.bind((bind, 0))
        want = addr[0].split("%")[0]
        for t in timeouts:
            try:
                sock.sendto(packet, addr)
            except OSError:
                return None
            end = time.time() + t
            while True:
                left = end - time.time()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, src = sock.recvfrom(1100)
                except socket.timeout:
                    break
                except OSError:  # e.g. ICMP port unreachable
                    return None
                if src[0].split("%")[0] == want:
                    return data
        return None
    except OSError:
        return None
    finally:
        sock.close()


# --------------------------------------------------------------------------
# NAT-PMP (RFC 6886)
# --------------------------------------------------------------------------
class NATPMP:
    name = "NAT-PMP"
    ERRORS = {1: "unsupported version", 2: "not authorized / refused", 3: "network failure (no WAN link yet?)",
              4: "out of resources", 5: "unsupported opcode"}

    def __init__(self, gateway, port=5351):
        self.addr = (gateway, port)
        self.ext_ip = None
        self.epoch = _Epoch()
        self.rebooted = False

    def _note(self, epoch):
        if self.epoch.check(epoch):
            self.rebooted = True

    def _query_ip(self, timeouts=(0.25, 0.5, 1.0)):
        r = _udp_exchange(self.addr, struct.pack(">BB", 0, 0), timeouts)  # "what is my external address?"
        if r and len(r) >= 12 and r[0] == 0 and r[1] == 128:
            return struct.unpack(">H", r[2:4])[0], struct.unpack(">I", r[4:8])[0], socket.inet_ntoa(r[8:12])
        return None

    def probe(self):
        res = self._query_ip()
        if res and res[0] == 0:
            self.ext_ip = res[2]
            self._note(res[1])
            self.rebooted = False
            return True
        return False

    def poll(self):
        """Cheap health check. True if the router rebooted (lost our mappings) or its external address changed."""
        res = self._query_ip()
        if not res or res[0] != 0:
            return False
        changed = res[2] != self.ext_ip
        self.ext_ip = res[2]
        self._note(res[1])
        rebooted, self.rebooted = self.rebooted, False
        return rebooted or changed

    @staticmethod
    def _op(proto):
        return 1 if proto == "UDP" else 2

    def map(self, proto, port, want=None):
        """Returns (lifetime, external port); the router may assign a different external port."""
        op = self._op(proto)
        r = _udp_exchange(self.addr, struct.pack(">BBHHHI", 0, op, 0, port, want or port, LEASE), MAP_TIMEOUTS)
        if not r or len(r) < 16 or r[0] != 0 or r[1] != 128 + op:
            raise PortMapError("no valid reply from the NAT-PMP gateway")
        result, epoch, _iport, eport, life = struct.unpack(">HIHHI", r[2:16])
        self._note(epoch)
        if result:
            raise PortMapError("NAT-PMP error %d (%s)" % (result, self.ERRORS.get(result, "unknown")),
                               temporary=result in (3, 4))
        return life, eport

    def unmap(self, proto, port):
        _udp_exchange(self.addr, struct.pack(">BBHHHI", 0, self._op(proto), 0, port, 0, 0), timeouts=(0.4, 0.8))

    def external_ip(self):
        return self.ext_ip


# --------------------------------------------------------------------------
# PCP (RFC 6887)
# --------------------------------------------------------------------------
class PCP:
    name = "PCP"
    ERRORS = {1: "unsupported version", 2: "not authorized / refused", 3: "malformed request",
              4: "unsupported opcode", 7: "network failure", 8: "no resources", 9: "unsupported protocol",
              10: "quota exceeded", 11: "cannot provide the requested external port", 12: "address mismatch"}

    def __init__(self, gateway, port=5351, local_ip=None):
        self.addr = (gateway, port)
        self.local_ip = local_ip or upnp_mod._local_ip(gateway)
        self.nonces = {}
        self.ext_ip = None
        self.epoch = _Epoch()
        self.rebooted = False

    def _x(self, packet, timeouts=MAP_TIMEOUTS):
        return _udp_exchange(self.addr, packet, timeouts)

    def _client(self):
        return b"\x00" * 10 + b"\xff\xff" + socket.inet_aton(self.local_ip)  # IPv4-mapped IPv6

    def _header(self, opcode, lifetime):
        return struct.pack(">BBHI", 2, opcode, 0, lifetime) + self._client()

    def _announce(self, timeouts):
        r = self._x(self._header(0, 0), timeouts)  # ANNOUNCE
        if r and len(r) >= 24 and r[0] == 2 and r[1] == 0x80 and r[3] == 0:
            self.epoch_note(struct.unpack(">I", r[8:12])[0])
            return True
        return False

    def epoch_note(self, epoch):
        if self.epoch.check(epoch):
            self.rebooted = True

    def probe(self):
        ok = self._announce((0.25, 0.5, 1.0))
        self.rebooted = False
        return ok

    def poll(self):
        """True if the router rebooted and forgot our mappings."""
        if not self._announce((0.25, 0.5, 1.0)):
            return False
        rebooted, self.rebooted = self.rebooted, False
        return rebooted

    def _map_packet(self, proto, port, want, lifetime):
        nonce = self.nonces.setdefault((proto, port), os.urandom(12))
        body = nonce + struct.pack(">B3xHH", 6 if proto == "TCP" else 17, port, want) + b"\x00" * 16
        return self._header(1, lifetime) + body, nonce

    def map(self, proto, port, want=None):
        """Returns (lifetime, external port)."""
        pkt, nonce = self._map_packet(proto, port, want or port, LEASE)
        r = self._x(pkt)
        if not r or len(r) < 60 or r[0] != 2 or r[1] != 0x81:
            raise PortMapError("no valid reply from the PCP gateway")
        self.epoch_note(struct.unpack(">I", r[8:12])[0])
        if r[3]:
            raise PortMapError("PCP error %d (%s)" % (r[3], self.ERRORS.get(r[3], "unknown")),
                               temporary=r[3] in (7, 8))
        if r[24:36] != nonce:
            raise PortMapError("PCP reply did not match our request")
        life = struct.unpack(">I", r[4:8])[0]
        eport = struct.unpack(">H", r[42:44])[0]
        ext = r[44:60]
        self.ext_ip = socket.inet_ntoa(ext[12:]) if ext[:12] == b"\x00" * 10 + b"\xff\xff" else None
        return life, eport

    def unmap(self, proto, port):
        pkt, _ = self._map_packet(proto, port, port, 0)
        self._x(pkt, timeouts=(0.4, 0.8))

    def external_ip(self):
        return self.ext_ip


# --------------------------------------------------------------------------
# UPnP adapters
# --------------------------------------------------------------------------
class UPnPBackend:
    name = "UPnP"

    def __init__(self, ssdp_addr=upnp_mod.SSDP, log=None, gateways=(), interfaces=None, unicast_port=1900):
        self.ssdp_addr, self.gw = ssdp_addr, None
        self.log, self.gateways, self.interfaces, self.unicast_port = log, tuple(gateways), interfaces, unicast_port
        self.ext = {}   # (proto, local port) -> external port we mapped
        self.ext_ip = None

    def probe(self):
        self.gw = upnp_mod.discover(ssdp_addr=self.ssdp_addr, log=self.log, gateways=self.gateways,
                                    interfaces=self.interfaces, unicast_port=self.unicast_port)
        return self.gw is not None

    def poll(self):
        try:
            ip = self.gw.external_ip() or None
        except Exception:
            return False
        changed = self.ext_ip is not None and ip != self.ext_ip
        self.ext_ip = ip
        return changed

    def _add(self, proto, ext, port):
        desc = "Bit Torrent Client " + proto
        try:
            self.gw.add_mapping(proto, ext, port, desc, upnp_mod.LEASE)
            return upnp_mod.LEASE
        except upnp_mod.UPnPError as e:
            if e.code == 718:  # external port taken: the caller picks another one
                raise
            # 725 = only permanent mappings; other routers reject timed ones with 402 / 501 / ... - try permanent
            if self.log:
                self.log("%s %d: router rejected a timed mapping (%s); retrying as a permanent mapping" % (proto, port, e))
        self.gw.add_mapping(proto, ext, port, desc, 0)
        return 0

    def map(self, proto, port, want=None):
        """Returns (lifetime, external port). If the wanted external port is taken (error 718) the next
        free ports are tried."""
        want = want or port
        tried, last = [], None
        candidates = [want] + [want + i for i in range(1, 10)] + [random.randint(20000, 60000) for _ in range(3)]
        for ext in candidates:
            if ext > 65535 or ext in tried:
                continue
            tried.append(ext)
            try:
                life = self._add(proto, ext, port)
            except upnp_mod.UPnPError as e:
                last = e
                if e.code == 718:   # conflict in mapping entry: try another external port
                    continue
                raise PortMapError(str(e))
            except (OSError, ValueError) as e:
                raise PortMapError(str(e))
            except Exception as e:  # malformed XML from the router
                raise PortMapError("bad reply from router: %s" % e)
            self.ext[(proto, port)] = ext
            return life, ext
        raise PortMapError(str(last) if last else "no free external port")

    def unmap(self, proto, port):
        self.gw.delete_mapping(proto, self.ext.pop((proto, port), port))

    def external_ip(self):
        try:
            self.ext_ip = self.gw.external_ip() or None
        except Exception:
            pass
        return self.ext_ip


# --------------------------------------------------------------------------
# Unsolicited router announcements (NAT-PMP and PCP): 224.0.0.1:5350
# --------------------------------------------------------------------------
class AnnounceListener(threading.Thread):
    """Routers multicast a packet when they reboot or their external address changes. Calls `on_event(source_ip)`."""

    def __init__(self, on_event, bind=("", 5350), multicast=True):
        super().__init__(name="pmp-announce", daemon=True)
        self.on_event, self.bind_addr, self.multicast = on_event, bind, multicast
        self._stop_ev = threading.Event()
        self.sock = None

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(self.bind_addr)
            if self.multicast:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton("224.0.0.1") + socket.inet_aton("0.0.0.0"))
            sock.settimeout(1.0)
        except OSError:
            return  # port busy or no multicast: polling still catches changes, just later
        self.sock = sock
        while not self._stop_ev.is_set():
            try:
                data, src = sock.recvfrom(1100)
            except socket.timeout:
                continue
            except OSError:
                break
            natpmp = len(data) >= 12 and data[0] == 0 and data[1] == 128 and data[2:4] == b"\x00\x00"
            pcp = len(data) >= 24 and data[0] == 2 and data[1] == 0x80  # ANNOUNCE response, R bit set
            if natpmp or pcp:
                try:
                    self.on_event(src[0])
                except Exception:
                    pass
        sock.close()

    def stop(self):
        self._stop_ev.set()


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------
class PortMapper:
    """Background worker: detect the router's protocol, map `ports` [(proto, local port), ...] and keep it alive:

      * renews mappings at half their lease;
      * notices router reboots (epoch), external-address changes and NAT-PMP/PCP announcements and re-maps;
      * calls `on_change()` when ports / addresses change so trackers can be re-announced.
    """

    def __init__(self, ports, protocols=("pcp", "natpmp", "upnp"), ssdp_addr=upnp_mod.SSDP,
                 gateways=None, pmp_port=5351, retry=300, tick=30, poll_interval=600,
                 announce_addr=("", 5350), announce_multicast=True, announce_listen=True, on_change=None,
                 ssdp_interfaces=None, ssdp_unicast_port=1900):
        self.ports, self.protocols = list(ports), tuple(protocols)
        self.ssdp_addr, self.gateways, self.pmp_port, self.retry = ssdp_addr, gateways, pmp_port, retry
        self.tick, self.poll_interval, self.on_change = tick, poll_interval, on_change
        self.ssdp_interfaces, self.ssdp_unicast_port = ssdp_interfaces, ssdp_unicast_port
        self.log = deque(maxlen=300)   # what happened, for the Diagnostics window
        self._gws_cache = (0.0, [])
        self.announce = (announce_addr, announce_multicast) if announce_listen else None
        self.status4 = "Searching for router..."
        self.protocol = None        # protocol in use
        self.external_ip = None
        self.ext = {}               # (proto, local port) -> external port the router gave us
        self.mapped = []            # mappings in place
        self.backend = None
        self.last_wait = None
        self._snap = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = self._listener = None

    @property
    def status(self):
        return self.status4

    def external_port(self, proto, port):
        return self.ext.get((proto, port), port)

    def _log(self, msg):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))

    def _gateways(self):
        """IPv4 gateways to talk to: the OS's default gateways, else a guess (cached for a minute)."""
        if self.gateways is not None:
            return self.gateways
        if time.time() - self._gws_cache[0] > 60:
            gws = default_gateways()
            if gws:
                self._log("default gateway(s) from the OS: %s" % ", ".join(gws))
            else:
                gws = guess_gateways()
                self._log("the OS did not report a default gateway; guessing %s" % (", ".join(gws) or "nothing"))
            self._gws_cache = (time.time(), gws)
        return self._gws_cache[1]

    def _upnp(self):
        return UPnPBackend(self.ssdp_addr, self._log, self._gateways(), self.ssdp_interfaces, self.ssdp_unicast_port)

    def rescan(self):
        """Forget what was detected and look for the router again right now."""
        self._log("re-scan requested")
        self.backend = None
        self._gws_cache = (0.0, [])
        self._wake.set()

    def diagnostics(self):
        ports = ", ".join("%s %d" % p for p in self.ports)
        ext = ", ".join("%s %d -> %d" % (p, port, e) for (p, port), e in sorted(self.ext.items())) or "none"
        lines = ["Status: %s" % self.status4, "Protocol in use: %s" % (self.protocol or "none"),
                 "Local ports: %s" % ports, "External ports from the router: %s" % ext,
                 "Router external address: %s" % (self.external_ip or "unknown"),
                 "Protocols tried, in order: %s" % ", ".join(p.upper() for p in self.protocols), "", "Log:"]
        return "\n".join(lines + list(self.log))

    def start(self):
        if self.announce and any(p in self.protocols for p in ("pcp", "natpmp")):
            (addr, multicast) = self.announce
            self._listener = AnnounceListener(self._on_announce, addr, multicast)
            self._listener.start()
        self._thread = threading.Thread(target=self._run, name="portmap", daemon=True)
        self._thread.start()

    def stop(self, timeout=12):
        """Stop the worker and remove our mappings / pinholes from the router."""
        self._stop.set()
        self._wake.set()
        if self._listener:
            self._listener.stop()
        if self._thread:
            self._thread.join(timeout)
        self.status4, self.status6 = "off", ""

    def _on_announce(self, src):
        b = self.backend
        if isinstance(b, (NATPMP, PCP)) and src == b.addr[0]:   # only believe our own gateway
            self._wake.set()

    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # -- IPv4 -------------------------------------------------------------
    def _detect(self):
        gws = self._gateways()
        for proto in self.protocols:
            if self._stop.is_set():
                return None
            if proto in ("pcp", "natpmp"):
                label = "PCP" if proto == "pcp" else "NAT-PMP"
                if not gws:
                    self._log("%s: no gateway address to ask" % label)
                for gw in gws:
                    try:
                        b = (PCP if proto == "pcp" else NATPMP)(gw, self.pmp_port)
                        ok = b.probe()
                    except OSError as e:
                        self._log("%s probe %s failed: %s" % (label, gw, e))
                        continue
                    self._log("%s probe %s: %s" % (label, gw, "router answered" if ok else "no answer"))
                    if ok:
                        return b
            elif proto == "upnp":
                self._log("UPnP: searching")
                b = self._upnp()
                if b.probe():
                    return b
                self._log("UPnP: no router found")
        return None

    def _describe(self, items):
        parts = []
        for proto, port in items:
            ext = self.ext.get((proto, port), port)
            parts.append("%s %d" % (proto, ext) + (" (local %d)" % port if ext != port else ""))
        return ", ".join(parts)

    def _map_all(self):
        b, done, errors, lifetimes, temporary = self.backend, [], [], [], []
        tcp_ext = None
        for proto, port in sorted(self.ports, key=lambda p: p[0] != "TCP"):  # TCP first: UDP follows its number
            want = self.ext.get((proto, port)) or (tcp_ext if proto == "UDP" and tcp_ext else port)
            try:
                life, ext = b.map(proto, port, want)
                self._log("%s %d: mapped to external port %d (lease %s)" % (
                    proto, port, ext, ("%d s" % life) if life else "permanent"))
            except (PortMapError, OSError) as e:
                self._log("%s %d: FAILED - %s" % (proto, port, e))
                errors.append("%s %d: %s" % (proto, port, e))
                temporary.append(isinstance(e, PortMapError) and e.temporary)
                self.ext.pop((proto, port), None)
                continue
            self.ext[(proto, port)] = ext
            lifetimes.append(life)
            done.append((proto, port))
            if proto == "TCP":
                tcp_ext = ext
        self.external_ip = b.external_ip()     # a router round trip: self.mapped is published only after the status
        b.rebooted = False   # we just re-created every mapping, so a pending reboot flag is moot
        listing = self._describe(done)
        if done and not errors:
            txt = "forwarded %s via %s" % (listing, b.name)
            if self.external_ip:
                try:
                    if ipaddress.ip_address(self.external_ip).is_global:
                        txt += " (public IP %s)" % self.external_ip
                    else:
                        txt += " (router WAN address %s is not public: double NAT / CGNAT, may be unreachable)" % self.external_ip
                except ValueError:
                    pass
            udp = [self.ext.get(m) for m in done if m[0] == "UDP"]
            if tcp_ext and udp and udp[0] != tcp_ext:
                txt += "; UDP external port differs from TCP, so inbound uTP peers may not find you"
            self.status4 = txt
        elif done:
            self.status4 = "partly forwarded via %s (%s); failed: %s" % (b.name, listing, "; ".join(errors))
        else:
            self.status4 = "%s router refused the mapping: %s" % (b.name, "; ".join(errors))
        self.mapped = done   # last: whoever sees the mappings also sees the status and external IP that go with them
        if done:  # renew at half the shortest lease (0 = permanent -> hourly check)
            real = [l for l in lifetimes if l > 0]
            return max(60, (min(real) // 2) if real else 3600)
        return 60 if temporary and all(temporary) else self.retry   # "WAN not up yet" style errors: look again soon

    def _pass4(self):
        if self.backend is None:
            self.status4 = "Searching for router..."
            self.backend = self._detect()
            self.protocol = self.backend.name if self.backend else None
        if self.backend is None:
            if not self._stop.is_set():
                self.status4 = "no PCP / NAT-PMP / UPnP router found (enable one in the router, or forward ports manually)"
            return self.retry
        wait = self._map_all()
        if not self.mapped:
            self.backend = None  # re-detect next round
            self.ext.clear()
        return wait

    # -- worker -----------------------------------------------------------
    def _run(self):
        nxt = 0.0
        next_poll = time.time() + self.poll_interval
        while not self._stop.is_set():
            woke = self._wake.is_set()
            self._wake.clear()
            now = time.time()
            try:
                if now >= nxt or woke:
                    self.last_wait = self._pass4()
                    nxt = time.time() + self.last_wait
                if self.backend is not None and time.time() >= next_poll:
                    next_poll = time.time() + self.poll_interval
                    if self.backend.poll():   # router rebooted or its external address changed
                        nxt = 0.0
                snap = (tuple(sorted(self.ext.items())), self.external_ip)
                if snap != self._snap:   # ports / address changed (including the first result): re-announce
                    self._snap = snap
                    self._notify()
            except Exception as e:  # network hiccup: forget the router and rediscover later
                self.backend = None
                self.status4 = "error: %s" % e
                nxt = time.time() + 60
            due = min(nxt, next_poll)
            self._wake.wait(min(self.tick, max(0.05, due - time.time())))
        if self.backend:
            for proto, port in self.mapped:
                try:
                    self.backend.unmap(proto, port)
                except Exception:
                    pass
        self.mapped = []
        self.ext.clear()
