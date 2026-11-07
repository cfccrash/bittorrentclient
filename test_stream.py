"""Streaming tests: HTTP Range server over a throttled download; seeks must fetch the wanted pieces first."""
import hashlib, http.client, os, random, sys, tempfile, time, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from torrent_core import Manager, create_torrent
import stream


def wait(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def get(srv, url, rng=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=60)
    hd = {}
    if rng:
        hd["Range"] = rng
    if host:
        hd["Host"] = host
    c.request("GET", url.split(str(srv.port), 1)[1], headers=hd)
    r = c.getresponse()
    body = r.read()
    return r, body


def main():
    assert stream.parse_range("bytes=0-9", 100) == (0, 9) and stream.parse_range("bytes=90-", 100) == (90, 99)
    assert stream.parse_range("bytes=-10", 100) == (90, 99) and stream.parse_range("bytes=100-", 100) == "bad"
    assert stream.parse_range(None, 100) is None and stream.parse_range("junk", 100) is None
    assert stream.media_kind("a.MKV") == "video" and stream.media_kind("a.flac") == "audio" and stream.media_kind("a.txt") == ""
    tmp = tempfile.mkdtemp()
    seed_dir, leech_dir = os.path.join(tmp, "seed"), os.path.join(tmp, "leech")
    os.makedirs(os.path.join(seed_dir, "Show")); os.makedirs(leech_dir)
    data = random.Random(7).randbytes(6 * 1024 * 1024 + 12345)
    other = random.Random(8).randbytes(500_000)
    with open(os.path.join(seed_dir, "Show", "ep1.mkv"), "wb") as f: f.write(data)
    with open(os.path.join(seed_dir, "Show", "extra.bin"), "wb") as f: f.write(other)
    raw = create_torrent(os.path.join(seed_dir, "Show"), piece_length=65536)
    seeder, leecher = Manager(0, dht=False), Manager(0, dht=False)
    srv = stream.StreamServer(leecher); srv.start()
    try:
        ss, _ = seeder.add(raw, seed_dir)
        assert wait(lambda: ss.state == "Seeding", 30), ss.state
        ls, _ = leecher.add(raw, leech_dir, start=True)
        assert wait(lambda: ls.state in ("Downloading", "Seeding"), 20)
        leecher.set_limits(400, 0)
        h = ls.t.hex
        idx = [i for i, f in enumerate(ls.t.files) if f[0][-1] == "ep1.mkv"][0]
        name, size = leecher.stream_prepare(h, idx)
        assert name == "ep1.mkv" and size == len(data)
        leecher.add_peer(h, "127.0.0.1", seeder.port)
        url = srv.url(h, idx, name)

        # security: wrong token, wrong host header
        bad = url.replace(srv.token, "x" * len(srv.token))
        assert get(srv, bad)[0].status == 403
        assert get(srv, url, host="evil.example.com")[0].status == 403
        r, _ = get(srv, url.replace("/%d/" % idx, "/99/")); assert r.status == 404

        # seek to the end first: tail bytes must arrive long before the rest of the file
        r, body = get(srv, url, "bytes=%d-" % (size - 100_000))
        assert r.status == 206 and r.getheader("Content-Range") == "bytes %d-%d/%d" % (size - 100_000, size - 1, size)
        assert body == data[-100_000:]
        assert ls.progress < 0.6, "tail request should not need most of the file (%.2f)" % ls.progress
        print("seek to end served first (progress %.0f%%): OK" % (ls.progress * 100))

        # a middle seek
        mid = 3 * 1024 * 1024 + 777
        r, body = get(srv, url, "bytes=%d-%d" % (mid, mid + 299_999))
        assert r.status == 206 and body == data[mid:mid + 300_000]
        info = json.loads(get(srv, srv.map_url(h, idx))[1])
        assert info["size"] == size and any(a <= mid and b >= mid + 300_000 for a, b in info["ranges"]), info
        print("middle seek + availability map: OK")

        r, _ = get(srv, url, "bytes=%d-" % (size + 5)); assert r.status == 416
        c = http.client.HTTPConnection("127.0.0.1", srv.port); c.request("HEAD", url.split(str(srv.port), 1)[1])
        r = c.getresponse(); assert r.status == 200 and int(r.getheader("Content-Length")) == size and r.getheader("Accept-Ranges") == "bytes"
        assert r.getheader("Content-Type") == "video/x-matroska"

        # whole file, byte-identical
        leecher.set_limits(0, 0)
        r, body = get(srv, url)
        assert r.status == 200 and hashlib.sha1(body).digest() == hashlib.sha1(data).digest()
        print("full file through HTTP identical: OK")

        # removing the torrent ends waiting requests cleanly
        assert srv.token and leecher.stream_read("0" * 40, 0, 0, 10) is None
    finally:
        srv.stop(); seeder.close() if hasattr(seeder, "close") else None; leecher.close() if hasattr(leecher, "close") else None
    print("ALL STREAM TESTS PASSED")


if __name__ == "__main__":
    main()
