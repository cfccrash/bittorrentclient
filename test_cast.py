"""Cast tests against fake devices on localhost: a UPnP/DLNA renderer (SSDP + SOAP) and a Chromecast (mDNS + CASTV2 over TLS)."""
import json, os, shutil, socket, ssl, struct, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cast


def dns_name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\x00"


def rr(name, typ, rdata, ttl=120):
    return dns_name(name) + struct.pack(">HHIH", typ, 1, ttl, len(rdata)) + rdata


def mdns_response():
    inst = "Living-Room-abc._googlecast._tcp.local"
    host = "abc.local"
    txt = b"".join(bytes([len(x)]) + x for x in (b"id=abc", b"fn=Living Room TV"))
    recs = [rr("_googlecast._tcp.local", 12, dns_name(inst)),
            rr(inst, 33, struct.pack(">HHH", 0, 0, CAST_PORT[0]) + dns_name(host)),
            rr(inst, 16, txt), rr(host, 1, socket.inet_aton("127.0.0.1"))]
    return struct.pack(">6H", 0, 0x8400, 0, len(recs), 0, 0) + b"".join(recs)


CAST_PORT = [0]
GOT = {"soap": [], "cast": []}


def main():
    # ---- protobuf framing
    raw = cast.encode_cast("sender-0", "receiver-0", cast.NS_CONN, '{"type":"CONNECT"}')
    assert cast.decode_cast(raw) == dict(src="sender-0", dst="receiver-0", ns=cast.NS_CONN, payload='{"type":"CONNECT"}')
    big = cast.encode_cast("a", "b", "c", "x" * 300)
    assert cast.decode_cast(big)["payload"] == "x" * 300
    recs = cast.parse_mdns(mdns_response())
    assert ("Living-Room-abc._googlecast._tcp.local", "TXT", {"id": "abc", "fn": "Living Room TV"}) in recs
    print("cast protobuf framing and DNS parsing: OK")

    # ---- fake DLNA renderer
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            xml = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName>Bedroom TV</friendlyName>'
                   '<serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>'
                   '<controlURL>/ctl/AVT</controlURL></service></serviceList></device></root>').encode()
            self.send_response(200); self.send_header("Content-Length", str(len(xml))); self.end_headers(); self.wfile.write(xml)
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            GOT["soap"].append((self.headers["SOAPACTION"].strip('"').split("#")[1], body))
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    httpd = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); ssdp.bind(("127.0.0.1", 0))
    def ssdp_loop():
        while True:
            try:
                data, src = ssdp.recvfrom(2048)
            except OSError:
                return
            if b"M-SEARCH" in data:
                ssdp.sendto(("HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1:%d/desc.xml\r\nST: x\r\n\r\n" % httpd.server_address[1]).encode(), src)
    threading.Thread(target=ssdp_loop, daemon=True).start()
    devs = cast.discover_dlna(1.5, addr=("127.0.0.1", ssdp.getsockname()[1]))
    assert len(devs) == 1 and devs[0].name == "Bedroom TV" and devs[0].control.endswith("/ctl/AVT"), devs
    d = devs[0]
    d.play("http://192.168.1.5:5000/s/tok/h/0/My%20Movie.mkv", "My <Movie>", "video/x-matroska"); d.pause(); d.resume(); d.seek(3725); d.stop()
    acts = [a for a, _ in GOT["soap"]]
    assert acts == ["SetAVTransportURI", "Play", "Pause", "Play", "Seek", "Stop"], acts
    assert "http://192.168.1.5:5000/s/tok/h/0/My%20Movie.mkv" in GOT["soap"][0][1] and "&amp;lt;Movie&amp;gt;" in GOT["soap"][0][1]
    assert "01:02:05" in GOT["soap"][4][1]
    print("DLNA: SSDP discovery, description, SetAVTransportURI/Play/Pause/Seek/Stop: OK")
    ssdp.close(); httpd.shutdown()

    # ---- fake Chromecast
    if not shutil.which("openssl"):
        print("Chromecast test: SKIPPED (no openssl to make a certificate)"); print("CAST TESTS: ALL PASSED"); return
    tmp = tempfile.mkdtemp()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", tmp + "/k.pem", "-out", tmp + "/c.pem",
                    "-days", "1", "-subj", "/CN=fake"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(tmp + "/c.pem", tmp + "/k.pem")
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(2); CAST_PORT[0] = ls.getsockname()[1]
    def recv_msg(c):
        h = b""
        while len(h) < 4:
            x = c.recv(4 - len(h))
            if not x: raise EOFError
            h += x
        n = struct.unpack(">I", h)[0]; b = b""
        while len(b) < n: b += c.recv(n - len(b))
        return cast.decode_cast(b)
    def send_msg(c, src, dst, ns, obj):
        r = cast.encode_cast(src, dst, ns, json.dumps(obj)); c.sendall(struct.pack(">I", len(r)) + r)
    def serve():
        c, _ = ls.accept(); c = ctx.wrap_socket(c, server_side=True)
        try:
            while True:
                m = recv_msg(c); o = json.loads(m["payload"]); GOT["cast"].append((m["ns"], m["dst"], o))
                if o["type"] == "LAUNCH":
                    send_msg(c, "receiver-0", "sender-0", cast.NS_RECV, {"type": "RECEIVER_STATUS", "requestId": o["requestId"],
                             "status": {"applications": [{"appId": "CC1AD845", "transportId": "web-5", "displayName": "Default Media Receiver"}]}})
                elif o["type"] == "LOAD":
                    send_msg(c, "web-5", "sender-0", cast.NS_MEDIA, {"type": "MEDIA_STATUS", "requestId": o["requestId"],
                             "status": [{"mediaSessionId": 7, "playerState": "PLAYING"}]})
        except (EOFError, OSError, ssl.SSLError):
            pass
    threading.Thread(target=serve, daemon=True).start()
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); us.bind(("127.0.0.1", 0))
    def mdns_loop():
        while True:
            try: data, src = us.recvfrom(2048)
            except OSError: return
            us.sendto(mdns_response(), src)
    threading.Thread(target=mdns_loop, daemon=True).start()
    found = cast.discover_cast(1.5, addr=("127.0.0.1", us.getsockname()[1]))
    assert len(found) == 1 and found[0].name == "Living Room TV" and found[0].port == CAST_PORT[0], found
    cd = found[0]
    cd.play("http://192.168.1.5:5000/s/tok/h/0/a.mkv", "A", "video/x-matroska")
    cd.pause(); cd.seek(42); cd.resume(); cd.stop(); time.sleep(0.4)
    types = [(o["type"], dst) for _ns, dst, o in GOT["cast"]]
    assert ("CONNECT", "receiver-0") in types and ("LAUNCH", "receiver-0") in types and ("CONNECT", "web-5") in types, types
    load = [o for _ns, _d, o in GOT["cast"] if o["type"] == "LOAD"][0]
    assert load["media"]["contentId"].endswith("/a.mkv") and load["media"]["contentType"] == "video/x-matroska" and load["autoplay"]
    media = [(o["type"], o.get("mediaSessionId")) for _ns, _d, o in GOT["cast"] if o["type"] in ("PAUSE", "PLAY", "SEEK", "STOP") and o.get("mediaSessionId")]
    assert media == [("PAUSE", 7), ("SEEK", 7), ("PLAY", 7), ("STOP", 7)], media
    seek = [o for _ns, _d, o in GOT["cast"] if o["type"] == "SEEK"][0]; assert seek["currentTime"] == 42.0
    cd.close(); us.close(); ls.close()
    print("Chromecast: mDNS discovery, TLS CASTV2 connect, launch, LOAD, pause/seek/play/stop (fake device): OK")
    print("CAST TESTS: ALL PASSED")


if __name__ == "__main__":
    main()
