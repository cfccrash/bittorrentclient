"""Loopback self-test: seed -> download -> verify -> corrupt -> re-check."""
import hashlib, os, random, shutil, tempfile, time
import queue, base64, threading
from torrent_core import Manager, make_torrent, bdecode, bencode, Torrent, parse_magnet
import single_instance


def tree_hash(root):
    h = hashlib.sha256()
    for dp, _, names in sorted(os.walk(root)):
        for n in sorted(names):
            p = os.path.join(dp, n)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


def wait(cond, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


def main():
    tmp = tempfile.mkdtemp()
    try:
        seed_dir, leech_dir = os.path.join(tmp, "seed"), os.path.join(tmp, "leech")
        content = os.path.join(seed_dir, "Album")
        os.makedirs(os.path.join(content, "sub"))
        os.makedirs(leech_dir)
        rnd = random.Random(1)
        # odd sizes so pieces span file boundaries and the last piece is short
        for rel, size in [("a.bin", 1_234_567), ("sub/b.bin", 777_777), ("sub/empty.txt", 0),
                          ("c.bin", 100_003)]:
            with open(os.path.join(content, rel), "wb") as f:
                f.write(rnd.randbytes(size))
        data = make_torrent(content, piece_length=32768)
        t = Torrent(data)
        assert bencode(bdecode(data)) == data, "bencode roundtrip"
        print("torrent:", t.name, "pieces:", len(t.hashes), "files:", len(t.files))

        seeder, leecher = Manager(0, dht=False), Manager(0, dht=False)
        s_sess, _ = seeder.add(data, seed_dir)
        assert wait(lambda: s_sess.state == "Seeding", 20), "seeder check: " + s_sess.state
        print("seeder ready, port", seeder.port)

        l_sess, _ = leecher.add(data, leech_dir)
        assert wait(lambda: l_sess.state in ("Downloading", "Seeding"), 20)
        leecher.add_peer(t.hex, "127.0.0.1", seeder.port)
        t0 = time.time()
        assert wait(lambda: l_sess.complete, 60), "download stalled at %.1f%%" % (l_sess.progress * 100)
        print("downloaded %.1f MB in %.1fs" % (t.length / 1e6, time.time() - t0))
        assert tree_hash(content) == tree_hash(os.path.join(leech_dir, "Album")), "content mismatch"
        print("content identical: OK; seeder uploaded", s_sess.uploaded, "bytes")

        # corrupt one byte -> a fresh client must detect it on re-check
        victim = os.path.join(leech_dir, "Album", "a.bin")
        leecher.shutdown()
        with open(victim, "r+b") as f:
            f.seek(500_000)
            b = f.read(1)
            f.seek(500_000)
            f.write(bytes([b[0] ^ 0xFF]))
        m3 = Manager(0, dht=False)
        s3, _ = m3.add(data, leech_dir)
        assert wait(lambda: s3.state in ("Downloading", "Seeding"), 20)
        assert not s3.complete and s3.progress > 0.95, "corruption not detected"
        print("corruption detected on re-check: %.1f%%" % (s3.progress * 100))
        # ...and it repairs itself from the seeder
        m3.add_peer(t.hex, "127.0.0.1", seeder.port)
        assert wait(lambda: s3.complete, 30), "repair failed"
        assert tree_hash(content) == tree_hash(os.path.join(leech_dir, "Album"))
        print("repaired from seeder: OK")
        m3.shutdown()

        # ---- magnet link: metadata comes from the seeder, then the data downloads
        mag_dir = os.path.join(tmp, "magnet")
        os.makedirs(mag_dir)
        m4 = Manager(0, dht=False)
        uri = "magnet:?xt=urn:btih:%s&dn=Album&x.pe=127.0.0.1:%d" % (t.hex, seeder.port)
        got = {}
        m4.on_metadata = lambda h, d: got.update({h: d})
        ms, new = m4.add_magnet(uri, mag_dir)
        assert new and m4.snapshot()[0]["state"] == "Fetching metadata"
        assert wait(lambda: t.hex in got, 30), "metadata never arrived"
        assert Torrent(got[t.hex]).info_hash == t.info_hash
        real = m4.sessions[t.hex]
        assert wait(lambda: real.state in ("Downloading", "Seeding"), 20)
        m4.add_peer(t.hex, "127.0.0.1", seeder.port)
        assert wait(lambda: real.complete, 60), "magnet download stalled"
        assert tree_hash(content) == tree_hash(os.path.join(mag_dir, "Album")), "magnet content mismatch"
        print("magnet link -> metadata -> download: OK")
        m4.shutdown()

        # ---- choosing files: skipped files are not downloaded; the choice can be changed later
        sel_dir = os.path.join(tmp, "select")
        os.makedirs(sel_dir)
        m5 = Manager(0, dht=False)
        fin = []
        m5.on_finished = lambda h, n: fin.append((h, n))
        names = ["/".join(f[0][1:]) for f in t.files]
        ia = names.index("a.bin")
        s5, _ = m5.add(data, sel_dir, skip={ia})
        m5.add_peer(t.hex, "127.0.0.1", seeder.port)
        assert wait(lambda: s5.done, 30), "selected files never finished (%.1f%%)" % (s5.progress * 100)
        assert wait(lambda: s5.state == "Finished", 5) and not s5.complete and s5.progress == 1.0
        assert wait(lambda: len(fin) == 1, 5) and fin[0][0] == t.hex, "no finished callback"
        assert s5.downloaded < t.length * 0.6, "downloaded skipped file (%d of %d)" % (s5.downloaded, t.length)
        for rel in ("c.bin", "sub/b.bin"):
            assert open(os.path.join(sel_dir, "Album", rel), "rb").read() == open(os.path.join(content, rel), "rb").read()
        snap = m5.snapshot()[0]
        assert snap["size"] == t.length - 1_234_567 and snap["total"] == t.length and snap["skipped"] == 1
        det = m5.details(t.hex)
        assert [f[3] for f in det["files"]].count(True) == 1 and len(det["files"]) == 4
        assert det["files"][names.index("c.bin")][2] == 1.0 and det["files"][ia][2] < 1.0
        assert len(det["peers"]) == 1, det["peers"]
        pe = det["peers"][0]
        assert pe["addr"] == "127.0.0.1:%d" % seeder.port and pe["client"].startswith("Bit Torrent Client") and pe["rx"] > 0, pe
        assert det["trackers"] == [] and det["have"] > 0 and m5.details("nope") is None
        try:
            m5.set_skip(t.hex, range(len(t.files))); raise SystemExit("accepted an empty selection")
        except ValueError:
            pass
        m5.set_skip(t.hex, [])                    # now also take the skipped file
        assert wait(lambda: s5.complete, 30), "late file selection did not download"
        assert wait(lambda: s5.state == "Seeding", 5) and len(fin) == 2
        assert tree_hash(content) == tree_hash(os.path.join(sel_dir, "Album")), "content mismatch after late selection"
        m5.set_skip(t.hex, [ia])                  # deselecting something already downloaded changes nothing
        assert s5.complete and s5.state == "Seeding"
        print("file selection (skip, finished-but-not-complete, late add, callbacks): OK")
        m5.shutdown()

        # ---- download queue: one at a time, queue order, pause/resume of queued torrents
        qdata, qdirs = [], []
        for k in range(3):
            d = os.path.join(seed_dir, "Q%d" % k)
            os.makedirs(d)
            open(os.path.join(d, "f.bin"), "wb").write(rnd.randbytes(150_000))
            qdata.append(make_torrent(d, piece_length=16384))
        for raw in qdata:
            ss, _ = seeder.add(raw, seed_dir)
            assert wait(lambda: ss.state == "Seeding", 20)
        q_dir = os.path.join(tmp, "queue")
        os.makedirs(q_dir)
        m6 = Manager(0, dht=False)
        m6.set_limits(60, 0)                      # slow enough to watch the queue work
        m6.set_max_active(1)
        done_order = []
        m6.on_finished = lambda h, n: done_order.append(n)
        qs = [m6.add(raw, q_dir)[0] for raw in qdata]
        for sess in qs:
            m6.add_peer(sess.t.hex, "127.0.0.1", seeder.port)
        assert wait(lambda: sum(1 for x in qs if x.state in ("Downloading", "Checking")) == 1 and
                    [x.state for x in qs].count("Queued") == 2, 10), [x.state for x in qs]
        shown = [x["state"] for x in m6.snapshot()]
        assert "Queued (1)" in shown and "Queued (2)" in shown, shown
        m6.move({qs[2].t.hex}, "top")             # last in line jumps to the front of the queue
        m6.pause(qs[1].t.hex)
        assert qs[1].state == "Paused" and qs[1].user_paused
        m6.resume(qs[1].t.hex)
        assert wait(lambda: qs[1].state in ("Queued", "Downloading", "Checking", "Seeding"), 5)
        most = 0
        t0 = time.time()
        while not all(x.complete for x in qs) and time.time() - t0 < 90:
            most = max(most, sum(1 for x in qs if x.state in ("Downloading", "Checking")))
            for x in qs:   # no tracker here, so hand a restarted torrent its only peer again
                if x.state == "Downloading" and not x.peers and not x.candidates:
                    m6.add_peer(x.t.hex, "127.0.0.1", seeder.port)
            time.sleep(0.05)
        assert all(x.complete for x in qs), [x.state for x in qs]
        assert most == 1, "more than one torrent downloaded at once (%d)" % most
        assert done_order.index(qs[2].t.name) < done_order.index(qs[1].t.name), done_order
        m6.set_max_active(0)
        print("download queue (limit, order, move, pause/resume): OK")
        m6.shutdown()

        # ---- resume data: an unchanged download is not re-hashed at the next start; any change forces a full check
        import json as _json
        r_dir = os.path.join(tmp, "resume"); os.makedirs(r_dir)
        m7 = Manager(0, dht=False)
        s7, _ = m7.add(data, r_dir)
        m7.add_peer(t.hex, "127.0.0.1", seeder.port)
        assert wait(lambda: s7.complete, 30)
        m7.shutdown()
        rd = _json.loads(_json.dumps(s7.resume_data()))      # as the app stores it
        assert rd and len(rd["files"]) == len(t.files)
        m8 = Manager(0, dht=False)
        s8, _ = m8.add(data, r_dir, resume=rd)
        assert wait(lambda: s8.state == "Seeding", 20), s8.state
        assert s8.resumed and s8.complete and s8.check_done == 0, "resume data was not used"
        m8.shutdown()
        with open(os.path.join(r_dir, "Album", "c.bin"), "r+b") as f:     # changes the file's timestamp (and a byte)
            f.seek(50_000); b = f.read(1); f.seek(50_000); f.write(bytes([b[0] ^ 0xFF]))
        m9 = Manager(0, dht=False)
        s9, _ = m9.add(data, r_dir, resume=rd)
        assert wait(lambda: s9.state in ("Downloading", "Seeding"), 20)
        assert not s9.resumed and not s9.complete, "stale resume data was trusted"
        m9.add_peer(t.hex, "127.0.0.1", seeder.port)
        assert wait(lambda: s9.complete, 30)
        # forced re-verification
        s9.downloaded = 0
        m9.recheck(t.hex)
        assert wait(lambda: s9.state == "Seeding" and s9.check_done == len(t.hashes), 20) and not s9.resumed
        bad = dict(rd, pieces="!!!")                           # garbage resume data must be ignored, not crash
        m9.shutdown()
        m10 = Manager(0, dht=False)
        s10, _ = m10.add(data, r_dir, resume=bad)
        assert wait(lambda: s10.state == "Seeding", 20) and not s10.resumed
        m10.shutdown()
        print("resume data (fast restart, stale data rejected, forced re-check, garbage ignored): OK")

        # ---- request pipeline depth follows the peer's speed (bounded)
        from torrent_core import Peer, MAX_PIPELINE
        pp = Peer.__new__(Peer)
        pp.rx_rate, pp._rx_bytes, pp._rx_t = 0.0, 0, time.time()
        assert pp.pipeline_depth() == 10
        pp.rx_rate, pp._rx_bytes, pp._rx_t = 100e3, 0, time.time()
        assert pp.pipeline_depth() == 10
        pp.rx_rate, pp._rx_bytes, pp._rx_t = 1.5e6, 0, time.time()
        assert 40 <= pp.pipeline_depth() < MAX_PIPELINE, pp.pipeline_depth()
        pp.rx_rate, pp._rx_bytes, pp._rx_t = 50e6, 0, time.time()
        assert pp.pipeline_depth() == MAX_PIPELINE
        pp._rx_bytes, pp._rx_t = 8_000_000, time.time() - 2.0      # smoothing: a burst raises the estimate
        before = pp.rx_rate; pp.pipeline_depth(); assert pp.rx_rate != before
        print("adaptive request pipeline (10 .. %d in flight): OK" % MAX_PIPELINE)

        # ---- magnet parsing
        ih = bytes(range(20))
        b32 = base64.b32encode(ih).decode()
        h, dn, trk, pe = parse_magnet("magnet:?xt=urn:btih:%s&dn=My+File&tr=udp%%3A%%2F%%2Ft.example%%3A80%%2Fann&tr=ftp://x&x.pe=1.2.3.4:5" % b32)
        assert h == ih and dn == "My File" and trk == ["udp://t.example:80/ann"] and pe == [("1.2.3.4", 5)]
        for bad in ("http://x", "magnet:?dn=x", "magnet:?xt=urn:btih:zzz"):
            try:
                parse_magnet(bad); raise SystemExit("accepted bad magnet " + bad)
            except ValueError:
                pass
        print("magnet parsing: OK")

        # ---- single instance forwarding
        q = queue.Queue()
        assert single_instance.acquire(["first"], q.put, port=47999) is True
        assert single_instance.acquire(["magnet:?xt=urn:btih:abc", "x.torrent"], q.put, port=47999) is False
        assert q.get(timeout=3) == ["magnet:?xt=urn:btih:abc", "x.torrent"]
        single_instance.release()
        q2 = queue.Queue()
        assert single_instance.acquire(["again"], q2.put, port=47999) is True      # the port is free again after release()
        single_instance.release()
        print("single-instance forwarding and release: OK")

        # ---- `BitTorrentClient.exe --quit` (installer / uninstaller): forwarded to the running copy, never opens a window
        import functools, sys
        from unittest import mock
        import bittorrentclient as BC
        q3 = queue.Queue()
        assert single_instance.acquire(["running"], q3.put, port=47999) is True
        real_acquire, old_argv = single_instance.acquire, sys.argv
        single_instance.acquire = functools.partial(real_acquire, port=47999)
        try:
            sys.argv = ["BitTorrentClient.exe", "--quit"]
            with mock.patch.object(BC.tk, "Tk", side_effect=AssertionError("--quit must not open a window")):
                BC.main()                                        # a copy is running: hand it --quit
                assert q3.get(timeout=3) == ["--quit"]
                single_instance.release()
                BC.main()                                        # nothing running: just returns
            assert real_acquire(["again"], q3.put, port=47999) is True, "--quit must not keep the instance port"
            single_instance.release()
        finally:
            single_instance.acquire, sys.argv = real_acquire, old_argv
        print("--quit: forwarded to a running copy, no window, port left free: OK")

        # ---- DHT: six real nodes on loopback (UDP), then trackerless magnet discovery
        first = Manager(0, dht_bootstrap=[])
        boot = [("127.0.0.1", first.dht.port)]
        mgrs = [first] + [Manager(0, dht_bootstrap=boot) for _ in range(5)]
        assert wait(lambda: all(len(m.dht.nodes) >= 3 for m in mgrs), 30), \
            "DHT tables did not fill: %s" % [len(m.dht.nodes) for m in mgrs]
        print("DHT routing tables:", [len(m.dht.nodes) for m in mgrs])
        ih = bytes(range(20))
        mgrs[1]._run(mgrs[1].dht.get_peers(ih, announce_port=12345))
        found = mgrs[4]._run(mgrs[4].dht.get_peers(ih))
        assert ("127.0.0.1", 12345) in found, "announce not found: %r" % found
        print("DHT announce + lookup across nodes: OK")

        dseed, dleech = mgrs[2], mgrs[5]
        ds, _ = dseed.add(data, seed_dir)
        assert wait(lambda: ds.state == "Seeding", 20)
        assert wait(lambda: any(t.info_hash in m.dht.store for m in mgrs), 30), "seeder never announced"
        ddir = os.path.join(tmp, "dhtleech")
        os.makedirs(ddir)
        meta = {}
        dleech.on_metadata = lambda h, d: meta.update({h: d})
        dleech.add_magnet("magnet:?xt=urn:btih:%s&dn=Album" % t.hex, ddir)  # no tr=, no x.pe=
        assert wait(lambda: t.hex in meta, 60), "DHT magnet: metadata not found"
        real = dleech.sessions[t.hex]
        assert wait(lambda: real.complete, 60), "DHT magnet download stalled"
        assert tree_hash(content) == tree_hash(os.path.join(ddir, "Album")), "DHT content mismatch"
        print("trackerless magnet link found via DHT, downloaded and verified: OK")

        # private torrents must never touch the DHT
        assert Torrent(make_torrent(content, 32768)).private is False
        priv = bdecode(data); priv[b"info"][b"private"] = 1
        assert Torrent(bencode(priv)).private is True
        print("private flag parsed: OK")
        for m in mgrs:
            m.shutdown()

        # =================== encryption / uTP / PEX / limits ===================
        import asyncio, socket as _socket
        from mse import RC4
        import utp as utp_mod
        import struct
        from torrent_core import DHT, _pack_peer
        assert RC4(b"Key", drop=0).process(b"Plaintext").hex().upper() == "BBF316E8D940AF0AD3"
        print("RC4 known-answer vector: OK")

        def run_pair(seed_kw, leech_kw, timeout=40, peer_ip="127.0.0.1", seed_limit=None, leech_limit=None):
            """Seeder + leecher on loopback; returns (completed, seconds, leecher_session)."""
            sm, lm = Manager(0, dht=False, **seed_kw), Manager(0, dht=False, **leech_kw)
            try:
                if seed_limit:
                    sm.set_limits(0, seed_limit)
                if leech_limit:
                    lm.set_limits(leech_limit, 0)
                ss, _ = sm.add(data, seed_dir)
                assert wait(lambda: ss.state == "Seeding", 20)
                ldir = tempfile.mkdtemp(dir=tmp)
                ls, _ = lm.add(data, ldir)
                assert wait(lambda: ls.state in ("Downloading", "Seeding"), 20)
                t0 = time.time()
                lm.add_peer(t.hex, peer_ip, sm.port)
                ok = wait(lambda: ls.complete, timeout)
                dt = time.time() - t0
                if ok:
                    assert tree_hash(content) == tree_hash(os.path.join(ldir, "Album")), "content mismatch"
                return ok, dt, ls
            finally:
                sm.shutdown(); lm.shutdown()

        ok, _, ls = run_pair(dict(encryption="require"), dict(encryption="require"))
        assert ok and ls.stats["encrypted"] > 0 and ls.stats["tcp"] > 0, ls.stats
        print("encrypted (RC4) transfer, both sides require it: OK", ls.stats)
        ok, _, ls = run_pair(dict(encryption="require"), dict(encryption="off"), timeout=5)
        assert not ok, "plaintext peer must be refused by a 'require' seeder"
        print("'require' correctly refuses plaintext peers: OK")
        # handshake-only mode: MSE negotiated, data stays plain (no RC4 cost)
        ok, _, ls = run_pair(dict(encryption="light"), dict(encryption="light"))
        assert ok and ls.stats["encrypted"] == 0 and ls.stats["tcp"] > 0, ls.stats
        ok, _, ls = run_pair(dict(encryption="light"), dict(encryption="require"))   # peer insists on RC4: honoured
        assert ok and ls.stats["encrypted"] > 0, ls.stats
        ok, _, ls = run_pair(dict(encryption="require"), dict(encryption="light"))   # we insist, light peer must pick RC4
        assert ok and ls.stats["encrypted"] > 0, ls.stats
        print("handshake-only encryption: plain data between two such peers, RC4 when either side requires it: OK")
        ok, _, ls = run_pair(dict(encryption="off"), dict(encryption="prefer"))
        assert ok and ls.stats["encrypted"] == 0, ls.stats
        print("'prefer' falls back to plaintext when the peer has no encryption: OK")

        ok, dt, ls = run_pair(dict(utp="prefer", encryption="off"), dict(utp="prefer", encryption="off"))
        assert ok and ls.stats["utp"] > 0, ls.stats
        print("uTP transfer: OK (%.1fs)" % dt, ls.stats)
        ok, dt, ls = run_pair(dict(utp="prefer", encryption="require"), dict(utp="prefer", encryption="require"))
        assert ok and ls.stats["utp"] > 0 and ls.stats["encrypted"] > 0, ls.stats
        print("encrypted uTP transfer: OK (%.1fs)" % dt, ls.stats)

        # ---- speed limits (KiB/s): 2.1 MB at 512 KiB/s needs >= ~4 s
        ok, dt, _ = run_pair({}, {}, timeout=40, seed_limit=512)
        assert ok and dt >= 3.0, "upload limit not enforced (%.1fs)" % dt
        print("upload limit 512 KiB/s: 2.1 MB took %.1fs: OK" % dt)
        ok, dt, _ = run_pair({}, {}, timeout=40, leech_limit=512)
        assert ok and dt >= 3.0, "download limit not enforced (%.1fs)" % dt
        print("download limit 512 KiB/s: 2.1 MB took %.1fs: OK" % dt)

        # ---- peer exchange: B only knows A, yet learns about the seeder S through ut_pex
        S, A, B = (Manager(0, dht=False, encryption="off") for _ in range(3))
        try:
            for m in (S, A, B):
                m.pex_interval = 1
            S.set_limits(0, 600)   # slow seeder so the PEX round happens mid-transfer
            ss, _ = S.add(data, seed_dir)
            assert wait(lambda: ss.state == "Seeding", 20)
            adir, bdir = tempfile.mkdtemp(dir=tmp), tempfile.mkdtemp(dir=tmp)
            asess, _ = A.add(data, adir)
            bsess, _ = B.add(data, bdir)
            assert wait(lambda: asess.state == "Downloading" and bsess.state == "Downloading", 20)
            A.add_peer(t.hex, "127.0.0.1", S.port)
            B.add_peer(t.hex, "127.0.0.1", A.port)   # B is never told about S directly
            assert wait(lambda: bsess.complete and asess.complete, 60), "PEX swarm stalled"
            assert bsess.stats["pex"] > 0, "B never learned a peer through PEX"
            assert tree_hash(content) == tree_hash(os.path.join(bdir, "Album"))
            print("peer exchange (ut_pex): B discovered %d peer(s) via A: OK" % bsess.stats["pex"])
        finally:
            for m in (S, A, B):
                m.shutdown()

        # ---- uTP over a lossy, reordering, duplicating virtual network
        async def utp_lossy(loss, jitter, size):
            loop = asyncio.get_running_loop()
            rnd = random.Random(7)
            socks = {}
            def make(name, other, accept):
                def send(buf, addr):
                    if rnd.random() < loss:
                        return
                    for _ in range(2 if rnd.random() < 0.02 else 1):
                        loop.call_later(0.005 + rnd.random() * jitter,
                                        lambda b=buf: socks[other].datagram_received(b, (name, 1)))
                socks[name] = utp_mod.UTPSocket(send, accept)
                socks[name].start()
                return socks[name]
            async def serve(r, w):
                got = await r.readexactly(size)
                w.write(hashlib.sha256(got).digest()); await w.drain(); w.close()
            a = make("A", "B", lambda r, w: None)
            b = make("B", "A", lambda r, w: loop.create_task(serve(r, w)))
            r, w = await a.connect(("B", 1), timeout=10)
            blob = os.urandom(size); w.write(blob); await w.drain()
            assert await asyncio.wait_for(r.readexactly(32), 60) == hashlib.sha256(blob).digest()
            w.close(); a.close(); b.close()
        for loss, jitter, size in [(0.0, 0.0, 1_000_000), (0.05, 0.02, 250_000)]:
            asyncio.run(utp_lossy(loss, jitter, size))
        print("uTP over lossy network (5% loss, reordering, duplicates): OK")

        # ---- automatic port forwarding against fake routers on loopback (PCP, NAT-PMP, UPnP)
        import http.server, threading as _th, urllib.parse
        import portmap as portmap_mod, upnp as upnp_mod
        import xml.etree.ElementTree as ET
        import struct
        ltag = lambda t: t.rsplit("}", 1)[-1]
        PUBLIC = "93.184.216.34"

        class FakeIGD:
            """UPnP router: SSDP responder + description + SOAP control (WANIPConnection and, optionally,
            WANIPv6FirewallControl)."""
            def __init__(self, reject_lease=False, conflict=False, busy_ext=(), fw6=True, pinhole_error=None):
                self.reject_lease, self.conflict, self.busy_ext = reject_lease, conflict, set(busy_ext)
                self.fw6, self.pinhole_error = fw6, pinhole_error
                self.added, self.deleted = [], []            # (proto, external port, internal port, lease) / (proto, ext)
                self.pinholes, self.pinholes_deleted, self.pinhole_updates = {}, [], []
                self.uid = 100
                igd = self
                class H(http.server.BaseHTTPRequestHandler):
                    def log_message(self, *a): pass
                    def do_GET(self):
                        fw = ('<service><serviceType>urn:schemas-upnp-org:service:WANIPv6FirewallControl:1</serviceType>'
                              '<controlURL>/fw6</controlURL></service>') if igd.fw6 else ""
                        body = ("""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">
<URLBase>http://127.0.0.1:%d/</URLBase><device><deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:2</deviceType>
<deviceList><device><deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType><deviceList><device>
<deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType><serviceList><service>
<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType><controlURL>/ctl</controlURL>
</service>%s</serviceList></device></deviceList></device></deviceList></device></root>""" % (igd.http.server_port, fw)).encode()
                        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                        self.wfile.write(body)
                    def do_POST(self):
                        raw = self.rfile.read(int(self.headers["Content-Length"]))
                        action = self.headers["SOAPAction"].strip('"').split("#")[1]
                        el = next(e for e in ET.fromstring(raw).iter() if ltag(e.tag) == action)
                        a = {ltag(c.tag): c.text or "" for c in el}
                        err, extra = None, ""
                        if action == "AddPortMapping":
                            ext = int(a["NewExternalPort"])
                            if igd.reject_lease and a["NewLeaseDuration"] != "0":
                                err = (725, "OnlyPermanentLeasesSupported")
                            elif igd.conflict or ext in igd.busy_ext:
                                err = (718, "ConflictInMappingEntry")
                            else:
                                igd.added.append((a["NewProtocol"], ext, int(a["NewInternalPort"]), a["NewLeaseDuration"]))
                        elif action == "DeletePortMapping":
                            igd.deleted.append((a["NewProtocol"], int(a["NewExternalPort"])))
                        elif action == "GetExternalIPAddress":
                            extra = "<NewExternalIPAddress>%s</NewExternalIPAddress>" % PUBLIC
                        elif action == "AddPinhole":
                            if igd.pinhole_error:
                                err = igd.pinhole_error
                            else:
                                igd.uid += 1
                                igd.pinholes[str(igd.uid)] = (("TCP" if a["Protocol"] == "6" else "UDP"), int(a["InternalPort"]),
                                                              a["InternalClient"], int(a["LeaseTime"]))
                                extra = "<UniqueID>%d</UniqueID>" % igd.uid
                        elif action == "UpdatePinhole":
                            igd.pinhole_updates.append(a["UniqueID"])
                        elif action == "DeletePinhole":
                            igd.pinholes_deleted.append(a["UniqueID"])
                        if err:
                            body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
                                    '<faultcode>s:Client</faultcode><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                                    '<errorCode>%d</errorCode><errorDescription>%s</errorDescription></UPnPError></detail>'
                                    '</s:Fault></s:Body></s:Envelope>' % err).encode()
                            self.send_response(500)
                        else:
                            body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                                    '<u:%sResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">%s</u:%sResponse>'
                                    '</s:Body></s:Envelope>' % (action, extra, action)).encode()
                            self.send_response(200)
                        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
                _th.Thread(target=self.http.serve_forever, daemon=True).start()
                self.udp = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                self.udp.bind(("127.0.0.1", 0))
                self.ssdp_addr = self.udp.getsockname()
                _th.Thread(target=self._ssdp, daemon=True).start()
            def _ssdp(self):
                while True:
                    try:
                        data, addr = self.udp.recvfrom(2048)
                    except OSError:
                        return
                    if b"M-SEARCH" in data:
                        self.udp.sendto(("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=120\r\nLOCATION: http://127.0.0.1:%d/desc.xml\r\n"
                                         "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:2\r\n\r\n" % self.http.server_port).encode(), addr)
            def close(self):
                self.http.shutdown(); self.udp.close()

        quiet = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM); quiet.bind(("127.0.0.1", 0))  # never answers
        QUIET = quiet.getsockname()

        class FakePMP:
            """One UDP port speaking both NAT-PMP (version 0) and PCP (version 2). `busy[proto]` lists external
            ports that are already taken; the router then assigns the next free one."""
            def __init__(self, pcp=True, natpmp=True, refuse=False, busy=None, code=0):
                self.pcp, self.natpmp, self.refuse, self.code = pcp, natpmp, refuse, code
                self.ext = _socket.inet_aton(PUBLIC)
                self.t0 = time.time()
                self.busy = {"TCP": set(), "UDP": set()}
                for k, v in (busy or {}).items():
                    self.busy[k] |= set(v)
                self.log = []        # (protocol name, "TCP"/"UDP", internal port, requested lifetime)
                self.assigned = {}   # (proto, internal port) -> external port handed out
                self.sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                self.sock.bind(("127.0.0.1", 0)); self.port = self.sock.getsockname()[1]
                _th.Thread(target=self._loop, daemon=True).start()
            def epoch(self):
                return int(time.time() - self.t0)     # router uptime, like a real gateway
            def reboot(self):
                self.assigned.clear(); self.t0 = time.time()   # forgets every mapping
            def announce(self, addr):
                """Unsolicited NAT-PMP 'external address' packet, as a router sends after a reboot / IP change."""
                self.sock.sendto(struct.pack(">BBHI", 0, 128, 0, self.epoch()) + self.ext, addr)
            def _loop(self):
                while True:
                    try:
                        d, addr = self.sock.recvfrom(2048)
                    except OSError:
                        return
                    rep = self._handle(d)
                    if rep:
                        self.sock.sendto(rep, addr)
            def _assign(self, proto, iport, suggested, life):
                if not life:
                    return self.assigned.pop((proto, iport), suggested)
                ext = suggested or iport
                while ext in self.busy[proto]:
                    ext += 1
                self.assigned[(proto, iport)] = ext
                return ext
            def _handle(self, d):
                if d[0] == 2:                                  # PCP
                    if not self.pcp:
                        return struct.pack(">BBHI", 0, 128, 1, 0)   # "unsupported version"
                    life = struct.unpack(">I", d[4:8])[0]
                    if d[1] & 0x7F == 0:                           # ANNOUNCE
                        return bytes([2, 0x80, 0, 0]) + struct.pack(">II", 0, self.epoch()) + bytes(12)
                    nonce, ptype = d[24:36], d[36]
                    proto = {6: "TCP", 17: "UDP"}[ptype]
                    iport, want = struct.unpack(">HH", d[40:44])
                    self.log.append(("PCP", proto, iport, life))
                    ext = self._assign(proto, iport, want, life)
                    hdr = bytes([2, 0x81, 0, 2 if self.refuse else self.code]) + struct.pack(">II", min(life, 7200), self.epoch()) + bytes(12)
                    return hdr + nonce + struct.pack(">B3xHH", ptype, iport, ext) + bytes(10) + b"\xff\xff" + self.ext
                if not self.natpmp:                            # NAT-PMP
                    return struct.pack(">BBHI", 0, 128 + d[1], 1, 0)
                if d[1] == 0:
                    return struct.pack(">BBHI", 0, 128, 0, self.epoch()) + self.ext
                iport, want, life = struct.unpack(">HHI", d[4:12])
                proto = "UDP" if d[1] == 1 else "TCP"
                self.log.append(("NAT-PMP", proto, iport, life))
                ext = self._assign(proto, iport, want, life)
                return struct.pack(">BBHIHHI", 0, 128 + d[1], 2 if self.refuse else self.code, self.epoch(), iport, ext, life)
            def adds(self):
                return sorted((p, port) for _, p, port, life in self.log if life > 0)
            def deletes(self):
                return sorted((p, port) for _, p, port, life in self.log if life == 0)
            def close(self):
                self.sock.close()

        def mapper(**kw):
            kw.setdefault("ssdp_addr", QUIET); kw.setdefault("gateways", [])
            kw.setdefault("announce_listen", False)   # do not bind the real port 5350 in tests
            return portmap_mod.PortMapper(kw.pop("ports", [("TCP", 6881), ("UDP", 6881)]), **kw)

        # -- default-gateway discovery parsers (Linux /proc, Windows `route print`, macOS `route get`)
        assert portmap_mod._parse_proc_route(
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
            "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
            "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n") == [(100, "192.168.1.1")]
        assert portmap_mod._parse_win_route(
            "Network Destination        Netmask          Gateway       Interface  Metric\n"
            "          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.50     25\n"
            "          0.0.0.0          0.0.0.0         On-link        10.0.0.5    281\n") == [(25, "192.168.1.1")]
        assert portmap_mod._parse_bsd_route("   route to: default\n    gateway: 192.168.0.1\n  interface: en0\n") == [(0, "192.168.0.1")]
        print("default gateway parsing (Linux / Windows / macOS): OK")

        # -- PCP is preferred when the router supports it
        pm = FakePMP()
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port); u.start()
        assert wait(lambda: len(u.mapped) == 2, 20), u.status
        assert u.protocol == "PCP" and "via PCP" in u.status and "public IP " + PUBLIC in u.status, u.status
        assert pm.adds() == [("TCP", 6881), ("UDP", 6881)] and all(l == 7200 for *_, l in pm.log if l)
        assert all(k == "PCP" for k, *_ in pm.log), "NAT-PMP must not be used when PCP works"
        u.stop()
        assert pm.deletes() == [("TCP", 6881), ("UDP", 6881)], "PCP mappings not removed on stop"
        pm.close()
        print("PCP: detected, mapped TCP+UDP, external IP, removed on exit: OK")

        # -- router that only speaks NAT-PMP: PCP is probed first, then we fall back
        pm = FakePMP(pcp=False)
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port); u.start()
        assert wait(lambda: len(u.mapped) == 2, 20), u.status
        assert u.protocol == "NAT-PMP" and "via NAT-PMP" in u.status, u.status
        u.stop()
        assert pm.deletes() == [("TCP", 6881), ("UDP", 6881)]
        pm.close()
        print("NAT-PMP: fallback from PCP, mapped, removed on exit: OK")

        # -- the router hands out a different external port: accept it, and move UDP to the same number
        for kw, label in ((dict(), "PCP"), (dict(pcp=False), "NAT-PMP")):
            pm = FakePMP(busy={"TCP": {6881}}, **kw)
            u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port); u.start()
            assert wait(lambda: len(u.mapped) == 2, 20), u.status
            assert u.ext == {("TCP", 6881): 6882, ("UDP", 6881): 6882}, u.ext
            assert pm.assigned == {("TCP", 6881): 6882, ("UDP", 6881): 6882}
            assert "TCP 6882 (local 6881)" in u.status and "differs" not in u.status, u.status
            assert u.external_port("TCP", 6881) == 6882
            u.stop()
            assert pm.deletes() == [("TCP", 6881), ("UDP", 6881)]
            pm.close()
            print("%s: external port 6881 busy -> router gave 6882 for TCP and UDP, accepted and cleaned up: OK" % label)
        pm = FakePMP(busy={"TCP": {6881}, "UDP": {6882}})   # UDP cannot follow TCP's number
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port); u.start()
        assert wait(lambda: len(u.mapped) == 2, 20), u.status
        assert u.ext == {("TCP", 6881): 6882, ("UDP", 6881): 6883} and "UDP external port differs" in u.status, (u.ext, u.status)
        u.stop(); pm.close()
        print("TCP/UDP external ports that cannot be aligned are reported: OK")

        pm = FakePMP(refuse=True)
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port, retry=60); u.start()
        assert wait(lambda: "refused" in u.status and "error 2" in u.status, 20), u.status
        assert not u.mapped
        u.stop(); pm.close()
        print("PCP refusal reported clearly: OK")

        # -- neither PCP nor NAT-PMP answers: falls through to UPnP
        igd = FakeIGD()
        u = mapper(gateways=["127.0.0.1"], pmp_port=QUIET[1], ssdp_addr=igd.ssdp_addr); u.start()
        assert wait(lambda: len(u.mapped) == 2, 30), u.status
        assert u.protocol == "UPnP" and "via UPnP" in u.status and "public IP " + PUBLIC in u.status, u.status
        assert sorted(igd.added) == [("TCP", 6881, 6881, "3600"), ("UDP", 6881, 6881, "3600")], igd.added
        u.stop()
        assert sorted(igd.deleted) == [("TCP", 6881), ("UDP", 6881)], "UPnP mappings not removed on stop"
        igd.close()
        print("UPnP: reached after PCP and NAT-PMP time out; mapped and removed: OK")

        igd = FakeIGD(busy_ext={6881})     # external port taken by another device (error 718): pick the next one
        u = mapper(protocols=("upnp",), ssdp_addr=igd.ssdp_addr); u.start()
        assert wait(lambda: len(u.mapped) == 2, 20), u.status
        assert u.ext == {("TCP", 6881): 6882, ("UDP", 6881): 6882} and sorted(igd.added) == [("TCP", 6882, 6881, "3600"), ("UDP", 6882, 6881, "3600")]
        u.stop()
        assert sorted(igd.deleted) == [("TCP", 6882), ("UDP", 6882)], "must delete the EXTERNAL port it created"
        igd.close()
        print("UPnP: port taken -> next free external port used, deleted by that port on exit: OK")

        igd = FakeIGD(reject_lease=True)   # router that only supports permanent mappings (error 725)
        u = mapper(ports=[("TCP", 7000)], protocols=("upnp",), ssdp_addr=igd.ssdp_addr); u.start()
        assert wait(lambda: u.mapped == [("TCP", 7000)], 20), u.status
        assert igd.added == [("TCP", 7000, 7000, "0")]
        u.stop(); igd.close()
        print("UPnP: falls back to a permanent mapping when the router rejects leases: OK")

        igd = FakeIGD(conflict=True)       # every external port conflicts: give up with a clear message
        u = mapper(ports=[("TCP", 7001)], protocols=("upnp",), ssdp_addr=igd.ssdp_addr, retry=60); u.start()
        assert wait(lambda: "refused" in u.status and "718" in u.status, 20), u.status
        assert not u.mapped
        u.stop(); igd.close()
        print("UPnP: no usable external port is reported clearly: OK")

        u = mapper(ports=[("TCP", 7002)], pmp_port=QUIET[1], retry=60); u.start()
        assert wait(lambda: u.status.startswith("no PCP / NAT-PMP / UPnP router"), 20), u.status
        u.stop()
        print("no router speaking any protocol: handled gracefully: OK")

        # -- UDP tracker (IPv4) end to end: announce carries the port; its peer list is used
        trk_log = []
        trk = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM); trk.bind(("127.0.0.1", 0))
        def trk_loop():
            while True:
                try:
                    d, addr = trk.recvfrom(2048)
                except OSError:
                    return
                if len(d) == 16:
                    trk.sendto(struct.pack(">IIQ", 0, struct.unpack(">I", d[12:16])[0], 777), addr)
                elif len(d) >= 98:
                    trk_log.append(struct.unpack(">H", d[96:98])[0])
                    trk.sendto(struct.pack(">IIIII", 1, struct.unpack(">I", d[12:16])[0], 1800, 0, 1)
                               + _socket.inet_aton("127.0.0.1") + struct.pack(">H", seeder.port), addr)
        _th.Thread(target=trk_loop, daemon=True).start()
        utdata = make_torrent(content, 32768, announce="udp://127.0.0.1:%d/announce" % trk.getsockname()[1])
        assert Torrent(utdata).info_hash == t.info_hash
        ul = Manager(0, dht=False)
        udir = tempfile.mkdtemp(dir=tmp)
        us, _ = ul.add(utdata, udir)
        assert wait(lambda: us.complete, 60), "download via UDP-tracker peer list stalled"
        assert trk_log and trk_log[0] == ul.port, (trk_log, ul.port)
        assert tree_hash(content) == tree_hash(os.path.join(udir, "Album"))
        ul.shutdown(); trk.close()
        print("UDP tracker: announced our port, peer list used, download verified: OK")

        # =================== NAT-PMP robustness ===================
        from portmap import _Epoch
        e = _Epoch()
        assert not e.check(100, now=1000.0) and not e.check(160, now=1060.0)        # normal uptime growth
        assert e.check(5, now=1120.0)                                                 # went backwards: rebooted
        e = _Epoch(); e.check(100, now=1000.0)
        assert e.check(400, now=1060.0)                                               # jumped ahead: state lost
        e = _Epoch(); e.check(100, now=1000.0)
        assert not e.check(161, now=1060.0)                                           # small clock skew tolerated
        print("router uptime (epoch) reboot rules from RFC 6886: OK")

        def free_udp_port():
            sk = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM); sk.bind(("127.0.0.1", 0))
            pt = sk.getsockname()[1]; sk.close(); return pt

        # -- NAT-PMP router reboots and multicasts its announcement: mappings come back immediately
        ap = free_udp_port()
        pm = FakePMP(pcp=False)
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port, tick=30, announce_listen=True,
                   announce_addr=("127.0.0.1", ap), announce_multicast=False); u.start()
        assert wait(lambda: len(u.mapped) == 2, 20), u.status
        time.sleep(0.5)
        pm.reboot()
        assert not pm.assigned, "reboot should have wiped the router's mappings"
        pm.announce(("127.0.0.1", ap))
        assert wait(lambda: len(pm.assigned) == 2, 10), "mappings were not restored after the router's announcement"
        u.stop(); pm.close()
        print("NAT-PMP: router reboot announcement -> mappings re-created at once: OK")

        # -- ...and with no announcement at all, the periodic uptime check notices the reboot
        pm = FakePMP(pcp=False)
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port, tick=0.3, poll_interval=1); u.start()
        assert wait(lambda: len(u.mapped) == 2, 20), u.status
        time.sleep(2.5)                    # let the router's uptime grow so a reset is unmistakable
        pm.reboot()
        assert wait(lambda: len(pm.assigned) == 2, 15), "silent router reboot was not detected"
        u.stop(); pm.close()
        print("NAT-PMP: silent router reboot detected by the uptime check and repaired: OK")

        # -- the ISP renumbers the router: external address change is noticed and reported
        changes = []
        pm = FakePMP(pcp=False)
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port, tick=0.3, poll_interval=1,
                   on_change=lambda: changes.append(1)); u.start()
        assert wait(lambda: len(u.mapped) == 2 and u.external_ip == PUBLIC, 20), u.status
        before = len(changes)
        pm.ext = _socket.inet_aton("198.51.100.9")
        assert wait(lambda: u.external_ip == "198.51.100.9", 15), "external address change not noticed"
        assert wait(lambda: len(changes) > before, 5), "on_change not fired"
        u.stop(); pm.close()
        print("NAT-PMP: external address change noticed and on_change fired: OK")

        # -- router says 'network failure' (WAN link not up yet): retry in a minute, not in five
        pm = FakePMP(pcp=False, code=3)
        u = mapper(gateways=["127.0.0.1"], pmp_port=pm.port, retry=300); u.start()
        assert wait(lambda: "NAT-PMP error 3" in u.status, 20), u.status
        assert wait(lambda: u.last_wait == 60, 5), u.last_wait
        u.stop(); pm.close()
        print("NAT-PMP: temporary router errors retried after 60 s: OK")

        # -- Manager: router-assigned ports are what we advertise (HTTP tracker + extension handshake)
        hits = []
        class TH(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                hits.append(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
                body = bencode({"interval": 1800, "peers": b""})
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        htrk = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TH)
        _th.Thread(target=htrk.serve_forever, daemon=True).start()
        pm = FakePMP()
        um = Manager(0, dht=False)
        pm.busy["TCP"].add(um.port)      # the router will not give us our own port number
        um.portmap_opts = dict(gateways=["127.0.0.1"], pmp_port=pm.port, ssdp_addr=QUIET)
        um.set_portmap(True)
        assert wait(lambda: um.portmap and len(um.portmap.mapped) == 2, 20), um.portmap.status
        assert um.public_port == um.port + 1, (um.public_port, um.port)
        assert um.public_udp_port == pm.assigned[("UDP", um.udp_port)] == um.public_port, "UDP should follow TCP"
        hdata = make_torrent(content, 32768, announce="http://127.0.0.1:%d/announce" % htrk.server_port)
        um.add(hdata, seed_dir)
        assert wait(lambda: hits, 30), "tracker never contacted"
        assert hits[0]["port"] == [str(um.public_port)], hits[0]
        n = len(hits)
        um._reannounce()                    # what the port mapper triggers on any change
        assert wait(lambda: len(hits) > n, 10), "no immediate re-announce after a change"
        expected = pm.adds()
        um.shutdown()
        assert pm.deletes() == expected, "Manager.shutdown() must unmap ports"
        pm.close(); quiet.close(); htrk.shutdown()
        print("Manager advertises the router-assigned port (%d, local %d) to trackers: OK" % (hits[0]["port"] and int(hits[0]["port"][0]), expected and um.port))

        # -- router-discovery hardening: tolerant XML, gateway guessing, diagnostics, rescan
        import http.server, threading as _th
        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"\xef\xbb\xbf \n<root>a\x01b</root>"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *a): pass
        _srv = http.server.HTTPServer(("127.0.0.1", 0), _H); _th.Thread(target=_srv.serve_forever, daemon=True).start()
        assert upnp_mod._fetch_xml("http://127.0.0.1:%d/x" % _srv.server_port).tag == "root"
        _srv.shutdown(); _srv.server_close()
        from unittest import mock
        with mock.patch.object(upnp_mod, "local_ipv4s", return_value=["192.168.7.55", "10.1.2.1", "192.168.7.56"]):
            g = portmap_mod.guess_gateways()
            assert g == ["192.168.7.1"], g   # 10.1.2.1 is our own address, so it cannot be the gateway
        dm = mapper(protocols=["upnp"])
        d = dm.diagnostics()
        assert "Status:" in d and "Log:" in d and "UPNP" in d
        dm.rescan()
        assert dm.backend is None and any("re-scan" in l for l in dm.log)
        print("router discovery hardening (tolerant XML, gateway guess, diagnostics, rescan): OK")

        seeder.shutdown()
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
