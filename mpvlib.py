"""Minimal ctypes binding to libmpv (the engine inside mpv). No third-party Python packages.

    lib = find_library()            # path or None
    m = Mpv(wid=frame_id)           # wid: native window id to draw the video in (Tk: frame.winfo_id())
    m.command("loadfile", url); m.set("pause", "no"); m.get_double("time-pos")

libmpv is not bundled with Python; on Windows put libmpv-2.dll (or mpv-2.dll) next to the program."""
import ctypes
import ctypes.util
import json
import os
import sys

NAMES = ("libmpv-2.dll", "mpv-2.dll", "libmpv-1.dll", "mpv-1.dll") if sys.platform == "win32" else \
        ("libmpv.so.2", "libmpv.so.1", "libmpv.so", "libmpv.2.dylib", "libmpv.dylib")

FMT_STRING, FMT_FLAG, FMT_INT64, FMT_DOUBLE = 1, 3, 4, 5


def downloaded_dir():
    """Where the client keeps a libmpv it downloaded itself (see libmpv_get.py)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "BitTorrentClient", "mpv")


def search_dirs(extra=()):
    here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    dirs = list(extra) + [here, getattr(sys, "_MEIPASS", ""), os.path.join(here, "mpv"), os.path.join(here, "_internal")]
    dirs.append(downloaded_dir())
    dirs += os.environ.get("PATH", "").split(os.pathsep)
    return [d for d in dirs if d]


def find_library(custom=""):
    """Full path of libmpv, or None. `custom` may be a file or a folder the user chose."""
    if custom:
        if os.path.isfile(custom):
            return custom
        if os.path.isdir(custom):
            for n in NAMES:
                p = os.path.join(custom, n)
                if os.path.isfile(p):
                    return p
    for d in search_dirs():
        for n in NAMES:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    found = ctypes.util.find_library("mpv")
    return found or None


class MpvError(Exception):
    pass


class Mpv:
    def __init__(self, wid=None, lib=None, options=None):
        path = lib or find_library()
        if not path:
            raise MpvError("libmpv was not found")
        try:
            if sys.platform == "win32" and os.path.isabs(path) and hasattr(os, "add_dll_directory"):
                os.add_dll_directory(os.path.dirname(path))
            self.dll = ctypes.CDLL(path)
        except OSError as e:
            raise MpvError("cannot load %s: %s" % (path, e))
        d = self.dll
        d.mpv_create.restype = ctypes.c_void_p
        d.mpv_initialize.argtypes = [ctypes.c_void_p]
        d.mpv_set_option_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        d.mpv_set_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        d.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        d.mpv_get_property_string.restype = ctypes.c_void_p
        d.mpv_free.argtypes = [ctypes.c_void_p]
        d.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
        d.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
        d.mpv_wait_event.argtypes = [ctypes.c_void_p, ctypes.c_double]
        d.mpv_wait_event.restype = ctypes.c_void_p
        d.mpv_error_string.argtypes = [ctypes.c_int]
        d.mpv_error_string.restype = ctypes.c_char_p
        self.path = path
        self.h = d.mpv_create()
        if not self.h:
            raise MpvError("mpv_create failed")
        opts = {"input-default-bindings": "no", "input-vo-keyboard": "yes", "input-cursor": "yes", "osc": "no", "keep-open": "yes",
                "idle": "yes", "cache": "yes", "demuxer-max-bytes": "150MiB", "cache-pause-initial": "yes"}
        opts.update(options or {})
        if wid is not None:
            opts["wid"] = str(int(wid))
        for k, v in opts.items():
            self.option(k, v)
        r = d.mpv_initialize(self.h)
        if r < 0:
            raise MpvError("mpv_initialize: " + self._err(r))

    def _err(self, code):
        try:
            return self.dll.mpv_error_string(code).decode()
        except Exception:
            return str(code)

    def option(self, name, value):
        r = self.dll.mpv_set_option_string(self.h, name.encode(), str(value).encode())
        return r >= 0

    def command(self, *args):
        arr = (ctypes.c_char_p * (len(args) + 1))(*[str(a).encode() for a in args], None)
        r = self.dll.mpv_command(self.h, arr)
        if r < 0:
            raise MpvError("%s: %s" % (args[0], self._err(r)))

    def set(self, name, value):
        if isinstance(value, bool):
            value = "yes" if value else "no"
        return self.dll.mpv_set_property_string(self.h, name.encode(), str(value).encode()) >= 0

    def get(self, name):
        """Property as text, or None when unavailable."""
        if not self.h:
            return None
        p = self.dll.mpv_get_property_string(self.h, name.encode())
        if not p:
            return None
        try:
            return ctypes.string_at(p).decode("utf-8", "replace")
        finally:
            self.dll.mpv_free(p)

    def get_double(self, name, default=0.0):
        v = self.get(name)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def get_flag(self, name):
        return self.get(name) == "yes"

    def get_json(self, name):
        v = self.get(name + "")
        return v

    def tracks(self):
        """[{id,type,title,lang,selected}] from track-list."""
        out = []
        n = int(self.get_double("track-list/count"))
        for i in range(n):
            g = lambda k: self.get("track-list/%d/%s" % (i, k))
            out.append(dict(id=int(g("id") or 0), type=g("type"), title=g("title") or "", lang=g("lang") or "",
                            selected=g("selected") == "yes"))
        return out

    def poll_events(self):
        """Drain pending events; returns names: 'file-loaded', 'end-file', 'shutdown', 'msg:<first argument>' for
        script-message commands (used for key bindings that must reach the host window), or the numeric id."""
        names = []
        if not self.h:
            return names
        for _ in range(50):
            ev = self.dll.mpv_wait_event(self.h, 0)
            eid = ctypes.c_int.from_address(ev + 0).value
            if eid == 0:
                break
            if eid == 16:                                       # MPV_EVENT_CLIENT_MESSAGE
                try:
                    data = ctypes.c_void_p.from_address(ev + 16).value
                    n = ctypes.c_int.from_address(data).value
                    argv = ctypes.c_void_p.from_address(data + ctypes.sizeof(ctypes.c_void_p)).value
                    first = ctypes.c_char_p.from_address(argv).value if n > 0 and argv else b""
                    names.append("msg:" + (first or b"").decode("utf-8", "replace"))
                except Exception:
                    names.append("16")
                continue
            names.append({7: "end-file", 8: "file-loaded", 1: "shutdown", 21: "restart"}.get(eid, str(eid)))
            if eid == 1:
                break
        return names

    def chapters(self):
        """[(time, title)] from chapter-list."""
        n = int(self.get_double("chapter-list/count"))
        return [(self.get_double("chapter-list/%d/time" % i), self.get("chapter-list/%d/title" % i) or "Chapter %d" % (i + 1))
                for i in range(n)]

    def close(self):
        h, self.h = self.h, None
        if h:
            try:
                self.dll.mpv_terminate_destroy(h)
            except Exception:
                pass
