"""Play a torrent's file while it downloads: a tiny HTTP server on this computer (127.0.0.1 only) that hands a player
the file's bytes, waiting for the pieces it needs and asking the downloader to fetch them first.

    server = StreamServer(manager); server.start()
    url = server.url(torrent_hex, file_index, "Movie.mkv")        # give this to mpv / VLC / any player

Players seek with HTTP Range requests; each one moves the 'playhead', so the pieces around the new position are fetched
before anything else. Requests need an unguessable token (so a web page in your browser cannot read your files) and a
localhost Host header (against DNS rebinding)."""
import ipaddress
import json
import mimetypes
import re
import secrets
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

CHUNK = 256 * 1024
MEDIA_TYPES = {".mkv": "video/x-matroska", ".mka": "audio/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4",
               ".webm": "video/webm", ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".ts": "video/mp2t",
               ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
               ".m4a": "audio/mp4", ".wav": "audio/wav", ".srt": "application/x-subrip", ".ass": "text/x-ssa"}
VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".webm", ".avi", ".mov", ".ts", ".wmv", ".flv", ".mpg", ".mpeg", ".m2ts", ".ogv", ".3gp")
AUDIO_EXT = (".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".wma", ".mka", ".ape")


def media_kind(name):
    n = name.lower()
    return "video" if n.endswith(VIDEO_EXT) else "audio" if n.endswith(AUDIO_EXT) else ""


SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub")
JUNK_EXT = (".nfo", ".txt", ".jpg", ".jpeg", ".png", ".url", ".exe", ".html", ".htm", ".website", ".sfv", ".diz", ".db")


def natural_key(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def is_sample(path):
    """A 'Sample' folder or file next to the real video."""
    low = [p.lower() for p in path]
    return any(re.search(r"(^|[\W_])sample([\W_]|$)", p) for p in low)


def suggest_skips(files, keep=()):
    """Indexes of files not worth downloading when you only want to watch: samples, .nfo/.jpg/.txt/.exe extras.
    `files` is [(path_tuple, size, progress)]; files already started are left alone."""
    out = []
    for i, (path, size, prog) in enumerate(files):
        name = path[-1].lower()
        if i in keep or prog > 0:
            continue
        if name.endswith(JUNK_EXT) or (is_sample(path) and media_kind(name)):
            out.append(i)
    return out


def playlist_for(files, start_idx):
    """Media files of the same kind, in natural (episode) order, from `start_idx` onwards. files: [(path_tuple, size)]."""
    kind = media_kind(files[start_idx][0][-1])
    items = [i for i, (p, _s) in enumerate(files) if media_kind(p[-1]) == kind and not is_sample(p)]
    items.sort(key=lambda i: natural_key("/".join(files[i][0])))
    if start_idx not in items:
        items.append(start_idx)
    k = items.index(start_idx)
    return items[k:] + items[:0]


def subtitles_for(files, video_idx):
    """Subtitle files that go with a video: same folder (or a Subs folder) and mentioning its base name, else all of the
    folder's when there is only one video there."""
    vpath = files[video_idx][0]
    base = re.sub(r"\.[^.]+$", "", vpath[-1]).lower()
    out = []
    for i, (p, _s) in enumerate(files):
        if not p[-1].lower().endswith(SUB_EXT):
            continue
        sub = re.sub(r"\.[^.]+$", "", p[-1]).lower()
        if sub.startswith(base) or base.startswith(sub) or any(part.lower() in ("subs", "subtitles") for part in p[:-1]) and base in "/".join(p).lower():
            out.append(i)
    return out


def content_type(name):
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return MEDIA_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def parse_range(header, size):
    """'bytes=a-b' -> (start, end) inclusive, or None for no/invalid header, 'bad' when unsatisfiable."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):                                   # suffix: the last n bytes
        n = int(m.group(2))
        return (max(0, size - n), size - 1) if n else "bad"
    start = int(m.group(1))
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    return "bad" if start >= size or end < start else (start, end)


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class StreamServer:
    def __init__(self, mgr):
        self.mgr = mgr
        self.token = secrets.token_urlsafe(24)
        self.httpd = None
        self.port = 0
        self.closing = False
        self.lan_httpd, self.lan_port, self.lan_ip, self.lan_token = None, 0, "", ""
        self.lan_expires, self.lan_hits = 0.0, 0

    def start(self):
        self.httpd = self._serve("127.0.0.1", self.token, ("s", "map", "hint", "prefetch"), ("127.0.0.1", "localhost"))
        self.port = self.httpd.server_address[1]
        return self.port

    def start_lan(self, ip, hours=8):
        """Also serve the video itself (no control routes) on this computer's network address so a TV or phone on the
        same network can open the link. Anyone who has the link can watch: it contains a random secret."""
        if self.lan_httpd:
            return self.lan_port
        self.lan_token = secrets.token_urlsafe(24)
        self.lan_expires = time.time() + max(0.05, hours) * 3600       # the link stops working after this
        self.lan_hits = 0
        self.lan_httpd = self._serve(ip, self.lan_token, ("s",), (ip,), lan=True)
        self.lan_ip, self.lan_port = ip, self.lan_httpd.server_address[1]
        return self.lan_port

    def stop_lan(self):
        if self.lan_httpd:
            try:
                self.lan_httpd.shutdown(); self.lan_httpd.server_close()
            except Exception:
                pass
        self.lan_httpd, self.lan_port, self.lan_ip = None, 0, ""

    def lan_active(self):
        return bool(self.lan_httpd) and time.time() < self.lan_expires

    def lan_url(self, h, idx, name):
        if not self.lan_httpd:
            return ""
        return "http://%s:%d/s/%s/%s/%d/%s" % (self.lan_ip, self.lan_port, self.lan_token, h, idx, urllib.parse.quote(name))

    def _serve(self, bind, token, kinds, hosts, lan=False):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _fail(self, code, text=""):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def _route(self):
                """(kind, hash, file index) or None."""
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in hosts:
                    return None
                if lan:                                            # only devices on this network, only until the link expires
                    try:
                        peer = ipaddress.ip_address(self.client_address[0])
                    except ValueError:
                        return None
                    if not (peer.is_private or peer.is_loopback or peer.is_link_local) or time.time() > outer.lan_expires:
                        return None
                    outer.lan_hits += 1
                parts = urllib.parse.urlsplit(self.path).path.split("/")      # ['', kind, token, hash, idx, name]
                if len(parts) < 5 or parts[1] not in kinds or not secrets.compare_digest(parts[2], token):
                    return None
                try:
                    return parts[1], parts[3], int(parts[4])
                except ValueError:
                    return None

            def do_HEAD(self):
                self._serve(head=True)

            def do_GET(self):
                self._serve(head=False)

            def _serve(self, head):
                route = self._route()
                if route is None:
                    return self._fail(403)
                kind, h, idx = route
                info = outer.mgr.stream_info(h, idx)
                if info is None:
                    return self._fail(404)
                if kind in ("hint", "prefetch"):
                    ok = False
                    if kind == "hint":
                        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                        try:
                            ok = bool(outer.mgr.stream_hint(h, idx, float(q.get("dur", ["0"])[0])))
                        except ValueError:
                            ok = False
                    else:
                        ok = outer.mgr.stream_prefetch(h, idx)
                    body = b"ok" if ok else b"no"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if not head:
                        self.wfile.write(body)
                    return
                if kind == "map":                                  # which byte ranges are downloaded (for the player's seek bar)
                    body = json.dumps(info).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if not head:
                        self.wfile.write(body)
                    return
                size = info["size"]
                name = urllib.parse.unquote(self.path.rsplit("/", 1)[-1].split("?")[0])
                rng = parse_range(self.headers.get("Range"), size)
                if rng == "bad":
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = rng if rng else (0, size - 1)
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", content_type(name))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                if rng:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("transferMode.dlna.org", "Streaming")           # TVs and cast devices look for these
                self.send_header("contentFeatures.dlna.org", "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
                self.end_headers()
                if head or size == 0:
                    return
                pos = start
                touch = "nt=1" not in urllib.parse.urlsplit(self.path).query      # thumbnails must not move the playhead
                while pos <= end and not outer.closing:
                    n = min(CHUNK, end - pos + 1)
                    data = outer.mgr.stream_read(h, idx, pos, n, cancel=lambda: outer.closing, touch=touch)
                    if data is None or len(data) != n:
                        self.close_connection = True               # timeout / torrent removed: the player sees a cut connection
                        return
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionError, OSError):
                        self.close_connection = True               # the player went away or seeked elsewhere
                        return
                    pos += n

        httpd = _Server((bind, 0), H)
        threading.Thread(target=httpd.serve_forever, name="stream-server", daemon=True).start()
        return httpd

    def url(self, h, idx, name):
        return "http://127.0.0.1:%d/s/%s/%s/%d/%s" % (self.port, self.token, h, idx, urllib.parse.quote(name))

    def hint_url(self, h, idx):
        return "http://127.0.0.1:%d/hint/%s/%s/%d/x" % (self.port, self.token, h, idx)

    def prefetch_url(self, h, idx):
        return "http://127.0.0.1:%d/prefetch/%s/%s/%d/x" % (self.port, self.token, h, idx)

    def map_url(self, h, idx):
        return "http://127.0.0.1:%d/map/%s/%s/%d/x" % (self.port, self.token, h, idx)

    def stop(self):
        self.closing = True
        self.stop_lan()
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
