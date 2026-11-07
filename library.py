"""A library view of what you can watch: titles guessed from torrent / file names, optional TMDB details and posters,
"continue watching" from media_state. TMDB needs a free API key (Settings); without one you still get a tidy list."""
import json
import os
import re
import urllib.parse
import urllib.request

TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w185"
NOISE = r"(2160p|1080p|720p|480p|4k|uhd|hdr10?|bluray|blu-ray|brrip|bdrip|web-?dl|webrip|hdtv|dvdrip|x26[45]|h\.?26[45]|hevc|10bit|aac\d?(\.\d)?|ddp?\d?(\.\d)?|dts|atmos|remux|proper|repack|extended|unrated|yify|yts|rarbg|amzn|nf|hmax)"


def parse_title(name):
    """-> dict(title, year, season, episode, kind) from a release name."""
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name) if re.search(r"\.[A-Za-z0-9]{2,4}$", name) else name
    base = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", base)
    base = re.sub(r"[._]+", " ", base)
    season = episode = None
    m2 = None
    m = re.search(r"\bS(\d{1,2})\s?E(\d{1,3})\b", base, re.I) or re.search(r"\b(\d{1,2})x(\d{2,3})\b", base)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    else:
        m2 = re.search(r"\bS(?:eason)?\s?(\d{1,2})\b", base, re.I)
        if m2:
            season = int(m2.group(1))
    year = None
    ym = re.search(r"\b(19[3-9]\d|20[0-3]\d)\b", base)
    cut = len(base)
    for mm in (m, m2, ym, re.search(r"\b" + NOISE + r"\b", base, re.I)):
        if mm and mm.start() > 2:
            cut = min(cut, mm.start())
    if ym:
        year = int(ym.group(1))
    title = re.sub(r"[-\s]+", " ", base[:cut]).strip(" -")
    return dict(title=title or name, year=year, season=season, episode=episode,
                kind="tv" if season is not None else "movie")


class Meta:
    """TMDB lookups cached in <data_dir>/library.json; posters in <data_dir>/posters/. `get` is injectable."""

    def __init__(self, data_dir, api_key="", get=None):
        self.dir, self.key = data_dir, api_key
        self.get = get or self._get
        self.path = os.path.join(data_dir, "library.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "BitTorrentClient"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
        except OSError:
            pass

    def lookup(self, info):
        """Details for a parsed title, or None (no key / not found). Cached by title+year."""
        k = "%s|%s|%s" % (info["kind"], info["title"].lower(), info.get("year") or "")
        if k in self.cache:
            return self.cache[k] or None
        if not self.key:
            return None
        q = {"api_key": self.key, "query": info["title"]}
        if info.get("year") and info["kind"] == "movie":
            q["year"] = info["year"]
        url = "%s/search/%s?%s" % (TMDB, "tv" if info["kind"] == "tv" else "movie", urllib.parse.urlencode(q))
        try:
            res = json.loads(self.get(url)).get("results") or []
        except Exception:
            return None                                       # network trouble: do not cache the miss
        if not res:
            self.cache[k] = {}
            self.save()
            return None
        r = res[0]
        det = dict(title=r.get("title") or r.get("name") or info["title"], overview=r.get("overview", ""),
                   year=(r.get("release_date") or r.get("first_air_date") or "")[:4], rating=r.get("vote_average"),
                   poster=r.get("poster_path") or "")
        self.cache[k] = det
        self.save()
        return det

    def poster_file(self, det):
        """Local path of the poster (downloaded once), or None."""
        if not det or not det.get("poster"):
            return None
        d = os.path.join(self.dir, "posters")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, det["poster"].strip("/"))
        if not os.path.isfile(p):
            try:
                data = self.get(IMG + det["poster"])
                with open(p, "wb") as f:
                    f.write(data)
            except Exception:
                return None
        return p


def entries(sessions, state):
    """Library rows from the engine's sessions: one per torrent that has video/audio files.
    sessions: [(hash, name, [(path_tuple, size)])]; state: media_state dict. Returns list of dicts."""
    import stream
    rows = []
    for h, name, files in sessions:
        media = [i for i, (p, _s) in enumerate(files) if stream.media_kind(p[-1]) and not stream.is_sample(p)]
        if not media:
            continue
        watched = sum(1 for i in media if (state.get("%s:%d" % (h, i)) or {}).get("watched"))
        part = [(i, state.get("%s:%d" % (h, i))) for i in media]
        resume = max((e for _i, e in part if e and e.get("pos", 0) >= 20 and not e.get("watched")),
                     key=lambda e: e.get("at", 0), default=None)
        rows.append(dict(hash=h, name=name, info=parse_title(name), files=len(media), watched=watched, resume=resume,
                         kind=stream.media_kind(files[media[0]][0][-1])))
    rows.sort(key=lambda r: r["info"]["title"].lower())
    return rows
