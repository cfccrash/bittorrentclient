"""Online torrent search: a small plugin system plus one built-in source (the Internet Archive).

A *source* is a function ``search(query, limit)`` returning a list of result dicts:

    name         (str)   title shown in the list
    size         (int)   bytes, or None
    seeders      (int)   or None when the source does not know
    leechers     (int)   or None
    magnet       (str)   a magnet: link ...
    torrent_url  (str)   ... or a direct link to a .torrent file (one of the two is required)
    page         (str)   optional web page for the item

Extra sources are plain .py files in the ``search_plugins`` folder inside the app's data folder
(the Search window's "Plugins" button opens it). A plugin defines

    NAME = "Display name"
    def search(query, limit=1000): ...

and may use ``import search as helpers`` for ``http_get`` / ``http_json`` / ``make_magnet``. A plugin that reads result
pages should stop at a module-level ``MAX_PAGES``: with the Search window's "No result limit" ticked the app raises
it to NO_LIMIT_PAGES for that search (and passes a huge ``limit``), so stop early when a page comes back empty. Files whose name starts
with ``_`` are ignored. Plugins run with the app's permissions - only install ones you trust.
"""
import html          # noqa: F401  (imported so packaged builds bundle the modules plugins commonly need)
import importlib.util
import json
import os
import re            # noqa: F401
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree   # noqa: F401

USER_AGENT = "BitTorrentClient/1.1 (+search)"
TIMEOUT = 15
LIMIT = 1000       # results asked of each source (each source returns as many as it allows, up to this)
NO_LIMIT = 1_000_000   # "No result limit": ask for everything the source has
NO_LIMIT_PAGES = 100   # ...and let page-based plugins (MAX_PAGES) read this many pages; they stop early on an empty page
ALL = "All sources"    # the Search window's entry that searches every source at once


def http_get(url, timeout=TIMEOUT, headers=None):
    """GET a URL and return the body bytes (HTTP errors raise)."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(8 * 1024 * 1024)


def http_json(url, timeout=TIMEOUT):
    return json.loads(http_get(url, timeout).decode("utf-8", "replace"))


def make_magnet(info_hash, name="", trackers=()):
    """Build a magnet link from a hex info-hash."""
    uri = "magnet:?xt=urn:btih:" + info_hash
    if name:
        uri += "&dn=" + urllib.parse.quote(name)
    for t in trackers:
        uri += "&tr=" + urllib.parse.quote(t, safe="")
    return uri


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# built-in source: Internet Archive (https://archive.org) - public-domain / freely shareable items
# --------------------------------------------------------------------------
def archive_search(query, limit=LIMIT):
    clean = re.sub(r'[\"():\[\]{}\\/^~*?!<>=&|+-]', " ", query).strip()
    if not clean:
        return []
    params = [("q", '(%s) AND format:"Archive BitTorrent"' % clean),
              ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "item_size"), ("fl[]", "downloads"),
              ("sort[]", "downloads desc"), ("rows", str(min(limit, 10000))), ("page", "1"), ("output", "json")]
    data = http_json("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params))
    out = []
    for d in data.get("response", {}).get("docs", []):
        ident = d.get("identifier")
        if not ident:
            continue
        title = d.get("title")
        if isinstance(title, list):
            title = title[0] if title else ""
        q = urllib.parse.quote(ident)
        out.append({"name": title or ident, "size": _num(d.get("item_size")), "seeders": None, "leechers": None,
                    "torrent_url": "https://archive.org/download/%s/%s_archive.torrent" % (q, q),
                    "page": "https://archive.org/details/" + q})
    return out


# --------------------------------------------------------------------------
# plugins
# --------------------------------------------------------------------------
class Source:
    def __init__(self, name, fn, path=None, module=None):
        self.name, self.fn, self.path, self.module = name, fn, path, module

    def search(self, query, limit=LIMIT, unlimited=False):
        """Results as normalised dicts. `unlimited` asks for everything: limit NO_LIMIT, and a plugin that reads
        pages up to a module-level MAX_PAGES may read NO_LIMIT_PAGES for this search."""
        mod = self.module
        pages = getattr(mod, "MAX_PAGES", None) if unlimited and mod is not None else None
        if isinstance(pages, int) and pages < NO_LIMIT_PAGES:
            mod.MAX_PAGES = NO_LIMIT_PAGES
        try:
            res = self.fn(query, NO_LIMIT if unlimited else limit)
        finally:
            if isinstance(pages, int):
                mod.MAX_PAGES = pages
        out = []
        for r in res or []:
            if not isinstance(r, dict) or not (r.get("magnet") or r.get("torrent_url")):
                continue
            out.append({"name": str(r.get("name") or "(unnamed)"), "size": _num(r.get("size")),
                        "seeders": _num(r.get("seeders")), "leechers": _num(r.get("leechers")),
                        "magnet": r.get("magnet") or None, "torrent_url": r.get("torrent_url") or None,
                        "page": r.get("page"), "source": r.get("source") or self.name})
        return out


BUILTIN = [Source("Internet Archive", archive_search)]

TEMPLATE = '''"""Example search plugin. Copy this file, remove the leading underscore from its name and edit it."""
import urllib.parse
import search as helpers   # helpers.http_json(url), helpers.http_get(url), helpers.make_magnet(hash, name)

NAME = "My source"


def search(query, limit=1000):
    # data = helpers.http_json("https://example.org/api?q=" + urllib.parse.quote(query))
    # return [{"name": d["title"], "size": d["bytes"], "seeders": d["seeds"], "leechers": d["peers"],
    #          "magnet": helpers.make_magnet(d["hash"], d["title"])} for d in data][:limit]
    return []
'''


def plugin_dir(data_dir):
    d = os.path.join(data_dir, "search_plugins")
    os.makedirs(d, exist_ok=True)
    example = os.path.join(d, "_example.py")
    if not os.path.exists(example):
        try:
            with open(example, "w", encoding="utf-8") as f:
                f.write(TEMPLATE)
        except OSError:
            pass
    return d


def load_sources(data_dir):
    """Built-in sources plus every plugin in the plugin folder. Returns (sources, errors)."""
    sources, errors = list(BUILTIN), []
    try:
        d = plugin_dir(data_dir)
        names = sorted(n for n in os.listdir(d) if n.endswith(".py") and not n.startswith("_"))
    except OSError:
        return sources, errors
    for n in names:
        path = os.path.join(d, n)
        try:
            spec = importlib.util.spec_from_file_location("btc_plugin_" + n[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "search")
            if not callable(fn):
                raise TypeError("'search' must be a function")
            sources.append(Source(str(getattr(mod, "NAME", n[:-3])), fn, path, mod))
        except Exception as e:
            errors.append("%s: %s" % (n, e))
            traceback.print_exc()
    return sources, errors


# --------------------------------------------------------------------------
# all sources at once
# --------------------------------------------------------------------------
def info_hash(r):
    """Lower-case hex info-hash of a result's magnet link, or None (base32 hashes are converted)."""
    m = re.search(r"xt=urn:btih:([0-9A-Za-z]+)", r.get("magnet") or "")
    if not m:
        return None
    h = m.group(1)
    if len(h) == 32:
        import base64
        try:
            return base64.b32decode(h.upper()).hex()
        except ValueError:
            return None
    return h.lower() if len(h) == 40 else None


def merge(results, more):
    """Add `more` to `results` (in place), dropping torrents already listed under another source (same info-hash;
    the copy with more seeders stays). Results without a magnet link cannot be compared and are all kept."""
    seen = {}
    for i, r in enumerate(results):
        h = info_hash(r)
        if h:
            seen[h] = i
    for r in more:
        h = info_hash(r)
        if h and h in seen:
            old = results[seen[h]]
            if (r.get("seeders") or -1) > (old.get("seeders") or -1):
                results[seen[h]] = r
            continue
        if h:
            seen[h] = len(results)
        results.append(r)
    return results


def search_all(sources, query, unlimited=False, report=None):
    """Search every source in parallel. `report(name, results_or_None, error_or_None)` is called from worker
    threads as each one finishes. Returns (merged results, {source name: error})."""
    import threading
    lock, merged, errors = threading.Lock(), [], {}

    def one(src):
        try:
            res, err = src.search(query, unlimited=unlimited), None
        except Exception as e:
            res, err = None, str(e) or e.__class__.__name__
        with lock:
            if err:
                errors[src.name] = err
            else:
                merge(merged, res)
        if report:
            report(src.name, res, err)
    threads = [threading.Thread(target=one, args=(s,), daemon=True, name="search-" + s.name) for s in sources]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return merged, errors
