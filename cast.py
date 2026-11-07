"""Send a stream to a TV or speaker on the local network.

* DLNA / UPnP media renderers (most smart TVs, game consoles, receivers): found with SSDP, controlled with AVTransport.
* Google Cast (Chromecast, Google TV, Nest speakers): found with mDNS, controlled over the CASTV2 protocol (TLS on
  port 8009) using the Default Media Receiver.

Both pull the video themselves from the address you give them, so it must be reachable from the network (see the
"Copy link for TV / phone" sharing in stream.py). Pure standard library."""
import json
import re
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SSDP_ADDR = ("239.255.255.250", 1900)
MDNS_ADDR = ("224.0.0.251", 5353)
AVT = "urn:schemas-upnp-org:service:AVTransport:1"


class CastError(Exception):
    pass


# =========================================================== DLNA / UPnP
def _local_ip_for(addr):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(addr)
        return s.getsockname()[0]
    finally:
        s.close()


def _ssdp_search(timeout, addr=SSDP_ADDR):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: %s:%d\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n" % addr).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.3)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    found = {}
    try:
        for _ in range(2):
            s.sendto(msg, addr)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, src = s.recvfrom(4096)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                found[m.group(1).decode()] = src[0]
    finally:
        s.close()
    return list(found)


def _describe(location):
    with urllib.request.urlopen(location, timeout=4) as r:
        root = ET.fromstring(r.read())
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    dev = root.find("d:device", ns)
    if dev is None:
        return None
    name = (dev.findtext("d:friendlyName", "", ns) or "TV").strip()
    base = (root.findtext("d:URLBase", "", ns) or "").strip() or location
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        st = svc.findtext("{urn:schemas-upnp-org:device-1-0}serviceType", "")
        if "AVTransport" in st:
            ctl = svc.findtext("{urn:schemas-upnp-org:device-1-0}controlURL", "")
            return name, st, urllib.parse.urljoin(base, ctl)
    return None


class DlnaDevice:
    kind = "DLNA"

    def __init__(self, name, service, control):
        self.name, self.service, self.control = name, service, control

    def _soap(self, action, args):
        body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>'
                % (action, self.service, "".join("<%s>%s</%s>" % (k, v, k) for k, v in args), action)).encode()
        req = urllib.request.Request(self.control, data=body, method="POST", headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": '"%s#%s"' % (self.service, action)})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.read()
        except Exception as e:
            raise CastError("%s: %s" % (self.name, e))

    def play(self, url, title="Video", content_type="video/mp4"):
        didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="0" parentID="-1" restricted="1"><dc:title>%s</dc:title>'
                '<upnp:class>object.item.%sItem</upnp:class><res protocolInfo="http-get:*:%s:*">%s</res></item></DIDL-Lite>'
                % (escape(title), "audio" if content_type.startswith("audio") else "video", content_type, escape(url)))
        self._soap("SetAVTransportURI", [("InstanceID", "0"), ("CurrentURI", escape(url)), ("CurrentURIMetaData", escape(didl))])
        self._soap("Play", [("InstanceID", "0"), ("Speed", "1")])

    def pause(self):
        self._soap("Pause", [("InstanceID", "0")])

    def resume(self):
        self._soap("Play", [("InstanceID", "0"), ("Speed", "1")])

    def stop(self):
        self._soap("Stop", [("InstanceID", "0")])

    def seek(self, secs):
        t = "%02d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)
        self._soap("Seek", [("InstanceID", "0"), ("Unit", "REL_TIME"), ("Target", t)])

    def close(self):
        pass


def discover_dlna(timeout=3.0, addr=SSDP_ADDR):
    out = []
    for loc in _ssdp_search(timeout, addr):
        try:
            d = _describe(loc)
        except Exception:
            continue
        if d:
            out.append(DlnaDevice(*d))
    return out


# =========================================================== Google Cast
def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _field_s(num, text):
    raw = text.encode()
    return _varint(num << 3 | 2) + _varint(len(raw)) + raw


def encode_cast(src, dst, namespace, payload):
    """CastMessage protobuf: protocol_version=0, source_id, destination_id, namespace, payload_type=STRING, payload_utf8."""
    return (_varint(1 << 3) + _varint(0) + _field_s(2, src) + _field_s(3, dst) + _field_s(4, namespace)
            + _varint(5 << 3) + _varint(0) + _field_s(6, payload))


def decode_cast(data):
    out, i = {}, 0
    while i < len(data):
        key, i = _read_varint(data, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(data, i)
        elif wire == 2:
            ln, i = _read_varint(data, i)
            val, i = data[i:i + ln], i + ln
        else:
            raise CastError("unexpected protobuf field")
        out[num] = val
    return dict(src=out.get(2, b"").decode(), dst=out.get(3, b"").decode(), ns=out.get(4, b"").decode(),
                payload=out.get(6, b"").decode("utf-8", "replace"))


def _read_varint(data, i):
    n = shift = 0
    while True:
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


NS_CONN, NS_HB, NS_RECV, NS_MEDIA = ("urn:x-cast:com.google.cast.tp.connection", "urn:x-cast:com.google.cast.tp.heartbeat",
                                     "urn:x-cast:com.google.cast.receiver", "urn:x-cast:com.google.cast.media")


class CastDevice:
    kind = "Chromecast"

    def __init__(self, name, host, port=8009):
        self.name, self.host, self.port = name, host, port
        self.sock = None
        self.lock = threading.Lock()
        self.req = 0
        self.transport = None
        self.media_session = None
        self.events = []
        self.alive = False

    # -- transport
    def _send(self, dst, ns, obj):
        raw = encode_cast("sender-0", dst, ns, json.dumps(obj))
        with self.lock:
            self.sock.sendall(struct.pack(">I", len(raw)) + raw)

    def _recv(self, timeout):
        self.sock.settimeout(timeout)
        hdr = b""
        while len(hdr) < 4:
            c = self.sock.recv(4 - len(hdr))
            if not c:
                raise CastError("connection closed")
            hdr += c
        n = struct.unpack(">I", hdr)[0]
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise CastError("connection closed")
            buf += c
        return decode_cast(buf)

    def _reader(self):
        while self.alive:
            try:
                m = self._recv(1.0)
            except socket.timeout:
                continue
            except Exception:
                self.alive = False
                return
            try:
                obj = json.loads(m["payload"])
            except ValueError:
                continue
            if obj.get("type") == "PING":
                try:
                    self._send("receiver-0", NS_HB, {"type": "PONG"})
                except Exception:
                    pass
            elif obj.get("type") == "MEDIA_STATUS" and obj.get("status"):
                self.media_session = obj["status"][0].get("mediaSessionId", self.media_session)
            self.events.append(obj)

    def _heartbeat(self):
        while self.alive:
            time.sleep(5)
            try:
                self._send("receiver-0", NS_HB, {"type": "PING"})
            except Exception:
                self.alive = False

    def _wait_for(self, pred, timeout=10):
        end = time.time() + timeout
        seen = 0
        while time.time() < end:
            while seen < len(self.events):
                ev = self.events[seen]
                seen += 1
                if pred(ev):
                    return ev
            if not self.alive:
                break
            time.sleep(0.05)
        raise CastError("%s did not answer" % self.name)

    def connect(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE                       # Cast devices use self-signed certificates
        raw = socket.create_connection((self.host, self.port), timeout=6)
        self.sock = ctx.wrap_socket(raw)
        self.alive = True
        self._send("receiver-0", NS_CONN, {"type": "CONNECT"})
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _rid(self):
        self.req += 1
        return self.req

    def play(self, url, title="Video", content_type="video/mp4"):
        if not self.alive:
            self.connect()
        self._send("receiver-0", NS_RECV, {"type": "LAUNCH", "appId": "CC1AD845", "requestId": self._rid()})
        ev = self._wait_for(lambda o: o.get("type") == "RECEIVER_STATUS" and any(
            a.get("appId") == "CC1AD845" and a.get("transportId") for a in o.get("status", {}).get("applications", [])), 15)
        app = [a for a in ev["status"]["applications"] if a.get("appId") == "CC1AD845"][0]
        self.transport = app["transportId"]
        self._send(self.transport, NS_CONN, {"type": "CONNECT"})
        self._send(self.transport, NS_MEDIA, {
            "type": "LOAD", "requestId": self._rid(), "autoplay": True, "currentTime": 0,
            "media": {"contentId": url, "contentType": content_type, "streamType": "BUFFERED",
                      "metadata": {"metadataType": 0, "title": title}}})
        try:
            self._wait_for(lambda o: o.get("type") in ("MEDIA_STATUS", "LOAD_FAILED", "INVALID_REQUEST"), 15)
        except CastError:
            pass

    def _media(self, typ, **extra):
        if not self.transport or self.media_session is None:
            raise CastError("nothing is playing on %s yet" % self.name)
        self._send(self.transport, NS_MEDIA, dict(type=typ, requestId=self._rid(), mediaSessionId=self.media_session, **extra))

    def pause(self):
        self._media("PAUSE")

    def resume(self):
        self._media("PLAY")

    def seek(self, secs):
        self._media("SEEK", currentTime=float(secs))

    def stop(self):
        self._media("STOP")
        self._send("receiver-0", NS_RECV, {"type": "STOP", "requestId": self._rid()})

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


def _dns_name(data, i):
    labels, jumped, end = [], False, 0
    while True:
        ln = data[i]
        if ln == 0:
            i += 1
            break
        if ln & 0xC0 == 0xC0:
            ptr = ((ln & 0x3F) << 8) | data[i + 1]
            if not jumped:
                end = i + 2
            jumped = True
            i = ptr
            continue
        labels.append(data[i + 1:i + 1 + ln].decode("utf-8", "replace"))
        i += 1 + ln
    return ".".join(labels), (end if jumped else i)


def parse_mdns(data):
    """-> list of (name, type, value) from a DNS message: PTR/SRV/A/TXT records of all sections."""
    qd, an, ns, ar = struct.unpack(">4H", data[4:12])
    i = 12
    for _ in range(qd):
        _n, i = _dns_name(data, i)
        i += 4
    out = []
    for _ in range(an + ns + ar):
        name, i = _dns_name(data, i)
        typ, _cls, _ttl, ln = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        rd = data[i:i + ln]
        if typ == 12:
            out.append((name, "PTR", _dns_name(data, i)[0]))
        elif typ == 33:
            port = struct.unpack(">H", rd[4:6])[0]
            out.append((name, "SRV", (_dns_name(data, i + 6)[0], port)))
        elif typ == 1 and ln == 4:
            out.append((name, "A", socket.inet_ntoa(rd)))
        elif typ == 16:
            txt, j = {}, 0
            while j < len(rd):
                l = rd[j]
                kv = rd[j + 1:j + 1 + l].decode("utf-8", "replace")
                j += 1 + l
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    txt[k] = v
            out.append((name, "TXT", txt))
        i += ln
    return out


def _mdns_query(name="_googlecast._tcp.local"):
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return struct.pack(">6H", 0, 0, 1, 0, 0, 0) + q + struct.pack(">HH", 12, 0x8001)   # PTR, class IN + "unicast response"


def discover_cast(timeout=3.0, addr=MDNS_ADDR):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.3)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    ptr, srv, addrs, txt = set(), {}, {}, {}
    try:
        for _ in range(2):
            s.sendto(_mdns_query(), addr)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, src = s.recvfrom(9000)
            except socket.timeout:
                continue
            try:
                recs = parse_mdns(data)
            except Exception:
                continue
            for name, typ, val in recs:
                if typ == "PTR" and name.startswith("_googlecast"):
                    ptr.add(val)
                elif typ == "SRV":
                    srv[name] = val
                elif typ == "A":
                    addrs[name] = val
                elif typ == "TXT":
                    txt[name] = val
            for inst in ptr:                                     # some devices answer with only the PTR: ask for the rest
                if inst in srv and srv[inst][0] not in addrs:
                    addrs[srv[inst][0]] = src[0]
    finally:
        s.close()
    out = []
    for inst in sorted(ptr):
        if inst in srv:
            host, port = srv[inst]
            ip = addrs.get(host)
            if ip:
                name = (txt.get(inst) or {}).get("fn") or inst.split("._googlecast")[0]
                out.append(CastDevice(name, ip, port))
    return out


def discover(timeout=3.0):
    """All cast targets found within `timeout` seconds (DLNA and Chromecast scanned at the same time)."""
    res = {"d": [], "c": []}

    def a():
        try:
            res["d"] = discover_dlna(timeout)
        except Exception:
            pass

    def b():
        try:
            res["c"] = discover_cast(timeout)
        except Exception:
            pass
    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return res["c"] + res["d"]
