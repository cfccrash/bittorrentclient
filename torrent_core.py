"""
Bit Torrent Client core engine - a small, dependency-free BitTorrent client library.

Implements: .torrent parsing (single + multi file), HTTP and UDP trackers,
the peer wire protocol (BEP 3), piece verification, resume via re-check,
rarest-first piece picking, end-game mode, seeding / uploading and an
inbound listener.

Magnet links are supported (BEP 9/10) via trackers and x.pe peers in the link.
Trackerless peer discovery uses a Kademlia DHT (BEP 5, IPv4 only), also honouring the private
flag (BEP 27). Peer exchange (BEP 11), Message Stream Encryption (mse.py), uTP (utp.py, BEP 29),
global speed limits and automatic router port forwarding
(PCP, NAT-PMP, UPnP - portmap.py) are supported. Not implemented: BEP 42 node-id hardening.
"""
import asyncio
import base64
import bisect
import collections
import hashlib
import ipaddress
import os
import random
import re
import shutil
import socket
import struct
import threading
import time
import types

import mse
import portmap
import utp
import urllib.parse
import urllib.request

BLOCK = 16384
PSTR = b"\x13BitTorrent protocol"
MAX_PEERS = 50
MAX_OUT = 40
LOW_SPACE = 32 * 1024 * 1024   # stop a download when the disk has less than this free


def _size_text(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


READ_CHUNK = 262144          # uploads read the disk this much at a time and serve blocks from memory
READ_CACHE_CHUNKS = 64      # ... keeping the last 16 MiB per torrent
STREAM_AHEAD = 32 * 1024 * 1024
STREAM_AHEAD_MAX = 256 * 1024 * 1024   # cap for the bitrate-based look-ahead
STREAM_SECONDS = 45                    # seconds of video to keep ready ahead of the playhead   # while streaming: keep this much after the playback position coming first
STREAM_NOW = 2 * 1024 * 1024      # ... and these first bytes before anything else
STREAM_TAIL = 3 * 1024 * 1024     # the end of the file is fetched early too (MP4 indexes often live there)
STREAM_IDLE = 120                 # seconds without a read before streaming priority lapses
UNCHOKE_SLOTS = 4            # peers we upload to by merit; one more slot rotates (the optimistic unchoke)
OPT_ROTATE = 30             # seconds the optimistic slot stays with one peer
IDLE_CLOSE = 150            # seconds without any message from a peer before it is dropped
MAX_PIPELINE = 64   # most block requests kept in flight to one peer


class RateLimiter:
    """Global byte-rate limiter. rate is bytes/second; 0 means unlimited."""

    def __init__(self, rate=0):
        self.rate = rate
        self.next = 0.0

    async def acquire(self, n):
        if self.rate <= 0:
            return
        now = time.monotonic()
        start = max(now, self.next)
        self.next = start + n / self.rate
        if start > now:
            await asyncio.sleep(start - now)


# --------------------------------------------------------------------------
# Bencode
# --------------------------------------------------------------------------
def bencode(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, (bytes, bytearray)):
        return b"%d:%s" % (len(v), bytes(v))
    if isinstance(v, list):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(bencode(k) + bencode(x) for k, x in items) + b"e"
    raise TypeError("cannot bencode %r" % type(v))


def _dec(d, i):
    c = d[i:i + 1]
    if c == b"i":
        j = d.index(b"e", i)
        return int(d[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while d[i:i + 1] != b"e":
            v, i = _dec(d, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while d[i:i + 1] != b"e":
            k, i = _dec(d, i)
            v, i = _dec(d, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        j = d.index(b":", i)
        n = int(d[i:j])
        j += 1
        if j + n > len(d):
            raise ValueError("truncated string")
        return d[j:j + n], j + n
    raise ValueError("bad bencode data")


def bdecode(data):
    v, end = _dec(data, 0)
    return v


def _decode_torrent(data):
    """Decode a .torrent and also return the raw byte span of the info dict."""
    if data[:1] != b"d":
        raise ValueError("not a .torrent file")
    i = 1
    meta, span = {}, None
    while data[i:i + 1] != b"e":
        k, i = _dec(data, i)
        start = i
        v, i = _dec(data, i)
        if k == b"info":
            span = (start, i)
        meta[k] = v
    if span is None:
        raise ValueError("torrent has no info dictionary")
    return meta, span


# --------------------------------------------------------------------------
# Torrent metadata
# --------------------------------------------------------------------------
def _safe(part):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part).strip(" .")
    return s or "_"


class Torrent:
    def __init__(self, data):
        meta, (s, e) = _decode_torrent(data)
        info = meta[b"info"]
        self.private = info.get(b"private") == 1
        self.info_raw = bytes(data[s:e])
        self.info_hash = hashlib.sha1(self.info_raw).digest()
        self.hex = self.info_hash.hex()
        self.name = _safe(info[b"name"].decode("utf-8", "replace"))
        self.piece_length = int(info[b"piece length"])
        pieces = info[b"pieces"]
        if len(pieces) % 20 or self.piece_length <= 0:
            raise ValueError("invalid piece data")
        self.hashes = [pieces[i:i + 20] for i in range(0, len(pieces), 20)]
        self.files = []  # (path_parts, length, offset)
        if b"files" in info:
            self.multi = True
            off = 0
            for f in info[b"files"]:
                parts = [self.name] + [_safe(p.decode("utf-8", "replace")) for p in f[b"path"]]
                ln = int(f[b"length"])
                self.files.append((parts, ln, off))
                off += ln
            self.length = off
        else:
            self.multi = False
            self.length = int(info[b"length"])
            self.files.append(([self.name], self.length, 0))
        expected = (self.length + self.piece_length - 1) // self.piece_length
        if self.length <= 0 or expected != len(self.hashes):
            raise ValueError("piece count does not match torrent length")
        trackers = []
        for tier in meta.get(b"announce-list", []):
            for u in tier:
                trackers.append(u.decode("utf-8", "replace"))
        if b"announce" in meta:
            trackers.insert(0, meta[b"announce"].decode("utf-8", "replace"))
        seen = []
        for t in trackers:
            if t not in seen and t.startswith(("http://", "https://", "udp://")):
                seen.append(t)
        self.trackers = seen

    def piece_size(self, i):
        if i < len(self.hashes) - 1:
            return self.piece_length
        return self.length - self.piece_length * (len(self.hashes) - 1)


def make_torrent(path, piece_length=65536, announce=None):
    """Create .torrent bytes for a file or folder (used for tests)."""
    path = os.path.abspath(path)
    name = os.path.basename(path)
    if os.path.isdir(path):
        flist = []
        for root, _, names in os.walk(path):
            for n in sorted(names):
                flist.append(os.path.join(root, n))
        flist.sort()
        blob = b"".join(open(f, "rb").read() for f in flist)
        files = [{"length": os.path.getsize(f),
                  "path": os.path.relpath(f, path).replace("\\", "/").split("/")} for f in flist]
        info = {"name": name, "files": files}
    else:
        blob = open(path, "rb").read()
        info = {"name": name, "length": len(blob)}
    pieces = b"".join(hashlib.sha1(blob[i:i + piece_length]).digest()
                      for i in range(0, len(blob), piece_length))
    info.update({"piece length": piece_length, "pieces": pieces})
    meta = {"info": info}
    if announce:
        meta["announce"] = announce
    return bencode(meta)


def auto_piece_length(total):
    """Piece size aiming for roughly 1000-2000 pieces (256 KiB .. 16 MiB)."""
    size = 256 * 1024
    while size < 16 * 1024 * 1024 and total / size > 2000:
        size *= 2
    return size


def create_torrent(path, trackers=(), piece_length=0, private=False, comment="", progress=None, cancel=None):
    """Hash a file or folder (streaming, so big files are fine) and return .torrent bytes.
    progress(fraction) is called now and then; cancel() returning True aborts with InterruptedError."""
    path = os.path.abspath(path)
    name = os.path.basename(path.rstrip("\\/")) or path
    if os.path.isdir(path):
        flist = []
        for root, _d, names in os.walk(path):
            for n in names:
                flist.append(os.path.join(root, n))
        flist.sort()
        if not flist:
            raise ValueError("the folder is empty")
        files = [{"length": os.path.getsize(f), "path": os.path.relpath(f, path).replace("\\", "/").split("/")}
                 for f in flist]
        info = {"name": name, "files": files}
    else:
        flist = [path]
        info = {"name": name, "length": os.path.getsize(path)}
    total = sum(os.path.getsize(f) for f in flist)
    if total <= 0:
        raise ValueError("nothing to share: the file is empty")
    pl = int(piece_length) or auto_piece_length(total)
    pieces, buf, done = [], b"", 0
    for f in flist:
        with open(f, "rb") as fh:
            while True:
                if cancel and cancel():
                    raise InterruptedError("cancelled")
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                buf += chunk
                while len(buf) >= pl:
                    pieces.append(hashlib.sha1(buf[:pl]).digest())
                    buf = buf[pl:]
                if progress:
                    progress(done / total)
    if buf:
        pieces.append(hashlib.sha1(buf).digest())
    info.update({"piece length": pl, "pieces": b"".join(pieces)})
    if private:
        info["private"] = 1
    meta = {"info": info, "created by": "Bit Torrent Client", "creation date": int(time.time())}
    trackers = [t.strip() for t in trackers if t and t.strip()]
    if trackers:
        meta["announce"] = trackers[0]
        if len(trackers) > 1:
            meta["announce-list"] = [[t] for t in trackers]
    if comment:
        meta["comment"] = comment
    return bencode(meta)


# --------------------------------------------------------------------------
# Disk storage
# --------------------------------------------------------------------------
class Storage:
    def __init__(self, t, root):
        self.t = t
        self.paths = [os.path.join(root, *f[0]) for f in t.files]
        self.starts = [f[2] for f in t.files]
        self.handles = {}
        self.lock = threading.Lock()

    def prepare(self):
        for i, (_, ln, _) in enumerate(self.t.files):
            if ln == 0 and not os.path.exists(self.paths[i]):
                os.makedirs(os.path.dirname(self.paths[i]), exist_ok=True)
                open(self.paths[i], "wb").close()

    def _span(self, offset, n):
        files = self.t.files
        i = max(bisect.bisect_right(self.starts, offset) - 1, 0)
        end = offset + n
        while offset < end and i < len(files):
            _, length, foff = files[i]
            fo = offset - foff
            if fo >= length:
                i += 1
                continue
            take = min(length - fo, end - offset)
            yield i, fo, take
            offset += take
            i += 1

    def _open(self, i, create):
        fh = self.handles.get(i)
        if fh:
            return fh
        p = self.paths[i]
        if not os.path.exists(p):
            if not create:
                return None
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").close()
        fh = open(p, "r+b")
        if create and os.path.getsize(p) < self.t.files[i][1]:
            fh.truncate(self.t.files[i][1])
        if len(self.handles) > 64:
            self._close_all()
        self.handles[i] = fh
        return fh

    def _close_all(self):
        for fh in self.handles.values():
            try:
                fh.close()
            except OSError:
                pass
        self.handles.clear()

    def read(self, offset, n):
        out = []
        with self.lock:
            for i, fo, take in self._span(offset, n):
                fh = self._open(i, False)
                if fh is None:
                    return None
                fh.seek(fo)
                chunk = fh.read(take)
                if len(chunk) != take:
                    return None
                out.append(chunk)
        data = b"".join(out)
        return data if len(data) == n else None

    def write(self, offset, data):
        pos = 0
        with self.lock:
            touched = []
            for i, fo, take in self._span(offset, len(data)):
                fh = self._open(i, True)
                fh.seek(fo)
                fh.write(data[pos:pos + take])
                pos += take
                touched.append(fh)
            for fh in touched:
                fh.flush()

    def close(self):
        with self.lock:
            self._close_all()


# --------------------------------------------------------------------------
# Peer connection
# --------------------------------------------------------------------------
class Partial:
    __slots__ = ("buf", "nblocks", "got", "req")

    def __init__(self, size):
        self.buf = bytearray(size)
        self.nblocks = (size + BLOCK - 1) // BLOCK
        self.got = set()
        self.req = {}  # block index -> {peer: time requested}


class Peer:
    def __init__(self, sess, reader, writer, addr, incoming):
        self.s = sess
        self.r = reader
        self.w = writer
        self.addr = addr
        self.incoming = incoming
        self.has = [False] * len(sess.have)
        self.choked = True
        self.am_choking = True
        self.interested = False
        self.peer_interested = False
        self.outstanding = set()
        self.rx_rate = 0.0               # smoothed bytes/s received from this peer
        self.rx_total = self.tx_total = 0
        self.rate_in = self.rate_out = 0.0     # smoothed bytes/s from / to this peer (updated every second by Session.tick)
        self._rx_seen = self._tx_seen = 0
        self.pid = b""
        self.since = time.time()
        self._rx_bytes, self._rx_t = 0, time.time()
        self.pid = None
        self.registered = False
        self.last_data = time.time()
        self.last_rx = time.time()      # last message of any kind (idle peers are dropped by Session.tick)
        self.ut_meta = None
        self.ut_pex = None
        self.listen_port = None
        self.pex_sent = set()
        self.next_pex = 0.0
        self.kind = getattr(writer, "kind", "tcp")
        self.encrypted = isinstance(writer, mse.CryptWriter) and writer.c is not None

    @property
    def listen_addr(self):
        """Address other peers can reach this peer on (None if unknown)."""
        if not self.incoming:
            return self.addr
        return (self.addr[0], self.listen_port) if self.listen_port else None

    def send_pex(self):
        s = self.s
        cur = {q.listen_addr for q in s.peers if q is not self and q.listen_addr}
        added = list(cur - self.pex_sent)[:50]
        dropped = list(self.pex_sent - cur)[:50]
        if not added and not dropped:
            return
        msg = {}
        for key, items in (("added", added), ("dropped", dropped)):
            for k, fam in ((key, False), (key + "6", True)):          # IPv4 in added/dropped, IPv6 in added6/dropped6
                part = [a for a in items if _is_v6(a[0]) == fam]
                if part or not fam:
                    msg[k] = b"".join(_pack_peer(a) for a in part)
                    if key == "added":
                        msg[k + ".f"] = bytes(len(part))
        self.pex_sent = (self.pex_sent | set(added)) - set(dropped)
        self.send(20, bytes([self.ut_pex]) + bencode(msg))

    # -- sending ----------------------------------------------------------
    def send(self, mid, payload=b""):
        if not self.w.is_closing():
            self.w.write(struct.pack(">IB", len(payload) + 1, mid) + payload)

    def keepalive(self):
        if not self.w.is_closing():
            self.w.write(b"\0\0\0\0")

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass

    def _handshake_bytes(self):
        return (PSTR + b"\0\0\0\0\0\x10\0" + (b"\x01" if self.s.mgr.dht else b"\0")
                + self.s.t.info_hash + self.s.mgr.peer_id)

    # -- lifecycle --------------------------------------------------------
    async def run(self, hs=None):
        s = self.s
        try:
            if self.incoming:
                self.w.write(self._handshake_bytes())
            else:
                self.w.write(self._handshake_bytes())
                hs = await asyncio.wait_for(self.r.readexactly(68), 10)
            if hs[:20] != PSTR or hs[28:48] != s.t.info_hash:
                return
            self.pid = hs[48:68]
            if self.pid == s.mgr.peer_id or self.pid in s.peer_ids or s.stopped:
                return
            s.peer_ids.add(self.pid)
            s.peers.add(self)
            self.registered = True
            s.stats[self.kind] = s.stats.get(self.kind, 0) + 1
            if self.encrypted:
                s.stats["encrypted"] += 1
            if hs[25] & 0x10:  # peer supports the extension protocol
                m = {"ut_metadata": 1}
                if not s.t.private:  # BEP 27: no peer exchange for private torrents
                    m["ut_pex"] = 2
                self.send(20, b"\0" + bencode({"m": m, "metadata_size": len(s.t.info_raw),
                                               "p": s.mgr.public_port}))
            if hs[27] & 1 and s.mgr.dht:  # peer speaks DHT: tell it our DHT port
                self.send(9, struct.pack(">H", s.mgr.public_udp_port))
            if any(s.have):
                bf = bytearray((len(s.have) + 7) // 8)
                for i, h in enumerate(s.have):
                    if h:
                        bf[i >> 3] |= 0x80 >> (i & 7)
                self.send(5, bytes(bf))
            while True:
                # no per-message timeout (wait_for was a large share of the CPU time): Session.tick drops silent peers
                hdr = await self.r.readexactly(4)
                self.last_rx = time.time()
                ln = struct.unpack(">I", hdr)[0]
                if ln == 0:
                    continue
                if ln > (1 << 20) + 16:
                    raise ValueError("message too large")
                data = await self.r.readexactly(ln)
                self.last_rx = time.time()
                await self.handle(data[0], data[1:])
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError,
                OSError, ValueError, struct.error, IndexError, AttributeError,
                KeyError, TypeError):
            pass
        finally:
            self.cleanup()

    def cleanup(self):
        s = self.s
        if self.registered:
            s.peers.discard(self)
            s.peer_ids.discard(self.pid)
            self.registered = False
            for i, h in enumerate(self.has):
                if h:
                    s.avail[i] -= 1
            self.has = [False] * len(self.has)
        s.release(self)
        self.close()

    # -- message handling -------------------------------------------------
    async def handle(self, mid, p):
        s = self.s
        n = len(s.have)
        if mid == 0:  # choke
            self.choked = True
            s.release(self)
        elif mid == 1:  # unchoke
            self.choked = False
            self.fill()
        elif mid == 2:
            self.peer_interested = True
            s.update_choking()
        elif mid == 3:
            self.peer_interested = False
            if not self.am_choking:
                self.am_choking = True
                self.send(0)
                s.update_choking()
        elif mid == 4:  # have
            idx = struct.unpack(">I", p)[0]
            if idx >= n:
                raise ValueError("bad have")
            if not self.has[idx]:
                self.has[idx] = True
                s.avail[idx] += 1
                if not s.have[idx] and s.want[idx] and not self.interested:
                    self.interested = True
                    self.send(2)
                self.fill()
        elif mid == 5:  # bitfield
            if len(p) != (n + 7) // 8:
                raise ValueError("bad bitfield")
            for i in range(n):
                bit = (p[i >> 3] >> (7 - (i & 7))) & 1
                if bit and not self.has[i]:
                    self.has[i] = True
                    s.avail[i] += 1
            if s.complete and all(self.has):
                raise ValueError("both seeds")
            want = any(h and s.want[i] and not s.have[i] for i, h in enumerate(self.has))
            if want and not self.interested:
                self.interested = True
                self.send(2)
            self.fill()
        elif mid == 9:  # peer's DHT port: learn a new DHT node
            port = struct.unpack(">H", p)[0]
            d = s.mgr.dht_for(self.addr[0])
            if port and d:
                asyncio.get_running_loop().create_task(d.ping((self.addr[0], port)))
        elif mid == 20:  # extension protocol: serve ut_metadata to magnet peers
            eid, body = p[0], p[1:]
            if eid == 0:
                d = bdecode(body)
                m = d.get(b"m", {})
                self.ut_meta = m.get(b"ut_metadata")
                if not s.t.private:
                    self.ut_pex = m.get(b"ut_pex")
                lp = d.get(b"p")
                if isinstance(lp, int) and 0 < lp < 65536:
                    self.listen_port = lp
            elif eid == 2 and not s.t.private:
                s.on_pex(bdecode(body))
            elif eid == 1 and self.ut_meta:
                d, _ = _dec(body, 0)
                pc = d.get(b"piece")
                if d.get(b"msg_type") == 0 and isinstance(pc, int):
                    raw = s.t.info_raw
                    if 0 <= pc and pc * BLOCK < len(raw):
                        hdr = bencode({"msg_type": 1, "piece": pc, "total_size": len(raw)})
                        self.send(20, bytes([self.ut_meta]) + hdr + raw[pc * BLOCK:(pc + 1) * BLOCK])
                    else:
                        self.send(20, bytes([self.ut_meta]) + bencode({"msg_type": 2, "piece": pc}))
        elif mid == 6:  # request
            idx, begin, ln = struct.unpack(">III", p)
            if (self.am_choking or ln > 32768 or idx >= n or not s.have[idx]
                    or begin + ln > s.t.piece_size(idx)):
                return
            await s.mgr.up.acquire(ln)  # upload speed limit
            await s.up_lim.acquire(ln)
            data = await s.read_block(idx, begin, ln)
            if data is None or self.s.stopped:
                return
            self.send(7, struct.pack(">II", idx, begin) + data)
            s.uploaded += ln
            self.tx_total += ln
            await self.w.drain()
        elif mid == 7:  # piece
            if len(p) < 9:
                raise ValueError("short piece")
            idx, begin = struct.unpack(">II", p[:8])
            data = p[8:]
            await s.mgr.down.acquire(len(data))  # download speed limit
            await s.down_lim.acquire(len(data))
            key = (idx, begin)
            if key not in self.outstanding:
                return
            self.outstanding.discard(key)
            self.last_data = time.time()
            part = s.partial.get(idx)
            if part is None:
                self.fill()
                return
            b = begin // BLOCK
            r = part.req.get(b)
            if r:
                r.pop(self, None)
            if b in part.got:
                self.fill()
                return
            expected = min(BLOCK, len(part.buf) - begin)
            if begin % BLOCK or len(data) != expected:
                raise ValueError("bad block")
            part.buf[begin:begin + expected] = data
            part.got.add(b)
            s.downloaded += expected
            self._rx_bytes += expected
            self.rx_total += expected
            if len(part.got) == part.nblocks:
                s.piece_done(idx)
            self.fill()

    def pipeline_depth(self):
        """How many block requests to keep in flight: enough to cover ~0.6 s of this peer's speed, so a fast peer on a
        high-latency link is never left waiting for the next batch (10 requests = 160 KB would cap it at 160 KB per round trip)."""
        now = time.time()
        if now - self._rx_t >= 1.0:
            self.rx_rate = 0.5 * self.rx_rate + 0.5 * self._rx_bytes / (now - self._rx_t)
            self._rx_bytes, self._rx_t = 0, now
        return int(min(MAX_PIPELINE, max(10, self.rx_rate * 0.6 / BLOCK + 6)))

    def fill(self):
        s = self.s
        if self.choked or not self.interested or s.done or s.stopped:
            return
        depth = self.pipeline_depth()
        while len(self.outstanding) < depth:
            nb = s.next_block(self)
            if not nb:
                break
            pi, b = nb
            part = s.partial[pi]
            part.req.setdefault(b, {})[self] = time.time()
            begin = b * BLOCK
            ln = min(BLOCK, s.t.piece_size(pi) - begin)
            self.outstanding.add((pi, begin))
            self.send(6, struct.pack(">III", pi, begin, ln))


# --------------------------------------------------------------------------
# Session (one torrent)
# --------------------------------------------------------------------------
class Session:
    dht_announce = True

    def __init__(self, mgr, torrent, save_dir):
        self.mgr = mgr
        self.t = torrent
        self.save_dir = save_dir
        self.storage = Storage(torrent, save_dir)
        n = len(torrent.hashes)
        self.have = [False] * n
        self.avail = [0] * n
        self.partial = {}
        self.writing = set()           # pieces received whole and being verified/written on a worker thread
        self.write_tasks = set()
        self.tracker_status = {}       # url -> (text, time of the last announce)
        self.peers = set()
        self.peer_ids = set()
        self.candidates = set()
        self.addrs = set()
        self.tried = {}
        self.conn_tasks = set()
        self.tasks = []
        self.downloaded = 0
        self.uploaded = 0
        self.left = torrent.length
        self.skip = set()              # indices of files the user does not want
        self.high = set()              # indices of files to fetch first
        self.hot = [False] * n         # per piece: overlaps a high-priority wanted file
        self.sequential = False        # fetch pieces in file order (for watching while it downloads)
        self._todo_list = None         # pieces that could be started (rebuilt lazily; see _todo)
        self._seq_ptr = 0              # in-order mode: nothing below this piece can be picked
        self.read_cache = collections.OrderedDict()   # (piece, chunk) -> bytes, for uploads
        self.stream_file = None        # file being played while it downloads (see stream_touch)
        self.stream_rate = 0.0         # bytes/s of the video, once the player reported its length
        self.stream_pos = 0            # piece the player is reading at
        self.stream_at = 0.0           # time of the last read by the player
        self._opt = None               # the peer holding the optimistic unchoke slot
        self._opt_since = 0.0
        self.last_space_check = 0.0
        self.want = [True] * n         # per piece: overlaps at least one wanted file
        self.want_left = torrent.length
        self.want_total = torrent.length
        self.user_paused = False
        self.resumed = False           # the last start skipped the hash check thanks to resume data
        self.resume = None             # saved verified-piece state (see resume_data) offered by the app at start-up
        self.order = 0                 # position in the download queue
        self.priority = 0              # -1 low, 0 normal, 1 high: higher priority starts first in the queue
        self.category = ""
        self.down_lim, self.up_lim = RateLimiter(), RateLimiter()   # this torrent's own speed limits (0 = none)
        self.up_base = 0               # bytes uploaded in earlier runs (for the share ratio)
        self.seed_secs = 0.0           # time spent seeding, including earlier runs
        self.rule_ignore = False       # the user resumed it after a seeding rule stopped it: keep seeding
        self.rule_hit = False
        self.down_rate = 0.0
        self.up_rate = 0.0
        self.state = "Queued"
        self.error = None
        self.checked = False
        self.check_done = 0
        self.stop_flag = False
        self.stopped = True
        self.pending_event = ""
        self.announce_now = None
        self.stats = {"tcp": 0, "utp": 0, "encrypted": 0, "pex": 0}

    @property
    def complete(self):
        return self.left == 0

    @property
    def share_ratio(self):
        """Uploaded / torrent size over the torrent's whole life."""
        return (self.up_base + self.uploaded) / self.t.length if self.t.length else 0.0

    def set_limits(self, down_kib, up_kib):
        self.down_lim.rate = max(0, int(down_kib)) * 1024
        self.up_lim.rate = max(0, int(up_kib)) * 1024

    async def relocate(self, new_dir):
        """Move the downloaded files to new_dir (keeping the torrent's own folder layout) and carry on."""
        was_running = not self.stopped
        await self.stop()
        loop = asyncio.get_running_loop()
        old_paths = list(self.storage.paths)
        t = self.t

        def move():
            for (parts, _ln, _off), src in zip(t.files, old_paths):
                dst = os.path.join(new_dir, *parts)
                if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(src, dst)
            if t.multi:     # tidy the now-empty old folders
                root = os.path.join(self.save_dir, t.name)
                for dp, _d, _f in sorted(os.walk(root), key=lambda x: -len(x[0])):
                    try:
                        os.rmdir(dp)
                    except OSError:
                        pass
        try:
            await loop.run_in_executor(None, move)
            self.save_dir = new_dir
            self.storage = Storage(self.t, new_dir)
        except OSError as e:
            self.error = "could not move files: %s" % e
        finally:
            if was_running and not self.user_paused:
                await self.start()

    @property
    def done(self):
        """Everything the user selected has been downloaded (the rest of the torrent may be missing)."""
        return self.want_left == 0

    @property
    def progress(self):
        return 1.0 - self.want_left / self.want_total if self.want_total else 1.0

    @property
    def selected_size(self):
        return sum(f[1] for i, f in enumerate(self.t.files) if i not in self.skip)

    def compute_want(self):
        """Rebuild the wanted-piece mask from `skip` and recount what is still missing."""
        t = self.t
        want = [False] * len(t.hashes)
        hot = [False] * len(t.hashes)
        for i, (_p, ln, off) in enumerate(t.files):
            if i in self.skip or ln <= 0:
                continue
            for pi in range(off // t.piece_length, (off + ln - 1) // t.piece_length + 1):
                want[pi] = True
                if i in self.high:
                    hot[pi] = True
        self.want, self.hot = want, hot
        self.invalidate_todo()
        self.want_total = sum(t.piece_size(i) for i, w in enumerate(want) if w)
        self.want_left = sum(t.piece_size(i) for i, w in enumerate(want) if w and not self.have[i])

    def file_progress(self):
        """Fraction downloaded for each file (by the verified pieces that cover it)."""
        t, out = self.t, []
        for _p, ln, off in t.files:
            if ln <= 0:
                out.append(1.0)
                continue
            got = 0
            for pi in range(off // t.piece_length, (off + ln - 1) // t.piece_length + 1):
                if self.have[pi]:
                    a, b = max(off, pi * t.piece_length), min(off + ln, pi * t.piece_length + t.piece_size(pi))
                    got += b - a
            out.append(got / ln)
        return out

    def set_skip(self, skip):
        """Choose the files to skip (loop thread). At least one file must stay selected."""
        skip = {int(i) for i in skip if 0 <= int(i) < len(self.t.files)}
        if len(skip) >= len(self.t.files):
            raise ValueError("select at least one file")
        was_done = self.done
        self.skip = skip
        self.compute_want()
        for pi in [pi for pi in self.partial if not self.want[pi]]:
            self.partial.pop(pi)       # blocks still arriving for it are ignored
        self.invalidate_todo()
        for p in list(self.peers):
            p.outstanding = {o for o in p.outstanding if self.want[o[0]]}
            wants = any(h and self.want[i] and not self.have[i] for i, h in enumerate(p.has))
            if wants and not p.interested:
                p.interested = True
                p.send(2)
            elif not wants and p.interested:
                p.interested = False
                p.send(3)
        if not self.stopped:
            self._update_state()
            for p in list(self.peers):
                p.fill()

    def set_file_prio(self, indices, level):
        """level: 'skip' | 'normal' | 'high' for the given file indices (loop thread)."""
        idx = {int(i) for i in indices if 0 <= int(i) < len(self.t.files)}
        skip, high = set(self.skip), set(self.high)
        if level == "skip":
            skip |= idx
            high -= idx
        elif level == "high":
            skip -= idx
            high |= idx
        else:
            skip -= idx
            high -= idx
        if len(skip) >= len(self.t.files):
            raise ValueError("select at least one file")
        self.high = high
        self.set_skip(skip)

    def free_space(self):
        try:
            return shutil.disk_usage(self.save_dir if os.path.isdir(self.save_dir) else os.path.dirname(self.save_dir) or ".").free
        except OSError:
            return None

    def space_needed(self):
        """Bytes still to be written for the wanted files."""
        return self.want_left

    # -- resume data ------------------------------------------------------
    def _file_stats(self):
        out = []
        for p in self.storage.paths:
            try:
                st = os.stat(p)
                out.append([st.st_size, st.st_mtime_ns])
            except OSError:
                out.append(None)
        return out

    def resume_data(self):
        """What is needed to skip the full hash check next time: which pieces are verified, plus every file's size
        and modification time. Only meaningful while no writes are pending (i.e. after the session stopped)."""
        if not self.checked:
            return None
        bits = bytearray((len(self.have) + 7) // 8)
        for i, h in enumerate(self.have):
            if h:
                bits[i >> 3] |= 0x80 >> (i & 7)
        return {"v": 1, "pieces": base64.b64encode(bytes(bits)).decode("ascii"), "files": self._file_stats()}

    def _from_resume(self):
        """The saved piece map if the files on disk are exactly as we left them, else None (then do a full check)."""
        r, n = self.resume, len(self.t.hashes)
        self.resume = None                      # only ever used once
        try:
            if not r or r.get("v") != 1 or r["files"] != self._file_stats():
                return None
            bits = base64.b64decode(r["pieces"])
            if len(bits) != (n + 7) // 8:
                return None
            return [bool(bits[i >> 3] & (0x80 >> (i & 7))) for i in range(n)]
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    # -- lifecycle --------------------------------------------------------
    def _check(self):
        t = self.t
        have = [False] * len(t.hashes)
        for i in range(len(have)):
            if self.stop_flag:
                return None
            data = self.storage.read(i * t.piece_length, t.piece_size(i))
            have[i] = data is not None and hashlib.sha1(data).digest() == t.hashes[i]
            self.check_done = i + 1
        return have

    async def start(self):
        if not self.stopped:
            return
        loop = asyncio.get_running_loop()
        self.stopped = False
        self.stop_flag = False
        self.error = None
        self.tried.clear()      # a restarted torrent may reconnect to the peers it just had, without the 60 s back-off
        self.announce_now = asyncio.Event()
        try:
            if not self.checked:
                self.state = "Checking"
                self.check_done = 0
                have = await loop.run_in_executor(None, self._from_resume)
                if have is not None:
                    self.resumed = True
                else:
                    self.resumed = False
                    have = await loop.run_in_executor(None, self._check)
                if have is None or self.stopped:
                    return
                self.have = have
                self.left = sum(self.t.piece_size(i) for i, h in enumerate(have) if not h)
                self.compute_want()
                self.checked = True
            self.storage.prepare()
        except Exception as e:  # disk errors etc.
            self.state = "Error"
            self.error = str(e)
            self.stopped = True
            return
        self._update_state()
        coros = [self.tracker_loop(), self.connect_loop(), self.tick()]
        if self.mgr.dht and not self.t.private:  # private torrents must not use DHT
            coros.append(self.dht_loop())
        self.tasks = [loop.create_task(c) for c in coros]

    async def stop(self, announce=False, state="Paused"):
        """`state` is shown at once: a torrent moved back to the queue must not read "Paused" during the announce."""
        self.state = state
        self.stopped = True
        self.stop_flag = True
        for tk in self.tasks + list(self.conn_tasks):
            tk.cancel()
        self.tasks = []
        for p in list(self.peers):
            p.close()
        self.down_rate = self.up_rate = 0.0
        self.read_cache.clear()
        if self.write_tasks:       # let pieces already received reach the disk before the files are closed
            await asyncio.gather(*list(self.write_tasks), return_exceptions=True)
        self.storage.close()
        if announce and self.t.trackers:
            loop = asyncio.get_running_loop()
            futs = [_in_daemon(loop, self._announce_blocking, u, "stopped") for u in self.t.trackers]
            try:
                await asyncio.wait_for(asyncio.gather(*futs, return_exceptions=True), 4)
            except asyncio.TimeoutError:
                pass

    def _update_state(self):
        self.state = "Seeding" if self.complete else "Finished" if self.done else "Downloading"

    # -- piece management -------------------------------------------------
    def release(self, peer):
        for pi, begin in peer.outstanding:
            part = self.partial.get(pi)
            if part:
                r = part.req.get(begin // BLOCK)
                if r:
                    r.pop(peer, None)
        peer.outstanding.clear()

    # -- choosing what to ask for -----------------------------------------
    def invalidate_todo(self):
        """Something made pieces startable again (or changed which ones are wanted): rebuild the candidate list."""
        self._todo_list = None
        self._seq_ptr = 0

    def _todo(self):
        """Pieces that could be started (wanted, missing, not in flight). Built lazily and allowed to go stale: entries
        are re-checked when used, so only things that *add* candidates (invalidate_todo) need to reset it."""
        lst = self._todo_list
        if lst is None:
            have, part, wr, want = self.have, self.partial, self.writing, self.want
            lst = self._todo_list = [i for i in range(len(have)) if want[i] and not have[i] and i not in part and i not in wr]
        return lst

    def _pick_piece(self, peer):
        """Index of the next piece to start for this peer, or None. Rarest-first (exact for up to 2048 candidates, a
        random window of them beyond that, so a huge torrent does not rescan every piece per request); lowest index
        first in in-order mode; pieces of high-priority files before all others."""
        have, part, wr, want, has = self.have, self.partial, self.writing, self.want, peer.has
        hot = self.hot if self.high else None
        if self.sequential:
            n = len(have)
            i = self._seq_ptr
            while i < n and (have[i] or not want[i] or i in part or i in wr):
                i += 1
            self._seq_ptr = i
            for only_hot in ((True, False) if hot else (False,)):
                for j in range(i, n):
                    if has[j] and not have[j] and want[j] and j not in part and j not in wr and (not only_hot or hot[j]):
                        return j
            return None
        lst = self._todo()
        n = len(lst)
        if not n:
            return None
        window = n if (n <= 2048 or hot) else 384
        start = random.randrange(n)           # ties go to whichever comes first after a random start
        avail = self.avail
        best, bestv, seen, stale = None, None, 0, 0
        for off in range(n):
            i = lst[(start + off) % n]
            if have[i] or i in part or i in wr or not want[i]:
                stale += 1
                continue
            seen += 1
            if has[i]:
                v = avail[i] + (0 if hot is None or hot[i] else 1 << 30)
                if bestv is None or v < bestv:
                    best, bestv = i, v
            if seen >= window and best is not None:
                break
        if stale > 64 and stale * 2 > n:
            self._todo_list = None            # mostly stale: compact on the next call
        return best

    # -- streaming (playing a file while it downloads) ---------------------
    def stream_touch(self, file_idx, byte_pos):
        """The player is reading file `file_idx` at `byte_pos`: fetch from there first (called from any thread)."""
        _p, ln, off = self.t.files[file_idx]
        self.stream_file = file_idx
        self.stream_pos = (off + max(0, min(byte_pos, max(ln - 1, 0)))) // self.t.piece_length
        self.stream_at = time.time()

    def stream_ready(self, file_idx, start, n):
        """True when every piece covering bytes [start, start+n) of the file has been verified (or is on disk)."""
        _p, ln, off = self.t.files[file_idx]
        if n <= 0 or ln <= 0:
            return True
        pl, have = self.t.piece_length, self.have
        return all(have[i] for i in range((off + start) // pl, (off + min(start + n, ln) - 1) // pl + 1))

    def _stream_ranges(self):
        """Inclusive piece ranges to serve first while streaming, most urgent first; [] when not streaming."""
        if self.stream_file is None or time.time() - self.stream_at > STREAM_IDLE:
            return []
        _p, ln, off = self.t.files[self.stream_file]
        pl = self.t.piece_length
        first, last = off // pl, (off + max(ln, 1) - 1) // pl
        pos = min(max(self.stream_pos, first), last)
        now_hi = min(last, pos + max(1, STREAM_NOW // pl))
        ahead = STREAM_AHEAD
        if self.stream_rate:                         # the player told us the bitrate: look ahead by time, not a fixed size
            ahead = int(min(STREAM_AHEAD_MAX, max(STREAM_AHEAD // 2, self.stream_rate * STREAM_SECONDS)))
        ahead_hi = min(last, pos + max(2, ahead // pl))
        tail_lo = max(first, last - max(1, STREAM_TAIL // pl) + 1)
        return [(pos, now_hi), (tail_lo, last), (pos, ahead_hi)]

    def _pick_in_ranges(self, peer, ranges):
        have, part, wr, want, has = self.have, self.partial, self.writing, self.want, peer.has
        for lo, hi in ranges:
            for i in range(lo, hi + 1):
                if has[i] and want[i] and not have[i] and i not in part and i not in wr:
                    return i
        return None

    def _partial_block(self, peer, now, ranges=None):
        for pi, part in self.partial.items():
            if not peer.has[pi] or not self.want[pi]:
                continue
            if ranges is not None and not any(lo <= pi <= hi for lo, hi in ranges):
                continue
            for b in range(part.nblocks):
                if b in part.got:
                    continue
                r = part.req.get(b)
                if r and (peer in r or any(now - t < 30 for t in r.values())):
                    continue
                return pi, b
        return None

    def next_block(self, peer):
        now = time.time()
        ranges = self._stream_ranges()
        if ranges:                        # playing while downloading: what the player needs next comes first
            r = self._partial_block(peer, now, ranges)
            if r:
                return r
            pi = self._pick_in_ranges(peer, ranges)
            if pi is not None:
                self.partial[pi] = Partial(self.t.piece_size(pi))
                return pi, 0
        r = self._partial_block(peer, now)
        if r:
            return r
        pi = self._pick_piece(peer)
        if pi is not None:
            self.partial[pi] = Partial(self.t.piece_size(pi))
            return pi, 0
        for pi, part in self.partial.items():  # end-game: duplicate requests
            if peer.has[pi] and self.want[pi]:
                for b in range(part.nblocks):
                    if b not in part.got and peer not in part.req.get(b, {}):
                        return pi, b
        return None

    async def read_block(self, idx, begin, ln):
        """Data for an upload. The disk is read READ_CHUNK at a time (one worker-thread hop per chunk instead of per
        16 KiB block) and the last few chunks are kept, since peers ask for a piece's blocks one after another."""
        loop = asyncio.get_running_loop()
        c = begin // READ_CHUNK
        if (begin + ln - 1) // READ_CHUNK != c:          # an odd request straddling two chunks: read it directly
            return await loop.run_in_executor(None, self.storage.read, idx * self.t.piece_length + begin, ln)
        key = (idx, c)
        data = self.read_cache.get(key)
        if data is None:
            size = min(READ_CHUNK, self.t.piece_size(idx) - c * READ_CHUNK)
            data = await loop.run_in_executor(None, self.storage.read, idx * self.t.piece_length + c * READ_CHUNK, size)
            if data is None:
                return None
            if self.stopped:
                return None
            self.read_cache[key] = data
            while len(self.read_cache) > READ_CACHE_CHUNKS:
                self.read_cache.popitem(last=False)
        else:
            self.read_cache.move_to_end(key)
        o = begin - c * READ_CHUNK
        return data[o:o + ln]

    def piece_done(self, idx):
        """A piece arrived whole: hand hashing and the disk write to a worker thread so the network loop never waits."""
        part = self.partial.pop(idx)
        self.writing.add(idx)
        task = asyncio.get_running_loop().create_task(self._finish_piece(idx, bytes(part.buf)))
        self.write_tasks.add(task)
        task.add_done_callback(self.write_tasks.discard)

    def _verify_write(self, idx, data):
        if hashlib.sha1(data).digest() != self.t.hashes[idx]:
            return False                   # corrupt piece: discard and re-download
        self.storage.write(idx * self.t.piece_length, data)
        return True

    async def _finish_piece(self, idx, data):
        try:
            ok = await asyncio.get_running_loop().run_in_executor(None, self._verify_write, idx, data)
        except OSError as e:
            self.writing.discard(idx)
            self.error = str(e)
            self.state = "Error"
            asyncio.get_running_loop().create_task(self.stop())
            return
        self.writing.discard(idx)
        if not ok:
            self.invalidate_todo()
            for p in list(self.peers):
                p.fill()
            return
        self.have[idx] = True
        self.left -= len(data)
        was_done = self.done
        if self.want[idx]:
            self.want_left -= len(data)
        for p in list(self.peers):
            p.send(4, struct.pack(">I", idx))
        if self.complete:
            self.pending_event = "completed"
            if self.announce_now:
                self.announce_now.set()
        if self.done:
            self._update_state()
            for p in list(self.peers):
                if p.interested:
                    p.interested = False
                    p.send(3)
            if not was_done and self.mgr.on_finished:   # a download just finished (not merely re-checked)
                try:
                    self.mgr.on_finished(self.t.hex, self.t.name)
                except Exception:
                    pass
            self.mgr._kick()                            # a queue slot is free

    def slots(self):
        """Upload slots: UNCHOKE_SLOTS (+1 optimistic), fewer when the upload limit is so low that each would starve."""
        rate = self.mgr.up.rate or self.up_lim.rate
        n = UNCHOKE_SLOTS if not rate else max(1, min(UNCHOKE_SLOTS, int(rate // (8 * 1024))))
        return n + 1

    def update_choking(self):
        """Quick path (a peer just became interested, one left): hand free slots to interested peers."""
        unchoked = sum(1 for p in self.peers if not p.am_choking)
        limit = self.slots()
        for p in list(self.peers):
            if unchoked >= limit:
                break
            if p.peer_interested and p.am_choking:
                p.am_choking = False
                p.send(1)
                unchoked += 1

    def rechoke(self, now=None):
        """Every 10 s: upload to the interested peers that serve us best (while downloading: those that send us the
        most; while seeding: those that take the most), plus one rotating optimistic slot so newcomers get a chance."""
        now = now or time.time()
        rate = (lambda p: p.rate_out) if self.complete else (lambda p: p.rate_in)
        interested = [p for p in self.peers if p.peer_interested]
        regular = self.slots() - 1
        keep = set(sorted(interested, key=rate, reverse=True)[:regular])
        rest = [p for p in interested if p not in keep]
        if self._opt not in rest or now - self._opt_since >= OPT_ROTATE:
            self._opt_since = now
            self._opt = random.choices(rest, [3 if now - p.since < 60 else 1 for p in rest])[0] if rest else None
        if self._opt is not None:
            keep.add(self._opt)
        for p in list(self.peers):
            if p in keep and p.am_choking:
                p.am_choking = False
                p.send(1)
            elif p not in keep and not p.am_choking:
                p.am_choking = True
                p.send(0)

    def update_rates(self, dt=1.0):
        """Per-peer bytes/s over the last `dt` seconds (smoothed)."""
        for p in list(self.peers):
            p.rate_in = 0.6 * p.rate_in + 0.4 * (p.rx_total - p._rx_seen) / dt
            p.rate_out = 0.6 * p.rate_out + 0.4 * (p.tx_total - p._tx_seen) / dt
            p._rx_seen, p._tx_seen = p.rx_total, p.tx_total

    def churn(self, now=None):
        """While downloading: drop peers that unchoked us, owe us data and have sent nothing for 30 s (snubbed), and
        - when the peer list is nearly full and other candidates are waiting - the slowest useless connections."""
        now = now or time.time()
        for p in list(self.peers):
            if not p.choked and p.outstanding and now - p.last_data > 30:
                p.close()
        if self.candidates and len(self.peers) >= MAX_PEERS - 3:
            useless = sorted((p for p in self.peers if now - p.since > 90 and p.rate_in < 1024 and p.rate_out < 1024),
                             key=lambda p: p.rate_in)
            for p in useless[:2]:
                p.close()

    # -- background tasks -------------------------------------------------
    async def tick(self):
        last_d, last_u, n = self.downloaded, self.uploaded, 0
        last_t = time.monotonic()
        while True:
            await asyncio.sleep(1)
            n += 1
            # bytes per second of REAL time: a busy loop wakes this late (1.1 - 1.5 s while downloading fast), and
            # counting that as one second overstated the speed by about a quarter
            now_t = time.monotonic()
            dt, last_t = max(now_t - last_t, 0.05), now_t
            self.down_rate = 0.6 * self.down_rate + 0.4 * (self.downloaded - last_d) / dt
            self.up_rate = 0.6 * self.up_rate + 0.4 * (self.uploaded - last_u) / dt
            last_d, last_u = self.downloaded, self.uploaded
            self.update_rates(dt)
            now = time.time()
            for p in list(self.peers):  # peer exchange (BEP 11)
                if p.ut_pex and now >= p.next_pex:
                    p.next_pex = now + self.mgr.pex_interval
                    p.send_pex()
            if n % 10 == 0:
                self.rechoke(now)
                if not self.complete:
                    self.churn(now)
            if n % 5 == 0:
                self.update_choking()
                now = time.time()
                for p in list(self.peers):
                    if p.outstanding and now - p.last_data > 60 or now - p.last_rx > IDLE_CLOSE:
                        p.close()
                    else:
                        p.fill()
            if n % 5 == 0 and not self.complete:
                free = self.free_space()
                if free is not None and free < LOW_SPACE:
                    self.error = "Disk full: only %s free" % _size_text(free)
                    self.state = "Error"
                    asyncio.get_running_loop().create_task(self.stop())
                    return
            if n % 90 == 0:
                for p in list(self.peers):
                    p.keepalive()

    async def connect_loop(self):
        while True:
            now = time.time()
            for addr in list(self.candidates):
                if len(self.conn_tasks) >= MAX_OUT or len(self.peers) >= MAX_PEERS:
                    break
                self.candidates.discard(addr)
                if addr in self.addrs or now - self.tried.get(addr, 0) < 60:
                    continue
                t = asyncio.get_running_loop().create_task(self.connect(addr))
                self.conn_tasks.add(t)
                t.add_done_callback(self.conn_tasks.discard)
            await asyncio.sleep(1)

    async def connect(self, addr):
        if addr[0] in self.mgr.banned:
            return
        self.addrs.add(addr)
        self.tried[addr] = time.time()
        try:
            r, w = await self.mgr.dial(addr, self.t.info_hash)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
            self.addrs.discard(addr)
            return
        try:
            await Peer(self, r, w, addr, False).run()
        finally:
            self.addrs.discard(addr)

    # -- trackers ---------------------------------------------------------
    async def tracker_loop(self):
        loop = asyncio.get_running_loop()
        event = "started"
        while True:
            if self.pending_event:
                event, self.pending_event = self.pending_event, ""
            interval = 1800
            if self.t.trackers:
                futs = [_in_daemon(loop, self._announce_blocking, u, event) for u in self.t.trackers]
                results = await asyncio.gather(*futs, return_exceptions=True)
                got = False
                now = time.time()
                for url, res in zip(self.t.trackers, results):
                    if isinstance(res, tuple):
                        self.tracker_status[url] = ("Working - %d peer%s" % (len(res[1]), "" if len(res[1]) == 1 else "s"), now)
                    else:
                        self.tracker_status[url] = ((str(res) or type(res).__name__)[:80], now)
                for res in results:
                    if isinstance(res, tuple):
                        got = True
                        interval = min(interval, max(res[0], 60))
                        for a in res[1]:
                            if a not in self.addrs:
                                self.candidates.add(a)
                if not got:
                    interval = 60
            event = ""
            wait = min(interval, 120) if len(self.peers) < 5 and not self.complete else interval
            try:
                await asyncio.wait_for(self.announce_now.wait(), wait)
            except asyncio.TimeoutError:
                pass
            self.announce_now.clear()

    def _usable(self, a):
        return bool(a[1]) and self.mgr.family_ok(a[0])

    def on_pex(self, d):
        """Peers learned through ut_pex (BEP 11)."""
        found = []
        added = d.get(b"added", b"")
        if isinstance(added, bytes):
            found += _compact(added)
        added6 = d.get(b"added6", b"")                     # IPv6 peers (BEP 11)
        if isinstance(added6, bytes):
            found += _compact6(added6)
        for a in found[:100]:
            if a not in self.addrs and a not in self.candidates and len(self.candidates) < 500 \
                    and self._usable(a):
                self.candidates.add(a)
                self.stats["pex"] += 1

    async def dht_loop(self):
        """Find peers through the DHT (and announce ourselves if we have the full torrent info)."""
        delay = 5
        while True:
            results = await asyncio.gather(
                *(d.get_peers(self.t.info_hash, (self.mgr.port if d.family == socket.AF_INET6 else self.mgr.public_port)
                              if self.dht_announce else 0)                   # (no NAT on IPv6: our own port)
                  for d in self.mgr.dhts), return_exceptions=True)
            peers = set()
            for res in results:
                if isinstance(res, set):
                    peers |= res
            for a in peers:
                if a not in self.addrs and self._usable(a):
                    self.candidates.add(a)
            if peers or self.peers:
                delay = 900
            else:
                delay = min(delay * 2, 120)  # back off while nobody is found
            await asyncio.sleep(delay)

    def _announce_blocking(self, url, event):
        if url.startswith("udp://"):
            return self._udp_announce(url, event)
        return self._http_announce(url, event)

    def _http_announce(self, url, event):
        q = {
            "port": self.mgr.public_port, "uploaded": self.uploaded, "downloaded": self.downloaded,
            "left": self.left, "compact": 1, "numwant": 50,
        }
        qs = urllib.parse.urlencode(q)
        qs += "&info_hash=" + urllib.parse.quote_from_bytes(self.t.info_hash, safe="")
        qs += "&peer_id=" + urllib.parse.quote_from_bytes(self.mgr.peer_id, safe="")
        if event:
            qs += "&event=" + event
        full = url + ("&" if "?" in url else "?") + qs
        req = urllib.request.Request(full, headers={"User-Agent": "BitTorrentClient/1.0"})
        with urllib.request.urlopen(req, timeout=4 if event == "stopped" else 15) as resp:
            d = bdecode(resp.read())
        if b"failure reason" in d:
            raise RuntimeError(d[b"failure reason"].decode("utf-8", "replace"))
        peers = d.get(b"peers", b"")
        out = []
        if isinstance(peers, bytes):
            out += _compact(peers)
        peers6 = d.get(b"peers6", b"")                      # BEP 7: compact IPv6 peers
        if isinstance(peers6, bytes):
            out += _compact6(peers6)
        else:
            for p in peers:
                try:
                    out.append((p[b"ip"].decode(), int(p[b"port"])))
                except (KeyError, ValueError):
                    pass
        return int(d.get(b"interval", 1800)), out

    def _udp_announce(self, url, event):
        """Announce over IPv4 and, if we have it and the tracker does, over IPv6 too (each family's tracker answer
        lists that family's peers)."""
        u = urllib.parse.urlparse(url)
        infos = socket.getaddrinfo(u.hostname, u.port or 80, type=socket.SOCK_DGRAM)
        fams = [socket.AF_INET] + ([socket.AF_INET6] if self.mgr.ipv6 else [])
        picks = [next((i for i in infos if i[0] == f), None) for f in fams]
        picks = [p for p in picks if p]
        if not picks:
            raise RuntimeError("tracker has no usable address")
        interval, peers, errors = None, [], []
        for family, _, _, _, addr in picks:
            try:
                iv, got = self._udp_announce_to(family, addr[:2], event)
                interval = iv if interval is None else min(interval, iv)
                peers += got
            except (OSError, RuntimeError, struct.error) as e:
                errors.append(e)
        if interval is None:
            raise errors[0]
        return interval, peers

    def _udp_announce_to(self, family, addr, event):
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.settimeout(2 if event == "stopped" else 8)   # "stopped" is a courtesy: never hold up quitting
        try:
            tid = random.getrandbits(32)
            sock.sendto(struct.pack(">QII", 0x41727101980, 0, tid), addr)
            data, _ = sock.recvfrom(2048)
            act, rtid, cid = struct.unpack(">IIQ", data[:16])
            if act != 0 or rtid != tid:
                raise RuntimeError("bad connect response")
            tid = random.getrandbits(32)
            ev = {"": 0, "completed": 1, "started": 2, "stopped": 3}[event]
            pkt = struct.pack(">QII20s20sQQQIIIiH", cid, 1, tid, self.t.info_hash,
                              self.mgr.peer_id, self.downloaded, self.left, self.uploaded,
                              ev, 0, random.getrandbits(32), -1, self.mgr.public_port)
            sock.sendto(pkt, addr)
            data, _ = sock.recvfrom(8192)
            act, rtid, interval = struct.unpack(">III", data[:12])
            if act != 1 or rtid != tid:
                raise RuntimeError("bad announce response")
            # BEP 15: 6-byte peer entries over IPv4, 18-byte ones over IPv6
            return interval, (_compact6 if family == socket.AF_INET6 else _compact)(data[20:])
        finally:
            sock.close()


# --------------------------------------------------------------------------
# Magnet links (BEP 9 / BEP 10): fetch the info dictionary from peers
# --------------------------------------------------------------------------
def parse_magnet(uri):
    u = urllib.parse.urlparse(uri.strip())
    if u.scheme.lower() != "magnet":
        raise ValueError("not a magnet link")
    q = urllib.parse.parse_qs(u.query)
    ih = None
    for xt in q.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            v = xt[9:]
            try:
                if len(v) == 40:
                    ih = bytes.fromhex(v)
                elif len(v) == 32:
                    ih = base64.b32decode(v.upper())
            except ValueError:
                pass
    if not ih or len(ih) != 20:
        raise ValueError("magnet link has no valid info hash")
    trackers = [t for t in q.get("tr", []) if t.startswith(("http://", "https://", "udp://"))]
    peers = []
    for pe in q.get("x.pe", []):
        host, _, port = pe.rpartition(":")
        try:
            peers.append((host, int(port)))
        except ValueError:
            pass
    return ih, q.get("dn", [""])[0], trackers, peers


class MagnetSession:
    """Stand-in for a Session until the torrent's metadata has been downloaded."""
    tracker_loop = Session.tracker_loop
    connect_loop = Session.connect_loop
    _announce_blocking = Session._announce_blocking
    _http_announce = Session._http_announce
    _udp_announce = Session._udp_announce
    dht_loop = Session.dht_loop
    _usable = Session._usable
    dht_announce = False

    def __init__(self, mgr, uri, save_dir):
        ih, dn, trackers, peers = parse_magnet(uri)
        self.mgr, self.uri, self.save_dir = mgr, uri, save_dir
        self.t = types.SimpleNamespace(info_hash=ih, trackers=trackers, private=False)
        self.hex = ih.hex()
        self.name = _safe(dn) if dn else "magnet:" + self.hex[:12]
        self.peers, self.addrs, self.tried = set(), set(), {}
        self.candidates = set(peers)
        self.conn_tasks, self.tasks = set(), []
        self.uploaded = self.downloaded = 0
        self.left = 1
        self.complete = False
        self.state = "Fetching metadata"
        self.error = None
        self.stopped = True
        self.pending_event = ""
        self.announce_now = None
        self.size = None
        self.pieces, self.pending = {}, {}
        self.finished = False

    @property
    def npieces(self):
        return (self.size + BLOCK - 1) // BLOCK if self.size else 0

    @property
    def progress(self):
        return len(self.pieces) / self.npieces if self.npieces else 0.0

    async def start(self):
        if not self.stopped:
            return
        self.stopped = False
        self.state = "Fetching metadata"
        self.announce_now = asyncio.Event()
        loop = asyncio.get_running_loop()
        coros = [self.tracker_loop(), self.connect_loop()]
        if self.mgr.dht:
            coros.append(self.dht_loop())
        self.tasks = [loop.create_task(c) for c in coros]

    async def stop(self, announce=False):
        self.stopped = True
        for tk in self.tasks + list(self.conn_tasks):
            tk.cancel()
        self.tasks = []
        for w in list(self.peers):
            w.close()
        self.state = "Paused"

    async def connect(self, addr):
        self.addrs.add(addr)
        self.tried[addr] = time.time()
        w = None
        try:
            r, w = await self.mgr.dial(addr, self.t.info_hash)
            self.peers.add(w)
            await self.talk(r, w)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError,
                ValueError, struct.error, IndexError, AttributeError, KeyError, TypeError):
            pass
        finally:
            self.peers.discard(w)
            self.addrs.discard(addr)
            if w:
                w.close()

    def _request(self, send_ext, ut):
        if not self.size:
            return
        now = time.time()
        for p in range(self.npieces):
            if p not in self.pieces and now - self.pending.get(p, 0) > 15:
                self.pending[p] = now
                send_ext(ut, bencode({"msg_type": 0, "piece": p}))
                return

    async def talk(self, r, w):
        ih = self.t.info_hash
        w.write(PSTR + b"\0\0\0\0\0\x10\0\0" + ih + self.mgr.peer_id)
        hs = await asyncio.wait_for(r.readexactly(68), 10)
        if (hs[:20] != PSTR or hs[28:48] != ih or not hs[25] & 0x10
                or hs[48:68] == self.mgr.peer_id):
            return

        def send_ext(eid, body):
            w.write(struct.pack(">IBB", len(body) + 2, 20, eid) + body)

        send_ext(0, bencode({"m": {"ut_metadata": 1}}))
        ut, idle = None, 0
        while not self.finished and not self.stopped:
            try:
                hdr = await asyncio.wait_for(r.readexactly(4), 10)
            except asyncio.TimeoutError:
                idle += 10
                if idle >= 60:
                    return
                if ut:
                    self._request(send_ext, ut)
                continue
            idle = 0
            ln = struct.unpack(">I", hdr)[0]
            if ln == 0:
                continue
            if ln > (1 << 20) + 16:
                raise ValueError("message too large")
            data = await asyncio.wait_for(r.readexactly(ln), 30)
            if data[0] != 20 or len(data) < 2:
                continue
            eid, body = data[1], data[2:]
            if eid == 0:  # extension handshake
                d = bdecode(body)
                ut = d.get(b"m", {}).get(b"ut_metadata")
                size = d.get(b"metadata_size")
                if not ut or not isinstance(size, int) or not 0 < size <= 16 << 20:
                    return
                if self.size is None:
                    self.size = size
                elif self.size != size:
                    return
                for _ in range(3):
                    self._request(send_ext, ut)
            elif eid == 1:  # ut_metadata reply
                d, end = _dec(body, 0)
                mt, pc = d.get(b"msg_type"), d.get(b"piece")
                if mt == 2:
                    return
                if mt == 1 and isinstance(pc, int) and self.size and 0 <= pc < self.npieces:
                    block = body[end:]
                    if len(block) != min(BLOCK, self.size - pc * BLOCK):
                        raise ValueError("bad metadata block")
                    self.pending.pop(pc, None)
                    self.pieces.setdefault(pc, block)
                    if len(self.pieces) == self.npieces:
                        self._verify()
                    elif ut:
                        self._request(send_ext, ut)

    def _verify(self):
        raw = b"".join(self.pieces[i] for i in range(self.npieces))
        if hashlib.sha1(raw).digest() == self.t.info_hash:
            if not self.finished:
                self.finished = True
                asyncio.get_running_loop().create_task(self.mgr._finish_magnet(self, raw))
        else:  # a peer sent junk: start over
            self.pieces.clear()
            self.pending.clear()
            self.size = None


def _compact(b):
    return [(socket.inet_ntoa(b[i:i + 4]), struct.unpack(">H", b[i + 4:i + 6])[0])
            for i in range(0, len(b) - 5, 6)]


def _compact6(b):
    """BEP 7 compact IPv6 peers: 16-byte address + 2-byte port."""
    return [(socket.inet_ntop(socket.AF_INET6, b[i:i + 16]), struct.unpack(">H", b[i + 16:i + 18])[0])
            for i in range(0, len(b) - 17, 18)]


def ipv6_address():
    """This computer's global IPv6 address (portmap.global_ipv6), or None."""
    return portmap.global_ipv6()


def _in_daemon(loop, fn, *args):
    """Run a blocking call (a tracker announce) on a daemon thread; returns an asyncio future.
    Not the loop's default executor: Python waits for executor threads at exit, so a dead tracker
    (8-16 s per address family) kept the process alive after quitting - and the update installer,
    tired of waiting, force-closed it together with itself."""
    fut = loop.create_future()

    def done(res, exc):
        if not fut.done():
            fut.set_exception(exc) if exc is not None else fut.set_result(res)

    def run():
        try:
            res, exc = fn(*args), None
        except BaseException as e:      # handed to the awaiting coroutine
            res, exc = None, e
        try:
            loop.call_soon_threadsafe(done, res, exc)
        except RuntimeError:            # the loop is closed: nobody is waiting any more
            pass

    threading.Thread(target=run, daemon=True, name="announce").start()
    return fut


def _global_ip(ip):
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_global
    except ValueError:
        return False


def _is_v6(ip):
    return ":" in ip


def _good_peer_ip(ip):
    """An address worth connecting to: not unspecified, multicast or (scope-less) IPv6 link-local. IPv4-mapped IPv6
    addresses are not used as such (the IPv4 form is)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_unspecified or a.is_multicast:
        return False
    if a.version == 6 and (a.is_link_local or a.ipv4_mapped is not None):
        return False
    return True


# --------------------------------------------------------------------------
# DHT (BEP 5) - Kademlia over UDP
# --------------------------------------------------------------------------
BOOTSTRAP = [("router.bittorrent.com", 6881), ("dht.transmissionbt.com", 6881),
             ("router.utorrent.com", 6881), ("dht.libtorrent.org", 25401)]
K = 8
ALPHA = 8


def _dist(a, b):
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


def _pack_peer(addr):
    """Compact peer: 6 bytes for IPv4, 18 for IPv6."""
    if _is_v6(addr[0]):
        return socket.inet_pton(socket.AF_INET6, addr[0]) + struct.pack(">H", addr[1])
    return socket.inet_aton(addr[0]) + struct.pack(">H", addr[1])


def _parse_nodes(b):
    return [(b[i:i + 20], (socket.inet_ntoa(b[i + 20:i + 24]), struct.unpack(">H", b[i + 24:i + 26])[0]))
            for i in range(0, len(b) - 25, 26)]


def _parse_nodes6(b):
    """BEP 32 "nodes6": 20-byte id + 16-byte address + 2-byte port."""
    return [(b[i:i + 20], (socket.inet_ntop(socket.AF_INET6, b[i + 20:i + 36]), struct.unpack(">H", b[i + 36:i + 38])[0]))
            for i in range(0, len(b) - 37, 38)]


class DHT:
    """One Kademlia node (BEP 5). Packets arrive via UDPMux, sharing the
    UDP socket with uTP."""

    def __init__(self, bootstrap=None, family=socket.AF_INET, node_id=None):
        self.family = family
        self.id = node_id or os.urandom(20)
        self.bootstrap_nodes = BOOTSTRAP if bootstrap is None else bootstrap
        self.nodes = {}      # (ip, port) -> [node_id, last_seen, failures]
        self.store = {}      # info_hash -> {(ip, port): announced_at}
        self.pending = {}    # transaction id -> future
        self.secret = self.old_secret = os.urandom(8)
        self.rotated = time.time()
        self.transport = None
        self.port = 0
        self.ready = None
        self.tasks = []

    # -- lifecycle --------------------------------------------------------
    def attach(self, transport, port):
        self.transport, self.port = transport, port
        self.ready = asyncio.Event()
        self.tasks = [asyncio.get_running_loop().create_task(self._maintain())]

    def close(self):
        for t in self.tasks:
            t.cancel()

    async def _maintain(self):
        empty_wait = 5   # an empty table retries soon (a lost bootstrap packet must not cost a minute), backing off to 60 s
        while True:
            if len(self.nodes) < 30:
                try:
                    await asyncio.wait_for(self._lookup(self.id, "find_node", {"target": self.id}), 25)
                except Exception:
                    pass
            self.ready.set()
            if not self.nodes:
                await asyncio.sleep(empty_wait)
                empty_wait = min(empty_wait * 2, 60)
                continue
            empty_wait = 5
            await asyncio.sleep(60 if len(self.nodes) < 30 else 300)
            now = time.time()
            if now - self.rotated > 300:  # tokens stay valid for ~10 minutes
                self.old_secret, self.secret, self.rotated = self.secret, os.urandom(8), now
            for ih in list(self.store):
                self.store[ih] = {a: t for a, t in self.store[ih].items() if now - t < 1800}
                if not self.store[ih]:
                    del self.store[ih]

    # -- routing table ----------------------------------------------------
    def add_node(self, nid, addr):
        if not isinstance(nid, bytes) or len(nid) != 20 or nid == self.id or not addr[1]:
            return
        if _is_v6(addr[0]) != (self.family == socket.AF_INET6):     # this table is for one address family only
            return
        self.nodes[addr] = [nid, time.time(), 0]
        if len(self.nodes) > 1500:
            oldest = min(self.nodes, key=lambda a: self.nodes[a][1])
            del self.nodes[oldest]

    def _fail(self, addr):
        n = self.nodes.get(addr)
        if n:
            n[2] += 1
            if n[2] >= 3:
                del self.nodes[addr]

    def closest(self, target, k):
        best = sorted(self.nodes.items(), key=lambda kv: _dist(kv[1][0], target))[:k]
        return [(addr, v[0]) for addr, v in best]

    # -- wire -------------------------------------------------------------
    def _send(self, msg, addr):
        try:
            self.transport.sendto(bencode(msg), addr)
        except (OSError, AttributeError):
            pass

    def _token(self, ip, secret=None):
        return hashlib.sha1((secret or self.secret) + ip.encode()).digest()[:8]

    def datagram_received(self, data, addr):
        try:
            msg = bdecode(data)
            y, t = msg.get(b"y"), msg.get(b"t")
            if y == b"r":
                fut = self.pending.get(t)
                r = msg.get(b"r")
                if isinstance(r, dict):
                    self.add_node(r.get(b"id"), addr)
                    if fut and not fut.done():
                        fut.set_result(r)
            elif y == b"e":
                fut = self.pending.get(t)
                if fut and not fut.done():
                    fut.set_result(None)
            elif y == b"q":
                self._on_query(msg, addr)
        except Exception:
            pass  # malformed packet

    def _on_query(self, msg, addr):
        a, q, t = msg.get(b"a"), msg.get(b"q"), msg.get(b"t")
        if not isinstance(a, dict) or not isinstance(t, bytes):
            return
        self.add_node(a.get(b"id"), addr)

        def reply(r):
            r["id"] = self.id
            self._send({"t": t, "y": "r", "r": r}, addr)

        def error(code, text):
            self._send({"t": t, "y": "e", "e": [code, text]}, addr)

        def nodes_for(target):
            blob = b"".join(nid + _pack_peer(ad) for ad, nid in self.closest(target, K))
            return {"nodes6" if self.family == socket.AF_INET6 else "nodes": blob}

        if q == b"ping":
            reply({})
        elif q == b"find_node":
            target = a.get(b"target")
            if not isinstance(target, bytes) or len(target) != 20:
                return error(203, "bad target")
            reply(nodes_for(target))
        elif q == b"get_peers":
            ih = a.get(b"info_hash")
            if not isinstance(ih, bytes) or len(ih) != 20:
                return error(203, "bad info_hash")
            r = {"token": self._token(addr[0])}
            known = list(self.store.get(ih, {}))[:50]
            if known:
                r["values"] = [_pack_peer(x) for x in known]
            else:
                r.update(nodes_for(ih))
            reply(r)
        elif q == b"announce_peer":
            ih, tok = a.get(b"info_hash"), a.get(b"token")
            if not isinstance(ih, bytes) or len(ih) != 20:
                return error(203, "bad info_hash")
            if tok not in (self._token(addr[0]), self._token(addr[0], self.old_secret)):
                return error(203, "bad token")
            port = addr[1] if a.get(b"implied_port") else a.get(b"port")
            if not isinstance(port, int) or not 0 < port < 65536:
                return error(203, "bad port")
            if len(self.store) < 5000:
                self.store.setdefault(ih, {})[(addr[0], port)] = time.time()
            reply({})
        else:
            error(204, "method unknown")

    async def query(self, addr, q, args, timeout=3.0):
        if self.family == socket.AF_INET6 and q in ("find_node", "get_peers"):
            args = dict(args, want=[b"n6"])                          # BEP 32: IPv6 nodes, please
        loop = asyncio.get_running_loop()
        tid = os.urandom(2)
        while tid in self.pending:
            tid = os.urandom(2)
        fut = loop.create_future()
        self.pending[tid] = fut
        args = dict(args)
        args["id"] = self.id
        self._send({"t": tid, "y": "q", "q": q, "a": args}, addr)
        try:
            r = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            r = None
        finally:
            self.pending.pop(tid, None)
        if r is None:
            self._fail(addr)
        return r

    async def ping(self, addr):
        await self.query(addr, "ping", {})

    # -- lookups ----------------------------------------------------------
    async def _resolve_bootstrap(self):
        loop = asyncio.get_running_loop()
        res = await asyncio.gather(
            *(loop.getaddrinfo(h, p, family=self.family, type=socket.SOCK_DGRAM)
              for h, p in self.bootstrap_nodes), return_exceptions=True)
        return [r[0][4][:2] for r in res if isinstance(r, list) and r]

    async def _lookup(self, target, method, extra):
        """Iterative Kademlia lookup. Returns (peers, tokens, responded)."""
        shortlist = {addr: nid for addr, nid in self.closest(target, 16)}
        if not shortlist:
            for addr in await self._resolve_bootstrap():
                shortlist[addr] = None
        queried, responded, tokens, peers = set(), {}, {}, set()

        def key(a):
            return _dist(shortlist[a], target) if shortlist[a] else 1 << 161

        for _ in range(14):
            todo = [a for a in sorted(shortlist, key=key)[:K] if a not in queried][:ALPHA]
            if not todo:
                break
            queried.update(todo)
            results = await asyncio.gather(*(self.query(a, method, extra) for a in todo))
            for addr, r in zip(todo, results):
                if not r:
                    shortlist.pop(addr, None)
                    continue
                nid = r.get(b"id")
                if isinstance(nid, bytes) and len(nid) == 20:
                    shortlist[addr] = nid
                    responded[addr] = nid
                if isinstance(r.get(b"token"), bytes):
                    tokens[addr] = r[b"token"]
                values = r.get(b"values")
                for v in values if isinstance(values, list) else []:
                    if isinstance(v, bytes) and len(v) == 6:
                        peers.add((socket.inet_ntoa(v[:4]), struct.unpack(">H", v[4:])[0]))
                    elif isinstance(v, bytes) and len(v) == 18:
                        peers.update(_compact6(v))
                v6 = self.family == socket.AF_INET6
                blob = r.get(b"nodes6" if v6 else b"nodes")
                if isinstance(blob, bytes):
                    for n_id, n_addr in (_parse_nodes6 if v6 else _parse_nodes)(blob):
                        if n_id != self.id and n_addr[1] and n_addr not in shortlist:
                            shortlist[n_addr] = n_id
        return peers, tokens, responded

    async def get_peers(self, info_hash, announce_port=0):
        """Look up peers for info_hash; if announce_port is set, announce ourselves too."""
        try:
            await asyncio.wait_for(self.ready.wait(), 20)
        except asyncio.TimeoutError:
            pass
        peers, tokens, responded = await asyncio.wait_for(
            self._lookup(info_hash, "get_peers", {"info_hash": info_hash}), 30)
        if announce_port:
            closest = sorted((a for a in responded if a in tokens),
                             key=lambda a: _dist(responded[a], info_hash))[:K]
            await asyncio.gather(*(self.query(
                a, "announce_peer", {"info_hash": info_hash, "port": announce_port,
                                     "token": tokens[a], "implied_port": 0}) for a in closest))
        return peers


# --------------------------------------------------------------------------
# Manager (thread-safe facade used by the GUI)
# --------------------------------------------------------------------------
class _V6Sender:
    """An IPv6 datagram transport that takes (ip, port) addresses: Windows' proactor event loop rejects the 2-tuple
    form with WSAEINVAL (reported only through error_received, so the packet just vanishes)."""

    def __init__(self, transport):
        self._t = transport
        self.sent = set()      # (ip, port) we have sent to: their packets are replies a firewall lets through

    def sendto(self, data, addr=None):
        if addr is not None:
            if len(self.sent) > 100000:
                self.sent.clear()
            self.sent.add((addr[0], addr[1]))
            if len(addr) == 2:
                addr = (addr[0], addr[1], 0, 0)
        self._t.sendto(data, addr)

    def __getattr__(self, name):
        return getattr(self._t, name)


class UDPMux(asyncio.DatagramProtocol):
    """One UDP socket shared by the DHT (bencoded: starts with 'd') and uTP."""

    def __init__(self, on_unsolicited=None):
        self.transport = None
        self.dht = None
        self.utp = None
        self.on_unsolicited = on_unsolicited   # IPv6: a packet from someone we never sent to (= reachable)

    def connection_made(self, transport):
        sock = transport.get_extra_info("socket")
        self.transport = _V6Sender(transport) if sock is not None and sock.family == socket.AF_INET6 else transport

    def datagram_received(self, data, addr):
        if not data:
            return
        addr = addr[:2]
        if self.on_unsolicited and isinstance(self.transport, _V6Sender) and addr not in self.transport.sent:
            self.on_unsolicited(addr)
        try:
            if data[:1] == b"d":
                if self.dht:
                    self.dht.datagram_received(data, addr)
            elif self.utp:
                self.utp.datagram_received(data, addr)
        except Exception:
            pass

    def error_received(self, exc):
        pass


def _bind(family, kind, port):
    s = socket.socket(family, kind)
    try:
        s.setblocking(False)
        if kind == socket.SOCK_STREAM and os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:          # IPv6 only: IPv4 has its own socket on the same port
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind(("::", port))
        else:
            s.bind(("0.0.0.0", port))
    except OSError:
        s.close()
        raise
    return s


CLIENTS = {"qB": "qBittorrent", "TR": "Transmission", "UT": "\u00B5Torrent", "UM": "\u00B5Torrent Mac", "lt": "libtorrent",
           "LT": "libtorrent", "DE": "Deluge", "AZ": "Vuze", "BT": "BitTorrent", "KT": "KTorrent", "BI": "BiglyBT",
           "WW": "WebTorrent", "WD": "WebTorrent Desktop", "FD": "Free Download Manager", "PY": "Bit Torrent Client",
           "BC": "BitComet", "XL": "Xunlei", "LW": "LimeWire", "FW": "FrostWire", "TL": "Tribler", "SD": "Thunder"}


def client_name(pid):
    """Readable client name from an Azureus-style peer id such as -qB4630-."""
    try:
        if pid and len(pid) >= 8 and pid[0:1] == b"-" and pid[7:8] == b"-":
            code = pid[1:3].decode("ascii")
            digits = pid[3:7].decode("ascii")
            parts = [str(int(c, 36)) for c in digits]
            while len(parts) > 2 and parts[-1] == "0":
                parts.pop()
            ver = ".".join(parts)
            name = CLIENTS.get(code)
            if name:
                return (name + " " + ver).strip()
    except (UnicodeDecodeError, ValueError):
        pass
    return "Unknown"


def _hex(s):
    return s.t.hex if isinstance(s, Session) else s.hex


class Manager:
    """Thread-safe facade: owns the event loop, listeners, DHT, uTP and global settings.

    encryption: "off" | "prefer" | "light" | "require"   (Message Stream Encryption; light = encrypted handshake, plain data)
    utp:        "off" | "fallback" | "prefer"     (TCP first / uTP only if TCP fails / uTP first)
    """

    def __init__(self, port=6881, dht=True, dht_bootstrap=None, encryption="prefer",
                 utp="fallback", down_limit=0, up_limit=0, portmap=False, ipv6=True):
        self.dht = None
        self.ipv6_wanted = ipv6  # also listen / connect over IPv6 when the system has it
        self.ipv6 = False        # ... and it does: an IPv6 listener is up
        self.inbound6 = {"tcp": 0, "udp": 0}   # connections / packets from the Internet nobody asked for: reachable
        self.started = time.time()
        self.portmap = None
        self.portmap_opts = {}  # PortMapper overrides (tests)
        self.udp_port = 0
        self.dhts = []
        self._use_dht, self._dht_bootstrap = dht, dht_bootstrap
        self.encryption = encryption
        self.utp_mode = utp
        self.down, self.up = RateLimiter(), RateLimiter()
        self.set_limits(down_limit, up_limit)
        self.pex_interval = 60
        self.banned = set()      # peer IPs we never talk to
        self.udp = {}
        self.servers = []
        self.peer_id = b"-PY0100-" + bytes(random.randrange(48, 123) for _ in range(12))
        self.sessions = {}
        self.port = 0
        self.on_metadata = None  # callback(hex, torrent_bytes) once a magnet resolves
        self.on_finished = None  # callback(hex, name) when a download completes (network thread)
        self.seed_ratio = 0.0    # stop seeding at this share ratio (0 = never)
        self.seed_minutes = 0    # ... or after this long (0 = never)
        self.seed_action = "pause"   # what to do then: "pause" or "remove"
        self.on_seed_limit = None    # callback(hex, action) on the network thread
        self._last_tick = time.monotonic()
        self.max_active = 0      # most torrents downloading at once (0 = no limit); the rest wait in the queue
        self._seq = 0
        self._closing = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self._run(self._serve(port))
        self.loop.call_soon_threadsafe(self._sched_tick)
        if portmap:
            self.set_portmap(True)

    # -- download queue ---------------------------------------------------
    def _next_order(self):
        self._seq += 1
        return self._seq

    def _sched_tick(self):
        if self._closing:
            return
        try:
            self._schedule()
        finally:
            self.loop.call_later(2.0, self._sched_tick)

    def _kick(self):
        """Re-run the queue now (safe from any thread)."""
        try:
            self.loop.call_soon_threadsafe(self._schedule)
        except RuntimeError:
            pass

    def _schedule(self):
        """Loop thread: the first `max_active` unfinished, un-paused torrents (in queue order) run, the rest wait."""
        if self._closing:
            return
        slots = self.max_active if self.max_active > 0 else float("inf")
        waiting = [s for s in self.sessions.values() if isinstance(s, Session) and not s.user_paused
                   and not s.error and not s.done]
        for s in sorted(waiting, key=lambda x: (-x.priority, x.order)):
            if slots > 0:
                slots -= 1
                if s.stopped:
                    self.loop.create_task(s.start())
            elif not s.stopped:
                self.loop.create_task(self._park(s))
            elif s.state != "Queued":
                s.state = "Queued"
        for s in self.sessions.values():   # finished torrents just seed; they never wait in the queue
            if isinstance(s, Session) and s.stopped and not s.user_paused and not s.error and s.done:
                self.loop.create_task(s.start())
        self._check_seeding()

    def _check_seeding(self):
        now = time.monotonic()
        dt, self._last_tick = min(now - self._last_tick, 10.0), now
        for h, s in list(self.sessions.items()):
            if not isinstance(s, Session) or not s.complete or s.stopped:
                continue
            s.seed_secs += dt
            if s.rule_ignore or s.rule_hit or s.user_paused:
                continue
            hit = (self.seed_ratio > 0 and s.share_ratio >= self.seed_ratio) or \
                  (self.seed_minutes > 0 and s.seed_secs >= self.seed_minutes * 60)
            if hit:
                s.rule_hit = True
                if self.on_seed_limit:
                    try:
                        self.on_seed_limit(h, self.seed_action)
                    except Exception:
                        pass

    def set_seed_rules(self, ratio=0.0, minutes=0, action="pause"):
        self.seed_ratio, self.seed_minutes = max(0.0, float(ratio)), max(0, int(minutes))
        self.seed_action = action if action in ("pause", "remove") else "pause"
        for s in self.sessions.values():
            if isinstance(s, Session):
                s.rule_hit = False

    def set_torrent_limits(self, h, down_kib, up_kib):
        s = self.sessions.get(h)
        if isinstance(s, Session):
            s.set_limits(down_kib, up_kib)

    def set_priority(self, hashes, priority):
        for h in hashes:
            s = self.sessions.get(h)
            if s is not None:
                s.priority = max(-1, min(1, int(priority)))
        self._kick()

    def set_category(self, hashes, category):
        for h in hashes:
            s = self.sessions.get(h)
            if s is not None:
                s.category = category or ""

    def relocate(self, h, new_dir):
        """Move a finished torrent's files to new_dir (blocking; call from a worker thread)."""
        s = self.sessions.get(h)
        if isinstance(s, Session):
            self._run(s.relocate(new_dir), timeout=3600)

    async def _park(self, s):
        await s.stop(announce=True, state="Paused" if s.user_paused else "Queued")
        if not s.user_paused:
            s.state = "Queued"

    def set_max_active(self, n):
        self.max_active = max(0, int(n))
        self._kick()

    def details(self, h):
        """Files, peers and trackers of one torrent for the details tabs (None if it has no file list yet)."""
        s = self.sessions.get(h)
        if not isinstance(s, Session):
            return None
        prog = s.file_progress()
        files = [("/".join(f[0][1:] if s.t.multi else f[0]), f[1], prog[i], i in s.skip, i in s.high)
                 for i, f in enumerate(s.t.files)]
        n = max(len(s.have), 1)
        peers = []
        for p in list(s.peers):
            flags = ("E" if p.encrypted else "") + ("I" if p.incoming else "") + ("u" if p.kind == "utp" else "")
            peers.append(dict(key=id(p), addr="%s:%d" % (p.addr[0], p.addr[1]) if ":" not in p.addr[0]
                              else "[%s]:%d" % (p.addr[0], p.addr[1]),
                              client=client_name(p.pid), flags=flags, progress=sum(p.has) / n,
                              rx=p.rx_total, tx=p.tx_total, choked=p.choked, interested=p.interested))
        now = time.time()
        trackers = [(u, *(s.tracker_status.get(u) or ("Waiting...", now))) for u in s.t.trackers]
        return dict(files=files, peers=peers, trackers=trackers, private=s.t.private,
                    pieces=len(s.t.hashes), piece_length=s.t.piece_length, have=sum(s.have), sequential=s.sequential)

    def move(self, hashes, where):
        """Change queue position: where is 'top', 'up', 'down' or 'bottom' (moves the whole selection)."""
        order = sorted((s for s in self.sessions.values()), key=lambda x: x.order)
        sel = [s for s in order if _hex(s) in hashes]
        if not sel:
            return
        rest = [s for s in order if s not in sel]
        if where == "top":
            order = sel + rest
        elif where == "bottom":
            order = rest + sel
        elif where == "up":
            idx = max(order.index(sel[0]) - 1, 0)
            order = [s for s in order[:idx] if s not in sel] + sel + [s for s in order[idx:] if s not in sel]
        else:
            idx = min(order.index(sel[-1]) + 1, len(order) - 1)
            order = [s for s in order[:idx + 1] if s not in sel] + sel + [s for s in order[idx + 1:] if s not in sel]
        for i, s in enumerate(order):
            s.order = i
        self._seq = max(self._seq, len(order))
        self._kick()

    def set_skip(self, h, skip):
        """Choose which files of a torrent to skip. Raises ValueError for a bad choice."""
        s = self.sessions.get(h)
        if not isinstance(s, Session):
            raise ValueError("this torrent has no file list yet")

        async def go():
            s.set_skip(skip)
            self._schedule()
        self._run(go())

    def set_file_prio(self, h, indices, level):
        """level: 'skip' | 'normal' | 'high'. Raises ValueError for a bad choice."""
        s = self.sessions.get(h)
        if not isinstance(s, Session):
            raise ValueError("this torrent has no file list yet")

        async def go():
            s.set_file_prio(indices, level)
            self._schedule()
        self._run(go())

    def stream_prepare(self, h, file_idx):
        """Get a torrent ready to be played: make sure the file is wanted and the torrent is running. Returns
        (name, size) of the file. Raises ValueError if it cannot be streamed."""
        s = self.sessions.get(h)
        if not isinstance(s, Session):
            raise ValueError("the torrent's file list is not known yet")
        if not 0 <= file_idx < len(s.t.files):
            raise ValueError("no such file")
        if file_idx in s.skip:
            self.set_file_prio(h, [file_idx], "normal")
        s.stream_touch(file_idx, 0)
        if s.user_paused:
            self.resume(h)
        self._kick_stream(s)
        path, ln, _off = s.t.files[file_idx]
        return path[-1], ln

    def stream_hint(self, h, file_idx, duration):
        """The player knows the length of the video: remember bytes per second so the look-ahead matches its bitrate."""
        s = self.sessions.get(h)
        if isinstance(s, Session) and 0 <= file_idx < len(s.t.files) and duration and duration > 1:
            s.stream_rate = s.t.files[file_idx][1] / float(duration)
            return s.stream_rate
        return 0.0

    def stream_prefetch(self, h, file_idx):
        """Start fetching a file that will be played next (the next episode)."""
        s = self.sessions.get(h)
        if not isinstance(s, Session) or not 0 <= file_idx < len(s.t.files):
            return False
        if file_idx in s.skip:
            self.set_file_prio(h, [file_idx], "high")
        elif file_idx not in s.high:
            self.set_file_prio(h, [file_idx], "high")
        return True

    def _kick_stream(self, s):
        def go():
            for p in list(s.peers):
                p.fill()
        self.loop.call_soon_threadsafe(go)

    def stream_info(self, h, file_idx):
        """(size, bytes of the file that are downloaded in order from the start, list of (start,end) byte ranges held)."""
        s = self.sessions.get(h)
        if not isinstance(s, Session) or not 0 <= file_idx < len(s.t.files):
            return None
        _p, ln, off = s.t.files[file_idx]
        pl, have = s.t.piece_length, s.have
        ranges, cur = [], None
        for i in range(off // pl, (off + max(ln, 1) - 1) // pl + 1):
            a, b = max(off, i * pl) - off, min(off + ln, i * pl + s.t.piece_size(i)) - off
            if have[i]:
                if cur and cur[1] == a:
                    cur[1] = b
                else:
                    cur = [a, b]
                    ranges.append(cur)
            else:
                cur = None
        return dict(size=ln, ranges=[tuple(r) for r in ranges])

    def stream_read(self, h, file_idx, start, n, timeout=90, cancel=None, touch=True):
        """Bytes [start, start+n) of a file, waiting for them to download (called on the streaming server's threads).
        Returns None on timeout / cancel / when the torrent is gone."""
        s = self.sessions.get(h)
        if not isinstance(s, Session) or not 0 <= file_idx < len(s.t.files):
            return None
        _p, ln, off = s.t.files[file_idx]
        n = max(0, min(n, ln - start))
        if n == 0:
            return b""
        if file_idx in s.skip:                      # a file the player moved on to (next episode, subtitles)
            self.set_file_prio(h, [file_idx], "normal")
        end, last_kick = time.time() + timeout, 0.0
        while not s.stream_ready(file_idx, start, n):
            if cancel and cancel() or time.time() > end or self._closing or self.sessions.get(h) is not s:
                return None
            now = time.time()
            if touch:
                s.stream_touch(file_idx, start)
            if now - last_kick > 1.0:
                last_kick = now
                self._kick_stream(s)
            time.sleep(0.05)
        if touch:
            s.stream_touch(file_idx, start + n)
        return s.storage.read(off + start, n)

    def set_sequential(self, hashes, on):
        for h in hashes:
            s = self.sessions.get(h)
            if isinstance(s, Session):
                s.sequential = bool(on)

    def ban(self, ip):
        """Refuse and disconnect a peer address for good (until unbanned)."""
        self.banned.add(ip)

        async def go():
            for s in list(self.sessions.values()):
                if isinstance(s, Session):
                    for p in list(s.peers):
                        if p.addr[0] == ip:
                            p.close()
        self._run(go())

    def unban(self, ip=None):
        if ip is None:
            self.banned.clear()
        else:
            self.banned.discard(ip)

    def _run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def set_limits(self, down_kib, up_kib):
        """Global speed limits in KiB/s (0 = unlimited)."""
        self.down.rate = max(0, int(down_kib)) * 1024
        self.up.rate = max(0, int(up_kib)) * 1024

    async def _serve(self, port):
        loop = asyncio.get_running_loop()
        for p in range(port, port + 20):
            try:
                s4 = _bind(socket.AF_INET, socket.SOCK_STREAM, p)
            except OSError:
                continue
            self.servers.append(await asyncio.start_server(self._incoming, sock=s4))
            self.port = s4.getsockname()[1]
            break
        else:   # all 20 taken or reserved (Windows excludes whole blocks): let the OS pick, never run without a listener
            try:
                s4 = _bind(socket.AF_INET, socket.SOCK_STREAM, 0)
                self.servers.append(await asyncio.start_server(self._incoming, sock=s4))
                self.port = s4.getsockname()[1]
            except OSError:
                pass
        fams = [socket.AF_INET]
        # IPv6: a second listener on the same port number (IPv6-only sockets, so both families can use it)
        if self.ipv6_wanted and socket.has_ipv6 and self.port:
            try:
                s6 = _bind(socket.AF_INET6, socket.SOCK_STREAM, self.port)
                self.servers.append(await asyncio.start_server(self._incoming, sock=s6))
                self.ipv6 = True
                fams.append(socket.AF_INET6)
            except OSError:
                self.ipv6 = False
        # UDP: the DHT and uTP share one socket
        node_id = os.urandom(20)
        for fam in fams:
            sock = None
            for udp_port in ((self.udp_port or self.port, 0) if fam == socket.AF_INET6 else (self.port, 0)):
                try:
                    sock = _bind(fam, socket.SOCK_DGRAM, udp_port)
                    break
                except OSError:
                    continue
            if sock is None:
                continue
            mux = UDPMux(self._inbound6_udp if fam == socket.AF_INET6 else None)
            await loop.create_datagram_endpoint(lambda m=mux: m, sock=sock)
            if fam == socket.AF_INET:
                self.udp_port = sock.getsockname()[1]
            mux.utp = utp.UTPSocket(mux.transport.sendto, self._utp_accept,
                                    lambda: self.utp_mode != "off")
            mux.utp.start()
            if self._use_dht:
                d = DHT(self._dht_bootstrap, fam, node_id)
                d.attach(mux.transport, sock.getsockname()[1])
                mux.dht = d
                self.dhts.append(d)
            self.udp[fam] = mux
        self.dht = self.dhts[0] if self.dhts else None

    def set_portmap(self, enabled):
        """Turn automatic router port forwarding (PCP / NAT-PMP / UPnP) on or off."""
        if enabled and self.portmap is None and self.port:
            opts = dict(on_change=self._reannounce)
            opts.update(self.portmap_opts)
            if self.ipv6 and "ports6" not in opts:    # IPv6: ask the router's firewall to let peers in
                m6 = self.udp.get(socket.AF_INET6)
                u6 = m6.transport.get_extra_info("sockname")[1] if m6 else 0
                opts["ports6"] = [("TCP", self.port)] + ([("UDP", u6)] if u6 else [])
            self.portmap = portmap.PortMapper([("TCP", self.port), ("UDP", self.udp_port or self.port)], **opts)
            self.portmap.start()
        elif not enabled and self.portmap is not None:
            m, self.portmap = self.portmap, None
            threading.Thread(target=m.stop, daemon=True).start()  # removing mappings may take a moment

    def _reannounce(self):
        """Ports or addresses changed (router mapping): tell every tracker right away."""
        for s in list(self.sessions.values()):
            ev = getattr(s, "announce_now", None)
            if ev is not None:
                self.loop.call_soon_threadsafe(ev.set)

    @property
    def public_port(self):
        """The TCP port peers should connect to: the router-assigned external port if one was given."""
        pm = self.portmap
        return (pm.ext.get(("TCP", self.port)) if pm else None) or self.port

    @property
    def public_udp_port(self):
        """The UDP port (DHT / uTP) as seen from outside."""
        pm = self.portmap
        local = self.udp_port or self.port
        return (pm.ext.get(("UDP", local)) if pm else None) or local

    def dht_for(self, ip):
        fam = socket.AF_INET6 if _is_v6(ip) else socket.AF_INET
        return next((d for d in self.dhts if d.family == fam), None)

    def _inbound6_udp(self, addr):
        if _global_ip(addr[0]):
            self.inbound6["udp"] += 1

    def ipv6_reachable(self):
        """True: peers on the Internet reach us over IPv6. False: nothing came in although we have been
        busy long enough that something should have (the router's firewall blocks it). None: too early to say."""
        if sum(self.inbound6.values()):
            return True
        d6 = self.dht_for("::1")
        busy = time.time() - self.started > 900 and d6 is not None and len(d6.nodes) >= 20
        return False if busy else None

    def ipv6_status(self):
        """Short text for Settings / Diagnostics."""
        if not self.ipv6_wanted:
            return "off"
        if not self.ipv6:
            return "not available on this computer"
        a = ipv6_address()
        if not a:
            return "on, but this network has no IPv6 Internet"
        txt = "on (%s)" % a
        reach = self.ipv6_reachable()
        if reach:
            txt += "; peers can reach you (%d incoming connection(s), %d packet(s))" % (
                self.inbound6["tcp"], self.inbound6["udp"])
        elif reach is False:
            txt += "; nothing has come in yet, so the router's IPv6 firewall is probably blocking incoming connections"
        else:
            txt += "; checking whether peers can reach you"
        pm = self.portmap
        if pm is not None and getattr(pm, "status6", ""):
            txt += "\nIPv6 firewall: %s" % pm.status6
        return txt

    def family_ok(self, ip):
        """Can we reach this address (IPv6 needs our IPv6 listener / sockets)?"""
        return _good_peer_ip(ip) and (not _is_v6(ip) or self.ipv6)

    def _utp_accept(self, r, w):
        asyncio.get_running_loop().create_task(self._incoming(r, w))

    # -- connections --------------------------------------------------------
    def _transports(self, addr):
        can_utp = self.utp_mode != "off" and (socket.AF_INET6 if _is_v6(addr[0]) else socket.AF_INET) in self.udp
        if not can_utp:
            return ["tcp"]
        return ["utp", "tcp"] if self.utp_mode == "prefer" else ["tcp", "utp"]

    async def _open(self, kind, addr):
        if kind == "tcp":
            return await asyncio.wait_for(asyncio.open_connection(addr[0], addr[1]), 8)
        return await self.udp[socket.AF_INET6 if _is_v6(addr[0]) else socket.AF_INET].utp.connect(addr, timeout=6)

    async def dial(self, addr, info_hash):
        """Open an outgoing peer connection (TCP/uTP, optionally encrypted); returns (reader, writer)
        ready for the BitTorrent handshake."""
        if _is_v6(addr[0]) and not self.ipv6:
            raise OSError("IPv6 is not available here")
        # light: encrypted handshake with plain data first, then full RC4 if the peer insists, then fully plain
        modes = {"off": ["plain"], "prefer": ["rc4", "plain"], "light": ["light", "rc4", "plain"],
                 "require": ["rc4"]}.get(self.encryption, ["plain"])
        last = OSError("connect failed")
        for enc in modes:
            reached = False
            for kind in self._transports(addr):
                try:
                    r, w = await self._open(kind, addr)
                except (OSError, asyncio.TimeoutError) as e:
                    last = e
                    continue
                reached = True
                if enc == "plain":
                    return r, w
                try:
                    return await asyncio.wait_for(mse.initiate(r, w, info_hash, plain_only=enc == "light"), 10)
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as e:
                    last = e
                    w.close()
                    break  # peer reachable but no MSE: retry in plaintext
            if not reached:
                break
        raise last

    async def _incoming(self, r, w):
        try:
            addr = tuple(w.get_extra_info("peername")[:2])
            if addr[0] in self.banned:
                raise ValueError("banned")
            if _is_v6(addr[0]) and _global_ip(addr[0]) and w.get_extra_info("socket") is not None:
                self.inbound6["tcp"] += 1     # TCP (a uTP SYN already counted as a packet)
            first = await asyncio.wait_for(r.readexactly(20), 10)
            if first == PSTR:  # plaintext BitTorrent handshake
                if self.encryption == "require":
                    raise ValueError("plaintext refused")
                hs = first + await asyncio.wait_for(r.readexactly(48), 10)
            else:  # anything else must be an MSE key exchange
                if self.encryption == "off":
                    raise ValueError("encryption disabled")
                skeys = [x.t.info_hash for x in self.sessions.values()
                         if isinstance(x, Session) and x.state in ("Downloading", "Finished", "Seeding")]
                r, w, _ = await asyncio.wait_for(
                    mse.respond(r, w, first, skeys, allow_plain=self.encryption != "require",
                                prefer_plain=self.encryption == "light"), 15)
                hs = await asyncio.wait_for(r.readexactly(68), 10)
            s = self.sessions.get(hs[28:48].hex())
            if (hs[:20] != PSTR or not s or s.state not in ("Downloading", "Finished", "Seeding")
                    or len(s.peers) >= MAX_PEERS):
                raise ValueError("unknown torrent")
        except Exception:
            w.close()
            return
        await Peer(s, r, w, addr, True).run(hs)

    def recheck(self, h):
        """Throw away what we know about the files and verify every piece again."""
        s = self.sessions.get(h)
        if not isinstance(s, Session):
            return

        async def go():
            was_running = not s.stopped
            await s.stop(announce=False)
            s.checked, s.resume = False, None
            s.have = [False] * len(s.t.hashes)
            s.left = s.t.length
            s.compute_want()
            s.state = "Queued" if not s.user_paused else "Paused"
            self._schedule()
        self._run(go())

    def add(self, torrent_bytes, save_dir, start=True, skip=None, resume=None, high=None, sequential=False):
        """Returns (session, is_new). Raises ValueError for bad torrents. `skip`: indices of files not to download."""
        try:
            t = Torrent(torrent_bytes)
        except (KeyError, TypeError, IndexError) as e:
            raise ValueError("invalid .torrent file (%s)" % e)
        if t.hex in self.sessions:
            return self.sessions[t.hex], False
        s = Session(self, t, save_dir)
        if skip:
            s.skip = {int(i) for i in skip if 0 <= int(i) < len(t.files)}
            if len(s.skip) >= len(t.files):
                raise ValueError("select at least one file")
            s.compute_want()
        if high:
            s.high = {int(i) for i in high if 0 <= int(i) < len(t.files)}
            s.compute_want()
        s.sequential = bool(sequential)
        s.order = self._next_order()
        s.user_paused = not start
        s.resume = resume
        self.sessions[t.hex] = s
        if start:
            self._kick()
        else:
            s.state = "Paused"
        return s, True

    def add_magnet(self, uri, save_dir, start=True):
        """Returns (session, is_new). Raises ValueError for bad links."""
        ms = MagnetSession(self, uri, save_dir)
        if ms.hex in self.sessions:
            return self.sessions[ms.hex], False
        ms.order = self._next_order()
        ms.user_paused = not start
        ms.category, ms.priority = "", 0
        self.sessions[ms.hex] = ms
        if start:
            asyncio.run_coroutine_threadsafe(ms.start(), self.loop)
        else:
            ms.state = "Paused"
        return ms, True

    async def _finish_magnet(self, ms, raw):
        await ms.stop()
        parts = [b"d"]
        if ms.t.trackers:
            parts += [b"8:announce", bencode(ms.t.trackers[0]),
                      b"13:announce-list", bencode([[t] for t in ms.t.trackers])]
        parts += [b"4:info", raw, b"e"]
        data = b"".join(parts)
        try:
            t = Torrent(data)
        except Exception as e:
            ms.error, ms.state = "bad metadata: %s" % e, "Error"
            return
        if self.sessions.get(ms.hex) is not ms:
            return  # removed while downloading metadata
        s = Session(self, t, ms.save_dir)
        s.order, s.user_paused = ms.order, ms.user_paused
        s.category, s.priority = getattr(ms, "category", ""), getattr(ms, "priority", 0)
        self.sessions[ms.hex] = s
        if self.on_metadata:
            try:
                self.on_metadata(ms.hex, data)
            except Exception:
                pass
        self._schedule()

    def pause(self, h):
        s = self.sessions.get(h)
        if s:
            s.user_paused = True
            self._run(s.stop(announce=True))
            self._kick()

    def resume(self, h):
        s = self.sessions.get(h)
        if not s:
            return
        s.user_paused = False
        if isinstance(s, Session):
            s.error = None
            if s.complete and s.rule_hit:
                s.rule_ignore = True    # the user chose to keep seeding past the rule
            if s.stopped:
                s.state = "Queued"
            self._kick()
        elif s.stopped:
            asyncio.run_coroutine_threadsafe(s.start(), self.loop)

    def remove(self, h, delete_files=False):
        s = self.sessions.pop(h, None)
        if not s:
            return
        self._run(s.stop(announce=True))
        self._kick()
        if delete_files and hasattr(s, "storage"):
            for p in s.storage.paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            root = os.path.join(s.save_dir, s.t.name)
            if s.t.multi:
                for dp, _, _ in sorted(os.walk(root), key=lambda x: -len(x[0])):
                    try:
                        os.rmdir(dp)
                    except OSError:
                        pass

    def add_peer(self, h, ip, port):
        s = self.sessions[h]
        self.loop.call_soon_threadsafe(s.candidates.add, (ip, port))

    def snapshot(self):
        out = []
        queued = sorted((x for x in self.sessions.values() if x.state == "Queued"), key=lambda x: x.order)
        for h, s in list(self.sessions.items()):
            if isinstance(s, MagnetSession):
                out.append(dict(hash=h, name=s.name, size=0, progress=s.progress,
                                state=s.state, raw_state=s.state, down=0.0, up=0.0,
                                peers=len(s.peers), eta=None, save_dir=s.save_dir,
                                error=s.error, downloaded=0, uploaded=0, magnet=s.uri, order=s.order,
                                user_paused=s.user_paused, category=getattr(s, "category", ""),
                                priority=getattr(s, "priority", 0), ratio=0.0, seed_secs=0.0,
                                limit_down=0, limit_up=0, sequential=False))
                continue
            state = s.state
            if state == "Checking":
                state = "Checking %d%%" % (100 * s.check_done // len(s.t.hashes))
            eta = None
            if s.state == "Queued" and s in queued:
                state = "Queued (%d)" % (queued.index(s) + 1)
            if s.state == "Downloading" and s.down_rate > 1024:
                eta = s.want_left / s.down_rate
            out.append(dict(hash=h, name=s.t.name, size=s.selected_size, total=s.t.length, order=s.order,
                            user_paused=s.user_paused, files=len(s.t.files), skipped=len(s.skip),
                            progress=s.progress,
                            state=state, raw_state=s.state, down=s.down_rate, up=s.up_rate,
                            peers=len(s.peers), eta=eta, save_dir=s.save_dir,
                            error=s.error, downloaded=s.downloaded, uploaded=s.uploaded,
                            category=s.category, priority=s.priority, ratio=s.share_ratio, seed_secs=s.seed_secs,
                            limit_down=s.down_lim.rate // 1024, limit_up=s.up_lim.rate // 1024,
                            sequential=s.sequential))
        return out

    def shutdown(self):
        self._closing = True
        if self.portmap:
            self.portmap.stop()  # removes the router mappings
            self.portmap = None
        def _close_udp():
            for d in self.dhts:
                d.close()
            for mux in self.udp.values():
                mux.utp.close()
                mux.transport.close()
            for srv in self.servers:
                srv.close()
            for tk in asyncio.all_tasks(self.loop):  # let nothing linger past shutdown
                if tk is not asyncio.current_task():
                    tk.cancel()

        self.loop.call_soon_threadsafe(_close_udp)

        async def _all():
            await asyncio.gather(*(s.stop(announce=True) for s in self.sessions.values()
                                   if not s.stopped), return_exceptions=True)
        try:
            self._run(_all(), 10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.call_later, 0.2, self.loop.stop)  # after cancellations run
