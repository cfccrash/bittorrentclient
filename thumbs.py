"""Picture grabbing with a second, invisible libmpv: seek-bar preview thumbnails and JPEG->PNG conversion for posters.
Only one job runs at a time, the newest request wins, and results are cached."""
import hashlib
import os
import queue
import tempfile
import threading
import time

import mpvlib

OPTS = {"vo": "null", "ao": "null", "audio": "no", "hr-seek": "no", "vf": "scale=160:-2", "pause": "yes", "osc": "no",
        "cache": "no", "sub": "no", "input-default-bindings": "no", "input-vo-keyboard": "no", "idle": "yes",
        "demuxer-max-bytes": "8MiB", "keep-open": "yes"}


class Grabber:
    def __init__(self, lib=None, width=160):
        self.lib, self.width = lib, width
        self.dir = tempfile.mkdtemp(prefix="btc-thumbs-")
        self.q = queue.Queue()
        self.mpv = None
        self.url = None
        self.cache = {}
        self.dead = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self, url, t, callback, bucket=5.0):
        """Ask for the frame at `t` seconds of `url`; callback(path or None) is called from the worker thread."""
        key = (url, int(t // bucket))
        if key in self.cache:
            callback(self.cache[key])
            return
        while True:                                   # only the newest request matters
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.q.put((url, key[1] * bucket + bucket / 2, key, callback))

    def _wait(self, ev, timeout):
        end = time.time() + timeout
        while time.time() < end and not self.dead:
            if ev in self.mpv.poll_events():
                return True
            time.sleep(0.01)
        return False

    def _run(self):
        while not self.dead:
            try:
                url, t, key, cb = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            path = None
            try:
                if self.mpv is None:
                    o = dict(OPTS); o["vf"] = "scale=%d:-2" % self.width
                    self.mpv = mpvlib.Mpv(lib=self.lib, options=o)
                if url != self.url:
                    self.mpv.command("loadfile", url)
                    self.url = url
                    if not self._wait("file-loaded", 6):
                        self.url = None
                        raise RuntimeError("load")
                self.mpv.command("seek", "%.2f" % t, "absolute+keyframes")
                if self._wait("restart", 3):
                    path = os.path.join(self.dir, "%s-%d.png" % (hashlib.md5(url.encode()).hexdigest()[:8], key[1]))
                    self.mpv.command("screenshot-to-file", path, "video")
                    if not os.path.isfile(path):
                        path = None
                    else:
                        self.cache[key] = path
            except Exception:
                path = None
            try:
                cb(path)
            except Exception:
                pass

    def convert_image(self, src, dest):
        """Any image libmpv can read (JPEG posters) -> PNG that Tk can show. Returns True on success."""
        try:
            m = mpvlib.Mpv(lib=self.lib, options={"vo": "null", "ao": "null", "audio": "no", "pause": "yes", "osc": "no",
                                                  "image-display-duration": "inf", "vf": "", "cache": "no"})
        except mpvlib.MpvError:
            return False
        try:
            m.command("loadfile", src)
            end = time.time() + 5
            ok = False
            while time.time() < end:
                ev = m.poll_events()
                if "file-loaded" in ev:
                    ok = True
                    break
                time.sleep(0.01)
            if not ok:
                return False
            time.sleep(0.15)
            m.command("screenshot-to-file", dest, "video")
            return os.path.isfile(dest)
        except Exception:
            return False
        finally:
            m.close()

    def close(self):
        self.dead = True
        m, self.mpv = self.mpv, None
        if m:
            m.close()


def png_for(path, lib=None):
    """Path of a PNG version of an image (JPEG etc.) for Tk, using Windows' own imaging (PowerShell) or libmpv; None if
    neither is available. PNG/GIF files are returned unchanged."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"\x89PNG") or head[:3] == b"GIF":
        return path
    out = path + ".png"
    if os.path.isfile(out):
        return out
    if os.name == "nt":
        import subprocess
        ps = ("Add-Type -AssemblyName System.Drawing; $i=[System.Drawing.Image]::FromFile('%s'); $i.Save('%s',"
              "[System.Drawing.Imaging.ImageFormat]::Png); $i.Dispose()" % (path.replace("'", "''"), out.replace("'", "''")))
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=30,
                           creationflags=0x08000000)
            if os.path.isfile(out):
                return out
        except Exception:
            pass
    if mpvlib.find_library(lib or ""):
        g = Grabber(lib or None)
        try:
            if g.convert_image(path, out):
                return out
        finally:
            g.close()
    return None
