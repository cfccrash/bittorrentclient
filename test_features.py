"""Engine tests for the 1.2 features (no GUI): torrent creation, seeding rules, priority, per-torrent limits,
move-on-completion."""
import os, queue, shutil, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from torrent_core import Manager, Torrent, bdecode, create_torrent, auto_piece_length


def wait(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def main():
    tmp = tempfile.mkdtemp()
    content = os.path.join(tmp, "Album")
    os.makedirs(os.path.join(content, "sub"))
    for name, size in (("a.bin", 300_000), ("b.bin", 70_001), (os.path.join("sub", "c.bin"), 1)):
        with open(os.path.join(content, name), "wb") as f:
            f.write(os.urandom(size))

    # ---- creating a torrent: folder and single file, trackers, private flag, progress, cancel
    seen = []
    raw = create_torrent(content, trackers=["udp://t1/announce", "http://t2/announce"], piece_length=65536,
                         private=True, comment="hi", progress=seen.append)
    t = Torrent(raw)
    assert t.name == "Album" and len(t.files) == 3 and t.length == 370_002 and t.private
    assert t.trackers == ["udp://t1/announce", "http://t2/announce"] and seen and seen[-1] == 1.0
    meta = bdecode(raw)
    assert meta[b"comment"] == b"hi" and meta[b"created by"] and meta[b"info"][b"piece length"] == 65536
    single = create_torrent(os.path.join(content, "a.bin"))
    ts = Torrent(single)
    assert ts.name == "a.bin" and not ts.multi and ts.length == 300_000 and not ts.trackers and not ts.private
    assert auto_piece_length(100 << 20) == 256 * 1024 and auto_piece_length(100 << 30) == 16 << 20
    try:
        create_torrent(content, cancel=lambda: True); raise SystemExit("cancel ignored")
    except InterruptedError:
        pass
    open(os.path.join(tmp, "empty"), "wb").close()
    try:
        create_torrent(os.path.join(tmp, "empty")); raise SystemExit("empty file accepted")
    except ValueError:
        pass
    print("create torrent (folder / file / trackers / private / progress / cancel / empty): OK")

    import geo
    assert geo.country("81.2.69.142") == "GB" and geo.country("8.8.8.8") == "US" and geo.country("200.160.2.3") == "BR"
    assert geo.country("192.168.0.9") is None and geo.country("127.0.0.1") is None and geo.country("2001:db8::1") is None
    assert geo.country("junk") is None and geo.name("GB") == "United Kingdom" and geo.name(None) == ""
    import flags_data, zlib, base64
    assert flags_data.FLAGS["GB"] and zlib.decompress(base64.b64decode(flags_data.FLAGS["GB"][0]))[:4] == b"\x89PNG"
    print("peer country lookup (offline IPv4 -> country, flags, private ranges ignored): OK")

    mgr = Manager(0, dht=False)
    try:
        # the created torrent verifies against the real files -> seeding
        s, _ = mgr.add(raw, tmp)
        h = s.t.hex
        assert wait(lambda: s.state == "Seeding"), s.state
        print("created torrent verifies against its source files and seeds: OK")

        # ---- seeding rules
        hits = queue.Queue()
        mgr.on_seed_limit = lambda hh, action: hits.put((hh, action))
        mgr.set_seed_rules(ratio=1.0, minutes=0, action="pause")
        s.uploaded = s.t.length // 2
        time.sleep(2.5)
        assert hits.empty(), "ratio 0.5 must not trigger a limit of 1.0"
        s.uploaded = s.t.length
        assert wait(lambda: not hits.empty(), 6) and hits.get() == (h, "pause")
        time.sleep(2.5); assert hits.empty(), "a rule must fire only once"
        mgr.pause(h); mgr.resume(h)               # the user keeps seeding on purpose
        assert wait(lambda: s.state == "Seeding") and s.rule_ignore
        time.sleep(2.5); assert hits.empty(), "a resumed torrent must not be stopped again"
        s.rule_ignore = s.rule_hit = False
        mgr.set_seed_rules(ratio=0, minutes=1, action="remove")
        s.seed_secs = 59.0
        assert wait(lambda: not hits.empty(), 8) and hits.get() == (h, "remove")
        mgr.set_seed_rules(0, 0)
        s.rule_hit = False; time.sleep(2.5); assert hits.empty(), "no rules = never stop"
        s.up_base = 5 * s.t.length; s.uploaded = 0
        assert abs(s.share_ratio - 5.0) < 1e-9
        print("seeding rules (ratio, time, action, fire once, user override, persisted base): OK")

        # ---- per-torrent speed limit + snapshot fields
        mgr.set_torrent_limits(h, 50, 20)
        snap = [x for x in mgr.snapshot() if x["hash"] == h][0]
        assert snap["limit_down"] == 50 and snap["limit_up"] == 20 and snap["ratio"] > 0
        assert s.down_lim.rate == 50 * 1024 and s.up_lim.rate == 20 * 1024
        mgr.set_torrent_limits(h, 0, 0); assert s.down_lim.rate == 0
        mgr.set_category([h], "Music"); mgr.set_priority([h], 5)
        snap = [x for x in mgr.snapshot() if x["hash"] == h][0]
        assert snap["category"] == "Music" and snap["priority"] == 1          # priority is clamped to -1..1
        print("per-torrent limits, category, priority fields: OK")

        # ---- move on completion
        dest = os.path.join(tmp, "Finished")
        mgr.relocate(h, dest)
        assert wait(lambda: s.state == "Seeding", 20), s.state
        assert s.save_dir == dest and os.path.getsize(os.path.join(dest, "Album", "a.bin")) == 300_000
        assert os.path.exists(os.path.join(dest, "Album", "sub", "c.bin")) and not os.path.exists(os.path.join(content, "a.bin"))
        assert s.complete and not s.error
        mgr.remove(h)
        print("move finished download to another folder (files moved, seeding resumes): OK")

        # ---- queue priority: with one slot, a high-priority torrent jumps the queue
        mgr.set_max_active(1)
        d1, d2 = os.path.join(tmp, "q1"), os.path.join(tmp, "q2")
        for d in (d1, d2):
            os.makedirs(d)
            with open(os.path.join(d, "f.bin"), "wb") as f:
                f.write(os.urandom(100_000))
        r1, r2 = create_torrent(os.path.join(d1, "f.bin"), piece_length=32768), create_torrent(os.path.join(d2, "f.bin"), piece_length=32768)
        empty1, empty2 = os.path.join(tmp, "e1"), os.path.join(tmp, "e2")
        os.makedirs(empty1); os.makedirs(empty2)      # no data there -> they need to download, so they occupy the slot
        a, _ = mgr.add(r1, empty1); b, _ = mgr.add(r2, empty2)
        assert wait(lambda: not a.stopped and b.stopped), (a.stopped, b.stopped)
        mgr.set_priority([b.t.hex], 1)
        assert wait(lambda: not b.stopped and a.stopped, 10), (a.stopped, b.stopped)
        assert b.state != "Queued" and a.state == "Queued"
        mgr.set_priority([b.t.hex], -1)
        assert wait(lambda: not a.stopped and b.stopped, 10)
        print("queue priority (high jumps the queue, low yields): OK")
    finally:
        mgr.shutdown()

    # ---- online search: "All sources" (merged, duplicates dropped) and "No result limit"
    import base64, types
    import search
    hx = "ab" * 20
    b32 = base64.b32encode(bytes.fromhex(hx)).decode()
    assert search.info_hash({"magnet": "magnet:?xt=urn:btih:%s&dn=x" % hx.upper()}) == hx
    assert search.info_hash({"magnet": "magnet:?xt=urn:btih:%s" % b32}) == hx, "base32 hashes compare equal to hex"
    assert search.info_hash({"torrent_url": "http://x/y.torrent"}) is None
    res = [{"name": "A", "magnet": "magnet:?xt=urn:btih:" + hx, "seeders": 5, "source": "One"}]
    search.merge(res, [{"name": "A again", "magnet": "magnet:?xt=urn:btih:" + b32, "seeders": 50, "source": "Two"},
                       {"name": "B", "torrent_url": "http://x/b.torrent", "seeders": None, "source": "Two"},
                       {"name": "B", "torrent_url": "http://x/b.torrent", "seeders": None, "source": "Two"}])
    assert [(r["name"], r["source"]) for r in res] == [("A again", "Two"), ("B", "Two"), ("B", "Two")], res
    # no limit: everything is asked for and a page-based plugin may read more pages - for that search only
    mod = types.SimpleNamespace(MAX_PAGES=10)
    asked = []
    src = search.Source("Paged", lambda q, limit: asked.append((limit, mod.MAX_PAGES)) or [], module=mod)
    src.search("x"); src.search("x", unlimited=True)
    assert asked == [(search.LIMIT, 10), (search.NO_LIMIT, search.NO_LIMIT_PAGES)] and mod.MAX_PAGES == 10, (asked, mod.MAX_PAGES)
    # all sources: run together, merged, a failing source reported without losing the others
    mk = lambda h, n, s: {"name": n, "magnet": "magnet:?xt=urn:btih:" + h, "seeders": s}
    def boom(q, limit):
        raise OSError("site down")
    srcs = [search.Source("One", lambda q, limit: [mk("11" * 20, "x", 1), mk("22" * 20, "y", 9)]),
            search.Source("Two", lambda q, limit: [mk("22" * 20, "y", 3), mk("33" * 20, "z", 2)]),
            search.Source("Broken", boom)]
    reports = []
    merged, errors = search.search_all(srcs, "q", report=lambda n, r, e: reports.append((n, e)))
    assert sorted(r["name"] for r in merged) == ["x", "y", "z"] and errors == {"Broken": "site down"}, (merged, errors)
    assert next(r for r in merged if r["name"] == "y")["seeders"] == 9 and len(reports) == 3
    print("online search: all sources merged, duplicates dropped, no-limit pages: OK")
    print("FEATURE TESTS: ALL PASSED")


if __name__ == "__main__":
    main()
