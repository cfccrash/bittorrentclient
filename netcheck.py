"""Is something on this PC interfering with local (loopback) network traffic?

Filtering software that hooks the Windows network stack (AdGuard was confirmed; others work the same way) can
drop the first UDP packet a new socket receives and reset or stall a few percent of local TCP connections. For this
app that means a lost DHT bootstrap packet and the built-in player's local stream connection failing at random.

check() takes a few seconds on a healthy PC (longer when connections stall), so run it in a thread."""
import os
import socket
import subprocess
import sys
import threading
import time

# process name (lower case, no .exe) -> product, only named when a check has actually failed
KNOWN_FILTERS = {"adguard": "AdGuard", "adguardsvc": "AdGuard", "portmaster-core": "Portmaster", "portmaster": "Portmaster",
                 "nlsvc": "NetLimiter", "netlimiter": "NetLimiter", "glasswire": "GlassWire", "gwctlsrv": "GlassWire",
                 "avp": "Kaspersky", "ekrn": "ESET", "bdservicehost": "Bitdefender", "avastsvc": "Avast",
                 "avgsvc": "AVG", "simplewall": "simplewall", "windivert": "WinDivert"}


def first_udp_packet_lost(tries=3, wait=0.5):
    """True when the first datagram to a fresh socket is lost every time while the second one arrives."""
    lost = 0
    for _ in range(tries):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rx.bind(("127.0.0.1", 0))
            rx.settimeout(wait)
            for b in (b"0", b"1"):
                tx.sendto(b, rx.getsockname())
            got = set()
            try:
                while True:
                    got.add(rx.recvfrom(16)[0])
            except socket.timeout:
                pass
            if got == {b"1"}:
                lost += 1
            elif not got:
                return False          # nothing at all: UDP is simply unavailable, not this effect
        finally:
            rx.close()
            tx.close()
    return lost == tries


def tcp_failures(n=40, timeout=2.0, budget=12.0):
    """Short request/response round trips through a local server. Returns (failed, attempted)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    addr = srv.getsockname()
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                c.settimeout(timeout)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = c.recv(64)
                    if not chunk:
                        break
                    buf += chunk
                c.sendall(b"ok " + b"x" * 200 + b"\n")
            except OSError:
                pass
            finally:
                c.close()
    th = threading.Thread(target=serve, name="netcheck", daemon=True)
    th.start()
    failed = done = 0
    end = time.time() + budget
    try:
        while done < n and time.time() < end:
            done += 1
            try:
                with socket.create_connection(addr, timeout=timeout) as c:
                    c.sendall(b"ping\n")
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = c.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                if not data.startswith(b"ok "):
                    failed += 1
            except OSError:
                failed += 1
    finally:
        stop.set()
        srv.close()
    return failed, done


def running_filters():
    """Names of known filtering programs that are running (Windows only)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace",
                             timeout=10, creationflags=0x08000000).stdout          # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        name = line.split('","')[0].strip('"').lower()
        name = name[:-4] if name.endswith(".exe") else name
        prod = KNOWN_FILTERS.get(name)
        if prod and prod not in found:
            found.append(prod)
    return found


def check():
    udp = first_udp_packet_lost()
    failed, tried = tcp_failures()
    bad = udp or failed > 0
    return {"ok": not bad, "udp_first_lost": udp, "tcp_failed": failed, "tcp_tried": tried,
            "filters": running_filters() if bad else []}


def program_name():
    return os.path.basename(sys.executable) or "BitTorrentClient.exe"


def summary(res):
    """One line for the diagnostics report."""
    if res is None:
        return "Local network check: running…"
    if res["ok"]:
        return "Local network check: OK (nothing is filtering local connections)."
    what = []
    if res["udp_first_lost"]:
        what.append("the first UDP packet to a new socket is dropped")
    if res["tcp_failed"]:
        what.append("%d of %d local connections were reset or stalled" % (res["tcp_failed"], res["tcp_tried"]))
    who = (" Running: %s." % ", ".join(res["filters"])) if res["filters"] else ""
    return "Local network check: PROBLEM - %s.%s" % ("; ".join(what), who)


def hint(res):
    """Plain-language advice, or None when all is well."""
    if not res or res["ok"]:
        return None
    who = " or ".join(res["filters"]) if res["filters"] else "a firewall, VPN or ad blocker"
    return ("Something on this PC (%s) is filtering local network connections. Playback inside the app and DHT "
            "can fail at random. Exclude %s from its filtering (add it to the program's exceptions / trusted apps)."
            % (who, program_name()))
