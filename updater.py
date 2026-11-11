"""Automatic updates from GitHub Releases.

Installers are published to a separate *public* repository that holds only releases (the source repository stays
private; its build pushes each release there with a token kept in GitHub's secrets, never in the app). The app asks
that repository for its latest release, downloads the Setup .exe into the data folder, checks its size and SHA-256
(GitHub publishes a digest for every asset), and runs it silently; the installer closes this copy with --quit and
starts the new one afterwards (/RELAUNCH)."""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

RELEASES_REPO = "cfccrash/bittorrentclient-releases"
API = "https://api.github.com/repos/%s/releases/latest"
UA = {"User-Agent": "BitTorrentClient-updater", "Accept": "application/vnd.github+json"}
ASSET = re.compile(r"^BitTorrentClient-Setup-[\w.\-]+\.exe$", re.I)
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")


def ver(text):
    """'v1.9.21' -> (1, 9, 21)"""
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:4])


def latest(repo=RELEASES_REPO, get=None):
    """The newest release as {"version", "url", "size", "sha256", "page"} - or None when it has no Setup .exe."""
    if get is None:
        def get(url):
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
    d = get(API % repo)
    for a in d.get("assets") or []:
        if ASSET.match(a.get("name") or ""):
            digest = a.get("digest") or ""
            return {"version": str(d.get("tag_name") or "").lstrip("vV"), "url": a.get("browser_download_url") or "",
                    "size": int(a.get("size") or 0), "page": d.get("html_url") or "",
                    "sha256": digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None}
    return None


def is_newer(rel, current):
    return bool(rel) and ver(rel["version"]) > ver(current)


def _host_ok(url):
    m = re.match(r"https://([^/]+)/", url or "")
    return bool(m) and m.group(1).lower() in ALLOWED_HOSTS


def download(rel, folder, progress=None, opener=None):
    """Fetch the installer into `folder` (resumable: an already complete, verified file is reused). Returns its path.
    Raises ValueError when the file does not match the published size / SHA-256."""
    if not _host_ok(rel["url"]):
        raise ValueError("unexpected download address: %s" % rel["url"])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "BitTorrentClient-Setup-%s.exe" % re.sub(r"[^\w.\-]", "_", rel["version"]))
    if os.path.isfile(path) and verify(path, rel):
        return path
    tmp = path + ".part"
    opener = opener or (lambda url: urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]}), timeout=60))
    h, done = hashlib.sha256(), 0
    with opener(rel["url"]) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, rel["size"])
    if (rel["size"] and done != rel["size"]) or (rel["sha256"] and h.hexdigest() != rel["sha256"]):
        os.remove(tmp)
        raise ValueError("the downloaded installer does not match the release (size or checksum) - not using it")
    os.replace(tmp, path)
    return path


def verify(path, rel):
    if rel["size"] and os.path.getsize(path) != rel["size"]:
        return False
    if rel["sha256"]:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest() == rel["sha256"]
    return True


def can_self_update():
    """Only an installed copy can update itself (not one run from the source code or a portable folder)."""
    return bool(getattr(sys, "frozen", False)) and os.path.isfile(os.path.join(os.path.dirname(sys.executable), "unins000.exe"))


def all_users():
    """Installed under Program Files (for all users) -> the update needs /ALLUSERS (Windows asks for permission)."""
    pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    exe = os.path.normcase(os.path.abspath(sys.executable))
    return any(p and exe.startswith(os.path.normcase(os.path.abspath(p)) + os.sep) for p in pf)


def install_command(path, relaunch=True):
    cmd = [path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/ALLUSERS" if all_users() else "/CURRENTUSER",
           "/LOG=" + os.path.join(os.path.dirname(os.path.abspath(path)), "install.log")]   # next to the download
    return cmd + (["/RELAUNCH=1"] if relaunch else [])


def run_installer(path, relaunch=True):
    """Start the installer detached; it closes this app (--quit) and, with `relaunch`, starts the new version."""
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(install_command(path, relaunch), close_fds=True, creationflags=flags)
