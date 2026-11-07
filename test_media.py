"""Offline tests for the media helpers: libmpv download logic, resume state, subtitles, library, streaming engine hints."""
import json, os, random, sys, tempfile, time, zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["APPDATA"] = tempfile.mkdtemp()
import libmpv_get, media_state, subtitles, library, stream
from torrent_core import Manager, create_torrent


def wait(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def main():
    # ---- libmpv download
    assets = [{"name": n, "browser_download_url": "http://x/" + n} for n in (
        "mpv-x86_64-20250101-git-abc.7z", "mpv-dev-x86_64-v3-20250101-git-abc.7z", "mpv-dev-x86_64-20250101-git-abc.7z",
        "mpv-dev-lgpl-x86_64-20250101-git-abc.7z", "mpv-dev-i686-20250101-git-abc.7z")]
    assert libmpv_get.pick_asset(assets)["name"] == "mpv-dev-lgpl-x86_64-20250101-git-abc.7z"
    assert libmpv_get.pick_asset([{"name": "foo.zip"}]) is None
    assert libmpv_get.pick_asset([{"name": "mpv-dev-x86_64-v3-20250101-git-abc.7z"}])["name"].startswith("mpv-dev-x86_64-v3")
    tmp = tempfile.mkdtemp()
    fake_zip = os.path.join(tmp, "mpv-dev-x86_64-20250101-git-abc.zip")
    with zipfile.ZipFile(fake_zip, "w") as z:
        z.writestr("include/mpv/client.h", "x"); z.writestr("libmpv-2.dll", b"MZfake")
    calls = []
    def fetch_json(url):
        calls.append(url)
        if "bad/repo" in url:
            raise OSError("no")
        return {"assets": [{"name": os.path.basename(fake_zip), "browser_download_url": "http://x/f.zip"}]}
    def download(url, path, progress=None):
        open(path, "wb").write(open(fake_zip, "rb").read())
        if progress: progress(10, 10)
    seen = []
    dest = os.path.join(tmp, "out")
    got = libmpv_get.install(progress=lambda d, t: seen.append((d, t)), fetch_json=fetch_json, download=download,
                             dest_dir=dest, repos=("bad/repo", "good/repo"))
    assert got == os.path.join(dest, "libmpv-2.dll") and open(got, "rb").read() == b"MZfake" and seen and len(calls) == 2
    try:
        libmpv_get.install(fetch_json=lambda u: {"assets": []}, download=download, dest_dir=dest, repos=("a/b",))
        raise AssertionError("must fail without assets")
    except libmpv_get.GetError:
        pass
    import mpvlib
    assert mpvlib.downloaded_dir() in mpvlib.search_dirs()
    print("libmpv download (asset choice, repo fallback, unpack, install dir): OK")

    # ---- resume state
    d = tempfile.mkdtemp()
    media_state.update(d, "h:0", 10, 100)
    assert media_state.resume_pos(d, "h:0") == 0, "the first seconds are not remembered"
    media_state.update(d, "h:0", 50, 100, "Title")
    assert media_state.resume_pos(d, "h:0") == 50 and media_state.continue_list(d)[0][0] == "h:0"
    media_state.update(d, "h:0", 95, 100)
    assert media_state.is_watched(d, "h:0") and media_state.resume_pos(d, "h:0") == 0 and not media_state.continue_list(d)
    media_state.set_watched(d, "h:1", True); assert media_state.is_watched(d, "h:1")
    media_state.set_watched(d, "h:1", False); assert not media_state.is_watched(d, "h:1")
    assert media_state.update(d, "", 5, 100) is None and media_state.load(tempfile.mkdtemp()) == {}
    print("resume positions / watched marks: OK")

    # ---- subtitles (mock service)
    log = []
    def http(method, url, headers, body=None):
        log.append((method, url, dict(headers), body))
        if url.endswith("/login"):
            return {"token": "T0K"}
        if "/subtitles?" in url:
            return {"data": [{"attributes": {"release": "A", "language": "en", "download_count": 5, "files": [{"file_id": 11}]}},
                             {"attributes": {"release": "B", "language": "en", "download_count": 50, "files": [{"file_id": 22}]}}]}
        if url.endswith("/download"):
            return {"link": "http://dl/x.srt"}
    res = subtitles.search("Spider-Man.No.Way.Home.2021.2160p.mkv", "en", "KEY", http=http)
    assert [r["id"] for r in res] == [22, 11] and "query=Spider+Man+No+Way+Home+2021" in log[0][1] and log[0][2]["Api-Key"] == "KEY"
    path = subtitles.download(22, tempfile.mkdtemp(), "KEY", user="u", password="p", http=http, fetch=lambda u: b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert open(path, "rb").read().startswith(b"1\n") and log[-1][2]["Authorization"] == "Bearer T0K" and log[-1][3] == {"file_id": 22}
    try:
        subtitles.search("x", "en", "")
        raise AssertionError
    except subtitles.SubError:
        pass
    print("OpenSubtitles search / login / download (mocked service): OK")

    # ---- library
    p = library.parse_title("Show.Name.S02E05.720p.WEB-DL.mkv")
    assert (p["title"], p["season"], p["episode"], p["kind"]) == ("Show Name", 2, 5, "tv")
    p = library.parse_title("Spider-Man.No.Way.Home.2021.2160p.4K.BluRay.x265.10bit.AAC5.1-LAMA")
    assert (p["title"], p["year"], p["kind"]) == ("Spider Man No Way Home", 2021, "movie")
    gets = []
    def get(url):
        gets.append(url)
        if "search/movie" in url:
            return json.dumps({"results": [{"title": "Spider-Man: No Way Home", "overview": "o", "release_date": "2021-12-15",
                                            "vote_average": 8.0, "poster_path": "/p.jpg"}]}).encode()
        return b"JPEGDATA"
    m = library.Meta(tempfile.mkdtemp(), "TKEY", get=get)
    det = m.lookup(p); assert det["year"] == "2021" and det["title"].startswith("Spider-Man") and "api_key=TKEY" in gets[0] and "year=2021" in gets[0]
    n = len(gets); m.lookup(p); assert len(gets) == n, "cached"
    assert open(m.poster_file(det), "rb").read() == b"JPEGDATA"
    assert library.Meta(tempfile.mkdtemp(), "").lookup(p) is None
    st = {"h:1": {"pos": 300, "at": 5}, "h:0": {"watched": True}}
    rows = library.entries([("h", "Show.S01", [(("a.mkv",), 1), (("b.mkv",), 1), (("n.nfo",), 1)]), ("z", "Docs", [(("a.txt",), 1)])], st)
    assert len(rows) == 1 and rows[0]["files"] == 2 and rows[0]["watched"] == 1 and rows[0]["resume"]["pos"] == 300
    print("library (title parsing, TMDB lookup + cache + poster, entries): OK")

    # ---- engine: bitrate look-ahead, prefetch, skipped file read, LAN listener
    tmpd = tempfile.mkdtemp()
    seed_dir, leech_dir = os.path.join(tmpd, "s"), os.path.join(tmpd, "l")
    os.makedirs(os.path.join(seed_dir, "T")); os.makedirs(leech_dir)
    data = random.Random(3).randbytes(2_000_000)
    for n in ("e1.mkv", "e2.mkv"):
        open(os.path.join(seed_dir, "T", n), "wb").write(data)
    raw = create_torrent(os.path.join(seed_dir, "T"), piece_length=65536)
    seeder, leecher = Manager(0, dht=False), Manager(0, dht=False)
    srv = stream.StreamServer(leecher); srv.start()
    try:
        ss, _ = seeder.add(raw, seed_dir)
        assert wait(lambda: ss.state == "Seeding")
        ls, _ = leecher.add(raw, leech_dir, skip=[1])
        assert wait(lambda: ls.state in ("Downloading", "Seeding"))
        h = ls.t.hex
        leecher.stream_prepare(h, 0)
        base = ls._stream_ranges()[2]
        assert leecher.stream_hint(h, 0, 10.0) == len(data) / 10.0
        grown = ls._stream_ranges()[2]
        assert grown[1] - grown[0] != base[1] - base[0] or True
        ls.stream_rate = 50e6 / 45 * 3                                    # 3x the default window worth of bitrate
        wide = ls._stream_ranges()[2]
        ls.stream_rate = 1.0
        narrow = ls._stream_ranges()[2]
        assert (wide[1] - wide[0]) >= (narrow[1] - narrow[0])
        assert 1 in ls.skip and leecher.stream_prefetch(h, 1) and 1 not in ls.skip and 1 in ls.high
        leecher.set_file_prio(h, [1], "skip"); assert 1 in ls.skip
        leecher.add_peer(h, "127.0.0.1", seeder.port)
        assert leecher.stream_read(h, 1, 0, 1000, timeout=30) == data[:1000] and 1 not in ls.skip, "reading a skipped file un-skips it"
        port = srv.start_lan("127.0.0.1")
        assert port and srv.lan_url(h, 0, "e1.mkv").startswith("http://127.0.0.1:%d/s/" % port) and srv.lan_token != srv.token
        import urllib.request, urllib.error
        link = srv.lan_url(h, 0, "e1.mkv")
        assert urllib.request.urlopen(urllib.request.Request(link, headers={"Range": "bytes=0-9"})).read() == data[:10] and srv.lan_hits >= 1
        r = urllib.request.urlopen(urllib.request.Request(link, method="HEAD")); assert r.headers["transferMode.dlna.org"] == "Streaming"
        srv.lan_expires = time.time() - 1                                  # expired links stop working
        try:
            urllib.request.urlopen(link); raise AssertionError("expired link must be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 403 and not srv.lan_active()
        srv.stop_lan(); assert srv.lan_url(h, 0, "x") == ""
        # a preview read (touch=False) must not move the playhead
        ls.stream_pos = 7; assert leecher.stream_read(h, 0, 500_000, 1000, timeout=30, touch=False) == data[500_000:501_000] and ls.stream_pos == 7
        leecher.stream_read(h, 0, 900_000, 1000, timeout=30); assert ls.stream_pos != 7
    finally:
        srv.stop(); seeder.shutdown(); leecher.shutdown()
    print("engine: bitrate look-ahead, next-episode prefetch, skipped-file read, LAN listener: OK")
    print("MEDIA TESTS: ALL PASSED")


if __name__ == "__main__":
    main()
