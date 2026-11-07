"""RSS / Atom feeds that add torrents automatically.

A feed rule says where to look and what to take: every ``interval`` minutes the feed is fetched, each item that has
not been seen before is tested against the include / exclude words, and matching items are added to the download list.
This module is pure (no GUI, no threads of its own) so it can be tested; bittorrentclient.py runs it on a worker thread.
"""
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

MAX_SEEN = 3000      # remembered item ids per feed
MAX_ITEMS = 500      # items looked at per fetch
DEFAULT_INTERVAL = 30


def new_feed(url="", name=""):
    return {"name": name, "url": url, "enabled": True, "interval": DEFAULT_INTERVAL, "include": "", "exclude": "",
            "category": "", "folder": "", "start": True, "grab_existing": False, "last": 0, "status": "", "primed": False}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _first(el, name):
    c = _children(el, name)
    return c[0] if c else None


def _is_magnet(u):
    return u.lower().startswith("magnet:")


def _is_http(u):
    return u.lower().startswith(("http://", "https://"))


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_feed(data):
    """-> (feed title, [item dicts]). Each item: id, title, magnet, torrent_url, size, date, page.
    Handles RSS 2.0 (enclosure / link / torrent:magnetURI / nyaa:infoHash) and Atom. Raises ValueError if it is not a feed."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError("not a valid RSS/Atom feed (%s)" % e)
    kind = _local(root.tag)
    if kind == "rss":
        chan = _first(root, "channel")
        if chan is None:
            raise ValueError("RSS feed without a channel")
        title, entries = _text(_first(chan, "title")), _children(chan, "item")
    elif kind == "feed":
        title, entries = _text(_first(root, "title")), _children(root, "entry")
    elif kind == "RDF":
        title, entries = _text(_first(_first(root, "channel") or root, "title")), _children(root, "item")
    else:
        raise ValueError("not an RSS or Atom feed")
    items = []
    for e in entries[:MAX_ITEMS]:
        name = _text(_first(e, "title"))
        magnet = torrent = page = ""
        size = None
        info_hash = ""
        links = []
        for c in e:
            tag = _local(c.tag)
            if tag == "enclosure":
                u = c.get("url", "").strip()
                links.append(u)
                size = size or _int(c.get("length")) or None
            elif tag == "link":
                u = (c.get("href") or _text(c)).strip()
                if c.get("rel") in (None, "alternate"):
                    page = page or u
                elif c.get("rel") == "enclosure":
                    size = size or _int(c.get("length")) or None
                links.append(u)
            elif tag == "magnetURI":
                links.append(_text(c))
            elif tag in ("contentLength", "size"):
                size = size or _int(_text(c))
            elif tag == "infoHash":
                info_hash = _text(c)
            elif tag == "guid":
                if _is_magnet(_text(c)):
                    links.append(_text(c))
        for u in links:
            if _is_magnet(u):
                magnet = magnet or u
            elif _is_http(u) and (u.split("?")[0].lower().endswith(".torrent") or "torrent" in u.lower()):
                torrent = torrent or u
        if not magnet and re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
            magnet = "magnet:?xt=urn:btih:%s&dn=%s" % (info_hash, urllib.parse.quote(name))
        if not magnet and not torrent:
            for c in e:                      # last resort: any enclosure
                if _local(c.tag) == "enclosure" and _is_http(c.get("url", "")):
                    torrent = c.get("url").strip()
                    break
        uid = _text(_first(e, "guid")) or _text(_first(e, "id")) or magnet or torrent or page or name
        items.append(dict(id=uid, title=name, magnet=magnet, torrent_url=torrent, size=size,
                          date=_text(_first(e, "pubDate")) or _text(_first(e, "updated")) or _text(_first(e, "published")),
                          page=page))
    return title, items


def _words(spec):
    """'a b "c d"' -> ['a', 'b', 'c d'];  a spec starting with re: is one regular expression."""
    spec = (spec or "").strip()
    if not spec:
        return []
    if spec.lower().startswith("re:"):
        return [spec]
    return [w.strip('"') for w in re.findall(r'"[^"]*"|\S+', spec)]


def _hit(word, title):
    if word.lower().startswith("re:"):
        try:
            return re.search(word[3:], title, re.I) is not None
        except re.error:
            return False
    return word.lower() in title.lower()


def matches(item, feed):
    """Include: every word (or the regex) must be in the title; empty = take everything. Exclude: any word rejects."""
    title = item.get("title", "")
    inc, exc = _words(feed.get("include")), _words(feed.get("exclude"))
    if inc and not all(_hit(w, title) for w in inc):
        return False
    if exc and any(_hit(w, title) for w in exc):
        return False
    return bool(item.get("magnet") or item.get("torrent_url"))


def check_regex(spec):
    """Error text for a bad 're:' spec, else ''."""
    spec = (spec or "").strip()
    if spec.lower().startswith("re:"):
        try:
            re.compile(spec[3:])
        except re.error as e:
            return str(e)
    return ""


def due(feed, now=None):
    now = now or time.time()
    return bool(feed.get("enabled", True) and feed.get("url") and
                now - float(feed.get("last", 0)) >= max(1, int(feed.get("interval", DEFAULT_INTERVAL))) * 60)


def select_new(feed, items, seen):
    """Split a fetch into (items to download, ids to mark as seen). The first successful look at a feed only records
    what is already there, unless 'grab_existing' is set, so adding a feed does not download its whole history."""
    seen = set(seen)
    fresh = [i for i in items if i["id"] not in seen]
    ids = [i["id"] for i in fresh]
    if not feed.get("primed") and not feed.get("grab_existing"):
        return [], ids
    return [i for i in fresh if matches(i, feed)], ids


def remember(seen_list, ids):
    """Append new ids (oldest dropped first) keeping at most MAX_SEEN."""
    have = set(seen_list)
    out = list(seen_list) + [i for i in ids if i not in have]
    return out[-MAX_SEEN:]


def fetch(url, timeout=20, get=None):
    """Download a feed (http / https only)."""
    if not _is_http(url or ""):
        raise ValueError("feed addresses must start with http:// or https://")
    if get is None:
        import search
        get = search.http_get
    return get(url, timeout)


def run_feed(feed, seen, get=None):
    """One check of one feed (worker thread; no GUI access). Returns a dict:
    title, total (items in the feed), jobs [(item, 'magnet'|'torrent', uri-or-bytes)], ids (to mark as seen), errors."""
    if get is None:
        import search
        get = search.http_get
    title, items = parse_feed(fetch(feed["url"], get=get))
    chosen, ids = select_new(feed, items, seen)
    jobs, failed, errors = [], set(), []
    for it in chosen:
        if it["magnet"]:
            jobs.append((it, "magnet", it["magnet"]))
            continue
        try:
            raw = get(it["torrent_url"], 20)
            if not raw.startswith(b"d"):
                raise ValueError("not a .torrent file")
            jobs.append((it, "torrent", raw))
        except Exception as e:                     # try again at the next check
            failed.add(it["id"])
            errors.append("%s: %s" % (it["title"] or it["torrent_url"], e))
    return dict(title=title, total=len(items), jobs=jobs, ids=[i for i in ids if i not in failed], errors=errors)


# -- searching the items of your feeds ------------------------------------------------------------
_cache = {}            # url -> (fetched at, feed title, items)
CACHE_SECONDS = 300


def search_feeds(feeds, query, limit=100, get=None, now=None):
    """Items of all switched-on feeds whose title matches `query` (same rules as the include words), newest first as
    each feed lists them. Feeds are fetched in parallel and kept for 5 minutes, so repeated searches are instant.
    Returns search-result dicts; raises if every feed failed."""
    import concurrent.futures
    now = now or time.time()
    words = _words(query)
    todo = [f for f in feeds if f.get("enabled", True) and _is_http(f.get("url", ""))]
    if not todo:
        raise ValueError("no RSS feeds are set up (use the RSS feeds button first)")

    def load(f):
        hit = _cache.get(f["url"])
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
        title, items = parse_feed(fetch(f["url"], timeout=15, get=get))
        _cache[f["url"]] = (now, title, items)
        return title, items

    results, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
        futures = [(f, pool.submit(load, f)) for f in todo]
        for f, fut in futures:
            try:
                title, items = fut.result()
            except Exception as e:
                errors.append("%s: %s" % (f.get("name") or f["url"], e))
                continue
            label = f.get("name") or title or f["url"]
            for it in items:
                if not (it["magnet"] or it["torrent_url"]):
                    continue
                if words and not all(_hit(w, it["title"]) for w in words):
                    continue
                results.append({"name": it["title"] or "(untitled)", "size": it["size"], "seeders": None, "leechers": None,
                                "magnet": it["magnet"] or None, "torrent_url": it["torrent_url"] or None,
                                "page": it["page"] or None, "source": label})
    if errors and not results and len(errors) == len(todo):
        raise ValueError("; ".join(errors))
    return results[:limit]


# -- finding feeds to subscribe to ----------------------------------------------------------------
COMMON_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/feeds/posts/default", "/?feed=rss2")
DIRECTORY_URL = "https://cloud.feedly.com/v3/search/feeds?query=%s&count=20"


def looks_like_url(text):
    """A website address (rather than words to search for)?"""
    t = text.strip()
    if not t or " " in t:
        return False
    if t.lower().startswith(("http://", "https://")):
        return True
    return bool(re.fullmatch(r"[\w.-]+\.[a-zA-Z]{2,}(:\d+)?(/\S*)?", t))


def normalise_url(text):
    t = text.strip()
    return t if t.lower().startswith(("http://", "https://")) else "https://" + t


class _LinkFinder:
    """Collects <link rel="alternate" type="application/rss+xml" href=...> from a page."""

    def __init__(self, base):
        import html.parser
        self.base, self.found = base, []
        outer = self

        class P(html.parser.HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag != "link":
                    return
                a = {k.lower(): (v or "") for k, v in attrs}
                kind = a.get("type", "").lower()
                if "alternate" in a.get("rel", "").lower() and ("rss" in kind or "atom" in kind) and a.get("href"):
                    outer.found.append((urllib.parse.urljoin(outer.base, a["href"]), a.get("title", "")))
        self.parser = P(convert_charrefs=True)

    def feed(self, text):
        try:
            self.parser.feed(text)
        except Exception:
            pass
        return self.found


def describe(url, get=None):
    """Fetch a candidate and say what is in it: dict(url, title, items, torrents, sample) - or None if it is not a feed."""
    try:
        title, items = parse_feed(fetch(url, timeout=12, get=get))
    except Exception:
        return None
    dl = [i for i in items if i["magnet"] or i["torrent_url"]]
    return dict(url=url, title=title or url, items=len(items), torrents=len(dl), sample=[i["title"] for i in dl[:3]])


def _describe_all(urls, get=None):
    import concurrent.futures
    urls = list(dict.fromkeys(urls))
    if not urls:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        found = [d for d in pool.map(lambda u: describe(u, get), urls) if d]
    found.sort(key=lambda d: (-d["torrents"], -d["items"]))     # feeds you can download from first
    return found


def discover(site, get=None):
    """Feeds published by a website (or the feed itself if the address already is one), best candidates first.
    Looks for <link rel=alternate> tags, then tries the usual paths. Raises ValueError when nothing is found."""
    url = normalise_url(site)
    if not _is_http(url):
        raise ValueError("enter a website address starting with http:// or https://")
    try:
        data = fetch(url, timeout=15, get=get)
    except Exception as e:
        raise ValueError("could not open %s: %s" % (url, e))
    try:
        title, items = parse_feed(data)          # the address is itself a feed
        dl = [i for i in items if i["magnet"] or i["torrent_url"]]
        return [dict(url=url, title=title or url, items=len(items), torrents=len(dl), sample=[i["title"] for i in dl[:3]])]
    except ValueError:
        pass
    links = [u for u, _t in _LinkFinder(url).feed(data.decode("utf-8", "replace"))]
    found = _describe_all(links, get)
    if not found:
        parts = urllib.parse.urlsplit(url)
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        found = _describe_all([origin + p for p in COMMON_PATHS], get)
    if not found:
        raise ValueError("no feed found on %s. Try the address of a feed itself, or a keyword." % url)
    return found


def directory_search(keyword, get=None):
    """Feeds matching a keyword, from a public feed directory (best effort: it needs that service to be reachable and
    answering). Each candidate is fetched so the list shows how many downloadable torrents it really has."""
    import json
    if get is None:
        import search
        get = search.http_get
    try:
        data = json.loads(get(DIRECTORY_URL % urllib.parse.quote(keyword), 15).decode("utf-8", "replace"))
    except Exception as e:
        raise ValueError("the feed directory could not be reached or did not answer (%s). Paste a website address "
                         "instead, and the app will look for its feeds." % e)
    urls = []
    for r in data.get("results", []) if isinstance(data, dict) else []:
        fid = r.get("feedId", "")
        u = fid[5:] if fid.startswith("feed/") else r.get("website", "")
        if _is_http(u):
            urls.append(u)
    found = _describe_all(urls[:20], get)
    if not found:
        raise ValueError("no working feeds found for '%s'" % keyword)
    return found


def find_feeds(text, get=None):
    """Website address -> discover(); anything else -> directory_search()."""
    return discover(text, get) if looks_like_url(text) else directory_search(text.strip(), get)
