"""UPnP Internet Gateway Device (IGD) client - standard library only.

Discovers the router with SSDP and talks SOAP over HTTP to it. The renew/cleanup logic lives in
portmap.py, which also supports PCP and NAT-PMP.
"""
import select
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SSDP = ("239.255.255.250", 1900)
TARGETS = ("urn:schemas-upnp-org:device:InternetGatewayDevice:2",
           "urn:schemas-upnp-org:device:InternetGatewayDevice:1")
WAN_SERVICES = ("WANIPConnection", "WANPPPConnection")
# Some routers only answer certain search targets, so ask for all of these.
SEARCH_TARGETS = TARGETS + ("urn:schemas-upnp-org:service:WANIPConnection:1",
                            "urn:schemas-upnp-org:service:WANPPPConnection:1", "upnp:rootdevice")
# Last resort when SSDP gets no answer at all: description documents routers commonly serve on fixed ports.
FALLBACK_URLS = ((5000, "/rootDesc.xml"), (49152, "/gatedesc.xml"), (52869, "/picsdesc.xml"),
                 (80, "/rootDesc.xml"), (1900, "/igd.xml"))
LEASE = 3600


class UPnPError(Exception):
    def __init__(self, code, text=""):
        super().__init__("UPnP error %s: %s" % (code, text))
        self.code = int(code) if str(code).isdigit() else 0


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _local_ip(host):
    """Our LAN address as seen on the route towards the gateway (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def local_ipv4s():
    """Our usable IPv4 addresses, one per network adapter (loopback and link-local excluded)."""
    ips = []
    try:
        ips += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    try:  # the address the default route uses (nothing is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    out = []
    for ip in ips:
        if ip not in out and not ip.startswith(("127.", "169.254.", "0.")):
            out.append(ip)
    return out


def _msearch(host, port, st):
    return ('M-SEARCH * HTTP/1.1\r\nHOST: %s:%d\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n'
            % (host, port, st)).encode()


def _ssdp_search(addr, timeout, log=None, gateways=(), interfaces=None, unicast_port=1900):
    """Find UPnP description URLs. The multicast search goes out of EVERY network adapter (VPN / Hyper-V / WSL
    adapters otherwise swallow it), and is also sent straight to the gateway: a unicast request makes the reply
    look like 'return traffic' to Windows Firewall, which drops the multicast kind."""
    log = log or (lambda m: None)
    multicast = addr == SSDP
    ifaces = interfaces if interfaces is not None else (local_ipv4s() if multicast else [])
    socks = []
    for bind_ip in [None] + list(ifaces):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            if bind_ip:
                s.bind((bind_ip, 0))
                if multicast:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            socks.append(s)
        except OSError as e:
            log("SSDP: cannot use adapter %s: %s" % (bind_ip, e))
    sent = 0
    first = socks[0] if socks else None
    for s in socks:
        for st in SEARCH_TARGETS:
            for dest in [addr] + [(g, unicast_port) for g in gateways if s is first]:
                try:
                    s.sendto(_msearch(dest[0], dest[1], st), dest)
                    sent += 1
                except OSError:
                    pass
    log("SSDP: sent %d search packets (%d adapter(s)%s)" % (
        sent, max(len(socks) - 1, 0), ", unicast to gateway " + ", ".join(gateways) if gateways else ""))
    locations, end, grace = [], time.time() + timeout, None
    try:
        while time.time() < end and (grace is None or time.time() < grace):
            try:
                ready, _, _ = select.select(socks, [], [], 0.3)
            except (OSError, ValueError):
                break
            for s in ready:
                try:
                    data, src = s.recvfrom(4096)
                except OSError:
                    continue
                for line in data.decode("latin-1").split("\r\n"):
                    if line.lower().startswith("location:"):
                        loc = line.split(":", 1)[1].strip()
                        if loc.lower().startswith("http") and loc not in locations:
                            locations.append(loc)
                            grace = grace or time.time() + 1.0
    finally:
        for s in socks:
            s.close()
    locations.sort(key=lambda u: urllib.parse.urlparse(u).hostname not in gateways)  # the gateway's own first
    return locations


def _fault(data):
    try:
        root = ET.fromstring(data)
        code = text = ""
        for el in root.iter():
            if _local(el.tag) == "errorCode":
                code = (el.text or "").strip()
            elif _local(el.tag) == "errorDescription":
                text = (el.text or "").strip()
        return code or "0", text
    except ET.ParseError:
        return "0", "unparseable error response"


class Gateway:
    def __init__(self, control_url, service, host):
        self.control_url, self.service, self.host = control_url, service, host
        self.local_ip = _local_ip(host)

    def call(self, action, args=(), url=None, service=None):
        service = service or self.service
        params = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in args)
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                '<u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>' % (action, self.service, params, action))
        req = urllib.request.Request(url or self.control_url, data=body.encode(), headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"%s#%s"' % (service, action)})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = r.read(200_000)
        except urllib.error.HTTPError as e:
            raise UPnPError(*_fault(e.read(200_000)))
        root = ET.fromstring(data)
        for el in root.iter():
            if _local(el.tag) == action + "Response":
                return {_local(c.tag): (c.text or "") for c in el}
        return {}

    def add_mapping(self, proto, ext_port, int_port, desc, lease):
        self.call("AddPortMapping", [
            ("NewRemoteHost", ""), ("NewExternalPort", ext_port), ("NewProtocol", proto),
            ("NewInternalPort", int_port), ("NewInternalClient", self.local_ip), ("NewEnabled", 1),
            ("NewPortMappingDescription", desc), ("NewLeaseDuration", lease)])

    def delete_mapping(self, proto, ext_port):
        self.call("DeletePortMapping", [("NewRemoteHost", ""), ("NewExternalPort", ext_port),
                                        ("NewProtocol", proto)])

    def external_ip(self):
        return self.call("GetExternalIPAddress").get("NewExternalIPAddress", "").strip()


def _fetch_xml(url, timeout=4):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read(400_000)
    data = data.lstrip(b"\xef\xbb\xbf \t\r\n\x00")   # BOM / blank space some routers put before <?xml
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return ET.fromstring(bytes(b for b in data if b >= 32 or b in (9, 10, 13)))   # stray control characters


def _describe(location, log=None, timeout=4):
    """Fetch a device description and return a Gateway for its WAN connection service, if any."""
    log = log or (lambda m: None)
    root = _fetch_xml(location, timeout)
    host = urllib.parse.urlparse(location).hostname
    base = location
    for el in root.iter():
        if _local(el.tag) == "URLBase" and (el.text or "").strip():
            ub = el.text.strip()
            if urllib.parse.urlparse(ub).hostname == host:
                base = ub
            else:  # routers sometimes advertise a wrong / internal address: trust where we actually got it from
                log("description %s: ignoring URLBase %s (different host)" % (location, ub))
    wan = None
    seen = []
    for svc in root.iter():
        if _local(svc.tag) != "service":
            continue
        info = {_local(c.tag): (c.text or "").strip() for c in svc}
        stype = info.get("serviceType", "")
        seen.append(stype.rsplit(":", 2)[-2] if stype.count(":") >= 2 else stype)
        if not info.get("controlURL"):
            continue
        url = urllib.parse.urljoin(base, info["controlURL"])
        if urllib.parse.urlparse(url).hostname != host:  # never follow a control URL to another host
            log("description %s: skipping %s control URL %s (other host)" % (location, stype, url))
            continue
        if any(w in stype for w in WAN_SERVICES) and wan is None:
            wan = (url, stype)
    if wan is None:
        log("description %s: no WANIPConnection / WANPPPConnection service (has: %s)" % (location, ", ".join(seen) or "none"))
        return None
    log("description %s: %s at %s" % (location, wan[1].rsplit(":", 2)[-2], wan[0]))
    return Gateway(wan[0], wan[1], host)


def discover(timeout=4.0, ssdp_addr=SSDP, log=None, gateways=(), interfaces=None, unicast_port=1900):
    log = log or (lambda m: None)
    locs = _ssdp_search(ssdp_addr, timeout, log, gateways, interfaces, unicast_port)
    log("SSDP: %d description URL(s): %s" % (len(locs), ", ".join(locs) or "none"))
    for loc in locs[:8]:
        try:
            gw = _describe(loc, log)
        except (OSError, ET.ParseError, ValueError) as e:
            log("description %s failed: %s" % (loc, e))
            continue
        if gw:
            return gw
    for g in gateways:   # SSDP got nowhere: try well-known description addresses on the gateway itself
        log("probing %d well-known UPnP description addresses on the gateway %s" % (len(FALLBACK_URLS), g))
        for port, path in FALLBACK_URLS:
            url = "http://%s:%d%s" % (g, port, path)
            try:
                gw = _describe(url, log, timeout=1.5)
            except (OSError, ET.ParseError, ValueError):
                continue
            if gw:
                log("found the router's description at the well-known address %s" % url)
                return gw
        log("gateway %s has no UPnP service on any well-known address: UPnP is most likely switched off in the router" % g)
    return None
