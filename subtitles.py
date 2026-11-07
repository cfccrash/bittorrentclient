"""Subtitle download from OpenSubtitles (REST API v1). Needs a free API key from opensubtitles.com (Settings); an account
login is needed by the service for downloads. `http` is injectable for tests: http(method, url, headers, body) -> dict."""
import json
import os
import re
import urllib.parse
import urllib.request

API = "https://api.opensubtitles.com/api/v1"
UA = "BitTorrentClient v1"


class SubError(Exception):
    pass


def _http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else {"raw": raw}


def clean_query(filename):
    """'Spider-Man.No.Way.Home.2021.2160p.BluRay.x265-LAMA.mkv' -> 'Spider Man No Way Home 2021'."""
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", filename)
    base = re.sub(r"[._]+", " ", base)
    m = re.search(r"\b(19|20)\d\d\b|\bS\d{1,2}E\d{1,3}\b", base, re.I)
    if m:
        base = base[:m.end()]
    return re.sub(r"[-\s]+", " ", base).strip()


def search(filename, lang, api_key, http=_http):
    """[{id, name, lang, downloads}] best first."""
    if not api_key:
        raise SubError("Add your OpenSubtitles API key in Settings > General > Media player.")
    q = urllib.parse.urlencode({"query": clean_query(filename), "languages": lang or "en"})
    d = http("GET", "%s/subtitles?%s" % (API, q), {"Api-Key": api_key, "User-Agent": UA, "Accept": "application/json"})
    out = []
    for it in d.get("data", []):
        a = it.get("attributes", {})
        files = a.get("files") or []
        if files:
            out.append(dict(id=files[0].get("file_id"), name=a.get("release") or files[0].get("file_name", ""),
                            lang=a.get("language", ""), downloads=a.get("download_count", 0)))
    out.sort(key=lambda r: -(r["downloads"] or 0))
    return out


def login(user, password, api_key, http=_http):
    d = http("POST", API + "/login", {"Api-Key": api_key, "User-Agent": UA, "Content-Type": "application/json"},
             {"username": user, "password": password})
    if not d.get("token"):
        raise SubError("OpenSubtitles login failed")
    return d["token"]


def download(file_id, dest_dir, api_key, token=None, user="", password="", http=_http, fetch=None):
    """Save the subtitle as a .srt in dest_dir; returns its path."""
    token = token or (login(user, password, api_key, http) if user else None)
    hdr = {"Api-Key": api_key, "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    d = http("POST", API + "/download", hdr, {"file_id": file_id})
    link = d.get("link")
    if not link:
        raise SubError(d.get("message") or "OpenSubtitles did not give a download link (login may be required)")
    if fetch:
        data = fetch(link)
    else:
        with urllib.request.urlopen(urllib.request.Request(link, headers={"User-Agent": UA}), timeout=30) as r:
            data = r.read()
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "sub-%s.srt" % file_id)
    with open(path, "wb") as f:
        f.write(data)
    return path
