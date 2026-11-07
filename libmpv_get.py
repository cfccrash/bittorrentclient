"""Download libmpv (the player engine) for the built-in player, with the user's permission.

Looks up the newest Windows build on GitHub, downloads the "mpv-dev" archive (LGPL build preferred), unpacks it with
the tar that ships with Windows 10/11 (or the zip module / 7-Zip) and keeps libmpv-2.dll in
%APPDATA%\\BitTorrentClient\\mpv. Network access is injectable so it can be tested."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import mpvlib

REPOS = ("shinchiro/mpv-winbuild-cmake", "zhongfly/mpv-winbuild")
DLL_NAMES = ("libmpv-2.dll", "mpv-2.dll")


class GetError(Exception):
    pass


def pick_asset(assets):
    """Best libmpv archive among a release's assets: 64-bit 'dev' package, generic CPU build first (the v3 build needs
    AVX2, used only if nothing else exists), LGPL first, zip before 7z."""
    best = None
    for a in assets:
        n = a.get("name", "")
        m = re.match(r"^(?:lib)?mpv-dev-(lgpl-)?(?:x86_64|x64)(-v3)?-\d.*\.(7z|zip)$", n)
        if not m:
            continue
        score = (0 if m.group(2) else 2, 1 if m.group(1) else 0, 1 if m.group(3) == "zip" else 0)
        if best is None or score > best[0]:
            best = (score, a)
    return best[1] if best else None


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "BitTorrentClient", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _http_download(url, path, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": "BitTorrentClient"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            b = r.read(256 * 1024)
            if not b:
                break
            f.write(b)
            done += len(b)
            if progress:
                progress(done, total)


def extract(archive, dest):
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return
    for cmd in (["tar", "-xf", archive, "-C", dest], ["bsdtar", "-xf", archive, "-C", dest],
                ["7z", "x", "-y", "-o" + dest, archive], ["7za", "x", "-y", "-o" + dest, archive]):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, creationflags=0x08000000 if os.name == "nt" else 0)
            if r.returncode == 0:
                return
    raise GetError("could not unpack the 7z archive (need Windows 10/11 tar, or 7-Zip)")


def find_dll(root):
    for base, _dirs, files in os.walk(root):
        for n in DLL_NAMES:
            if n in files:
                return os.path.join(base, n)
    return None


def install(progress=None, fetch_json=_http_json, download=_http_download, dest_dir=None, repos=REPOS):
    """Download + unpack; returns the path of the installed DLL. progress(done_bytes, total_bytes) is optional."""
    dest_dir = dest_dir or mpvlib.downloaded_dir()
    asset, last = None, None
    for repo in repos:
        try:
            rel = fetch_json("https://api.github.com/repos/%s/releases/latest" % repo)
            asset = pick_asset(rel.get("assets", []))
            if asset:
                break
        except Exception as e:
            last = e
    if not asset:
        raise GetError("could not find a libmpv download%s" % (" (%s)" % last if last else ""))
    tmp = tempfile.mkdtemp(prefix="libmpv-")
    try:
        arc = os.path.join(tmp, asset["name"])
        download(asset["browser_download_url"], arc, progress)
        out = os.path.join(tmp, "x")
        os.makedirs(out)
        extract(arc, out)
        dll = find_dll(out)
        if not dll:
            raise GetError("the download did not contain libmpv-2.dll")
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, os.path.basename(dll))
        shutil.copyfile(dll, target)
        return target
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # build-time use:  python libmpv_get.py <folder>   (build.bat / the GitHub workflow bundle libmpv into the program folder)
    import sys
    if len(sys.argv) != 2:
        print("usage: python libmpv_get.py <destination folder>")
        sys.exit(2)
    try:
        print("libmpv bundled:", install(dest_dir=sys.argv[1], progress=lambda d, t: None))
    except Exception as e:
        print("could not download libmpv:", e)
        sys.exit(1)
