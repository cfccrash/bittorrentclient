"""RSS auto-download: parsing (RSS 2.0, Atom, nyaa/torrent namespaces), filters, first-look priming, dedupe, and an
end-to-end run against a local HTTP server that serves a feed and a .torrent."""
import http.server, os, sys, tempfile, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rss
from torrent_core import create_torrent, Torrent

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:torrent="http://xmlns.ezrss.it/0.1/" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
<channel><title>Test feed</title>
<item><title>Show S01E01 1080p</title><guid>g1</guid>
  <enclosure url="http://example.invalid/a.torrent" length="1234" type="application/x-bittorrent"/></item>
<item><title>Show S01E02 720p</title><guid>g2</guid><link>magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&amp;dn=x</link></item>
<item><title>Other Thing 1080p</title><guid>g3</guid><torrent:magnetURI>magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</torrent:magnetURI></item>
<item><title>Hashed 1080p</title><guid>g4</guid><nyaa:infoHash>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</nyaa:infoHash><nyaa:size>5 GiB</nyaa:size></item>
<item><title>No link at all</title><guid>g5</guid><link>http://example.invalid/page.html</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom feed</title>
<entry><title>Atom Ep 1</title><id>tag:1</id><link rel="alternate" href="http://example.invalid/page"/>
  <link rel="enclosure" href="http://example.invalid/x.torrent" length="99"/><updated>2026-01-01T00:00:00Z</updated></entry>
</feed>"""


def main():
    title, items = rss.parse_feed(RSS)
    assert title == "Test feed" and len(items) == 5
    assert items[0]["torrent_url"].endswith("a.torrent") and items[0]["size"] == 1234 and not items[0]["magnet"]
    assert items[1]["magnet"].startswith("magnet:?xt=urn:btih:0123") and items[1]["id"] == "g2"
    assert items[2]["magnet"].endswith("a" * 40)
    assert items[3]["magnet"].startswith("magnet:?xt=urn:btih:" + "b" * 40)
    assert not items[4]["magnet"] and not items[4]["torrent_url"] and items[4]["page"].endswith("page.html")
    t2, a = rss.parse_feed(ATOM)
    assert t2 == "Atom feed" and a[0]["torrent_url"].endswith("x.torrent") and a[0]["page"].endswith("/page") and a[0]["size"] == 99
    for bad in (b"<html><body>hi</body></html>", b"not xml at all", b"<rss></rss>"):
        try:
            rss.parse_feed(bad); raise SystemExit("accepted a bad feed")
        except ValueError:
            pass
    print("feed parsing (RSS 2.0, Atom, magnetURI, infoHash, bad input): OK")

    f = rss.new_feed("http://x/", "t")
    f.update(include="show 1080p")
    assert [i["id"] for i in items if rss.matches(i, f)] == ["g1"]
    f.update(include='"Show S01" ', exclude="720p")
    assert [i["id"] for i in items if rss.matches(i, f)] == ["g1"]
    f.update(include="re:^(show|other).*1080p$", exclude="")
    assert [i["id"] for i in items if rss.matches(i, f)] == ["g1", "g3"]
    f.update(include="", exclude="")
    assert [i["id"] for i in items if rss.matches(i, f)] == ["g1", "g2", "g3", "g4"]      # g5 has nothing to download
    assert rss.check_regex("re:(") and not rss.check_regex("re:ok") and not rss.check_regex("plain (")
    assert rss.matches(items[0], dict(include="re:[", exclude="")) is False                # a broken regex never matches
    print("filters (all words, phrases, exclude, regex, nothing to download): OK")

    # first look only records what is there; later looks take new matches; seen items never come back
    f = rss.new_feed("http://x/", "t")
    f.update(include="1080p")
    take, ids = rss.select_new(f, items, [])
    assert take == [] and set(ids) == {"g1", "g2", "g3", "g4", "g5"}
    f["primed"] = True
    seen = rss.remember([], ids)
    more = items + [dict(id="g6", title="New 1080p", magnet="magnet:?xt=urn:btih:" + "c" * 40, torrent_url="")]
    take, ids = rss.select_new(f, more, seen)
    assert [i["id"] for i in take] == ["g6"] and ids == ["g6"]
    seen = rss.remember(seen, ids)
    assert rss.select_new(f, more, seen) == ([], [])
    f2 = dict(f, primed=False, grab_existing=True)
    assert [i["id"] for i in rss.select_new(f2, items, [])[0]] == ["g1", "g3", "g4"]
    assert len(rss.remember([], [str(i) for i in range(rss.MAX_SEEN + 50)])) == rss.MAX_SEEN
    assert rss.due(dict(f, last=0)) and not rss.due(dict(f, last=time.time())) and not rss.due(dict(f, enabled=False))
    assert not rss.due(dict(url="", last=0))
    print("new-item logic (priming, dedupe, grab existing, cap, due): OK")

    # end to end over HTTP
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "ep.bin"); open(src, "wb").write(os.urandom(50000))
    torrent = create_torrent(src, piece_length=32768)
    state = {"feed": b""}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"<html>error page</html>" if self.path == "/bad.torrent"
                    else torrent if self.path.endswith(".torrent") else state["feed"])
            if self.path == "/missing.torrent":
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_port
    feed_xml = lambda extra="": ("""<rss version="2.0"><channel><title>Local</title>
      <item><title>Ep One 1080p</title><guid>e1</guid><enclosure url="%s/ep1.torrent" length="1"/></item>%s
      </channel></rss>""" % (base, extra)).encode()
    state["feed"] = feed_xml()
    feed = rss.new_feed(base + "/feed.xml", "Local"); feed["include"] = "1080p"
    res = rss.run_feed(feed, [])
    assert res["title"] == "Local" and res["total"] == 1 and res["jobs"] == [] and res["ids"] == ["e1"]     # first look: prime only
    feed["primed"] = True
    seen = rss.remember([], res["ids"])
    state["feed"] = feed_xml("""<item><title>Ep Two 1080p</title><guid>e2</guid><enclosure url="%s/ep2.torrent" length="1"/></item>
        <item><title>Ep Three 1080p</title><guid>e3</guid><enclosure url="%s/missing.torrent" length="1"/></item>
        <item><title>Ep Four 1080p</title><guid>e4</guid><enclosure url="%s/bad.torrent" length="1"/></item>
        <item><title>Ep Five 720p</title><guid>e5</guid><enclosure url="%s/ep5.torrent" length="1"/></item>""" % ((base,) * 4))
    res = rss.run_feed(feed, seen)
    assert [j[0]["id"] for j in res["jobs"]] == ["e2"] and res["jobs"][0][1] == "torrent", (res["jobs"], res["errors"], res["ids"])
    assert Torrent(res["jobs"][0][2]).name == "ep.bin"
    assert len(res["errors"]) == 2 and "e3" not in res["ids"] and "e4" not in res["ids"] and {"e2", "e5"} <= set(res["ids"])
    try:
        rss.run_feed(dict(feed, url="file:///etc/passwd"), []); raise SystemExit("file:// accepted")
    except ValueError:
        pass
    # finding feeds to subscribe to
    assert rss.looks_like_url("example.com") and rss.looks_like_url("https://a.b/c") and rss.looks_like_url("sub.site.org/rss")
    assert not rss.looks_like_url("ubuntu iso") and not rss.looks_like_url("ubuntu") and not rss.looks_like_url("")
    assert rss.normalise_url("example.com/x") == "https://example.com/x"
    torrents_feed = feed_xml()
    blog = b"<rss version='2.0'><channel><title>Blog</title><item><title>Hello</title><guid>b1</guid><link>http://x/p</link></item></channel></rss>"
    pages = {
        "/site": b"""<html><head><link rel="alternate" type="application/rss+xml" title="Releases" href="/releases.xml">
            <link rel="alternate" type="application/atom+xml" href="blog.xml"><link rel="stylesheet" href="a.css"></head></html>""",
        "/releases.xml": torrents_feed, "/blog.xml": blog,
        "/bare/": b"<html><body>no feed tags</body></html>", "/bare/feed.xml": torrents_feed,
        "/nothing": b"<html></html>", "/selffeed": torrents_feed,
    }
    class F(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in pages:
                self.send_response(200); self.end_headers(); self.wfile.write(pages[path])
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *a):
            pass
    srv2 = http.server.HTTPServer(("127.0.0.1", 0), F)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    b2 = "http://127.0.0.1:%d" % srv2.server_port
    pages["/releases.xml"] = pages["/bare/feed.xml"] = pages["/selffeed"] = feed_xml().replace(base.encode(), b2.encode())
    got = rss.discover(b2 + "/site")
    assert [g["url"] for g in got] == [b2 + "/releases.xml", b2 + "/blog.xml"], got      # downloadable feed ranked first
    assert got[0]["torrents"] == 1 and got[1]["torrents"] == 0 and got[0]["title"] == "Local"
    got = rss.discover(b2 + "/selffeed")                                                  # the address is a feed already
    assert len(got) == 1 and got[0]["url"] == b2 + "/selffeed" and got[0]["torrents"] == 1
    try:
        rss.discover(b2 + "/nothing"); raise SystemExit("found a feed where there is none")
    except ValueError as e:
        assert "no feed found" in str(e)
    try:
        rss.discover("ftp://x/y"); raise SystemExit("ftp accepted")
    except ValueError:
        pass
    # probing the usual paths when the page has no <link> tags (the site root answers with a page that has none)
    pages["/"] = pages["/bare/"]
    pages["/feed.xml"] = pages["/selffeed"]
    got = rss.discover(b2)
    assert [g["url"] for g in got] == [b2 + "/feed.xml"], got
    srv2.shutdown()
    # keyword search through the directory (stubbed network)
    import json as _j
    net = {rss.DIRECTORY_URL % "linux": _j.dumps({"results": [{"feedId": "feed/http://t.example/a.xml"}, {"feedId": "feed/http://t.example/b.xml"},
                                                              {"feedId": "feed/ftp://bad"}, {"website": "http://t.example/c"}]}).encode(),
           "http://t.example/a.xml": blog, "http://t.example/b.xml": torrents_feed.replace(base.encode(), b"http://t.example")}
    def fake_get(url, timeout=15):
        if url in net:
            return net[url]
        raise OSError("unreachable")
    got = rss.find_feeds("linux", fake_get)
    assert [g["url"] for g in got] == ["http://t.example/b.xml", "http://t.example/a.xml"] and got[0]["torrents"] == 1
    try:
        rss.find_feeds("nonexistentword", fake_get); raise SystemExit("directory failure not reported")
    except ValueError as e:
        assert "directory" in str(e)
    print("finding feeds (site <link> tags, usual paths, the feed itself, nothing found, directory keyword search): OK")

    # searching the items of your feeds
    rss._cache.clear()
    state["feed"] = feed_xml("""<item><title>Ep Two 1080p</title><guid>e2</guid><enclosure url="%s/ep2.torrent" length="7"/></item>
        <item><title>Other Show 720p</title><guid>e9</guid><link>magnet:?xt=urn:btih:%s</link></item>
        <item><title>No download here</title><guid>e8</guid><link>http://x/page</link></item>""" % (base, "d" * 40))
    f1 = rss.new_feed(base + "/feed.xml", "Mine")
    f2 = rss.new_feed(base + "/dead.xml", "Dead")
    f2["url"] = "http://127.0.0.1:1/none.xml"
    got = rss.search_feeds([f1, f2], "", 100)
    assert [r["name"] for r in got] == ["Ep One 1080p", "Ep Two 1080p", "Other Show 720p"], [r["name"] for r in got]
    assert all(r["source"] == "Mine" for r in got) and got[1]["size"] == 7 and got[2]["magnet"].endswith("d" * 40)
    assert [r["name"] for r in rss.search_feeds([f1, f2], "1080p ep", 100)] == ["Ep One 1080p", "Ep Two 1080p"]
    assert [r["name"] for r in rss.search_feeds([f1], "re:^other", 100)] == ["Other Show 720p"]
    assert len(rss.search_feeds([f1], "", 2)) == 2
    state["feed"] = b"changed"                                       # cached for five minutes: still answers
    assert len(rss.search_feeds([f1], "", 100)) == 3
    try:
        rss.search_feeds([f2], "x", 10); raise SystemExit("all feeds failed but no error")
    except ValueError as e:
        assert "Dead" in str(e)
    try:
        rss.search_feeds([dict(f1, enabled=False)], "x", 10); raise SystemExit("no feeds but no error")
    except ValueError:
        pass
    print("searching your feeds (all feeds, words, regex, limit, cache, dead feed, none set up): OK")
    srv.shutdown()
    print("end to end over HTTP (prime, new item, failed downloads retried, only http/https): OK")
    print("RSS TESTS: ALL PASSED")


if __name__ == "__main__":
    main()
