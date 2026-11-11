"""Media player built on libmpv. Works on its own (python player.py [file-or-url]) and is what the torrent client opens
for "Play": it can show which parts of a streaming file are already downloaded on its seek bar.

    python player.py movie.mkv
    python player.py http://127.0.0.1:port/s/... --map http://127.0.0.1:port/map/... --title "Movie"
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import chrome
import mpvlib
import ui
import thumbs
import media_state
import subtitles as subs_mod
import time

SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
# (menu label, video-aspect-override, panscan, keepaspect): original shape, forced shapes, crop to fill, stretch to fill
ASPECTS = (("Original", "-1", 0, True), ("16:9", "16:9", 0, True), ("4:3", "4:3", 0, True), ("21:9", "21:9", 0, True),
           ("2.35:1", "2.35:1", 0, True), ("1:1", "1:1", 0, True), ("Fill (crop edges)", "-1", 1, True),
           ("Stretch to window", "-1", 0, False))
BG, FG, ACC, BUF = "#101216", "#e6e8ec", "#4c9aff", "#6b7488"
OSD_BG, TRACK, MUTED = "#0b0d12", "#2a2f3a", "#9aa3b2"
BAR_H = 84                     # height of the control strip (96 dpi pixels)
UP_NEXT_S = 15                 # the "Up next" card shows this many seconds before the end
ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
ICON = dict(play="\uE768", pause="\uE769", stop="\uE71A", prev="\uE892", next="\uE893", back="\uED3C",
            fwd="\uED3D", vol="\uE767", mute="\uE74F", full="\uE740", unfull="\uE73F", subs="\uED1E",
            chapters="\uE8FD", aspect="\uE799", camera="\uE722", mini="\uEE49", detach="\uE8A7", attach="\uEE47")
PREFS_FILE = "track_prefs.json"
GLYPH_NAME = {v: k for k, v in ICON.items()}


def draw_glyph(cv, name, cx, cy, size, col, bold=False):
    """The player's icons drawn with lines and shapes - used where the Windows icon fonts are missing (Linux)."""
    h = size / 2.0
    w = max(1.5, size / 10.0) * (1.6 if bold else 1.0)
    L = lambda *pts, **kw: cv.create_line(*pts, fill=col, width=w, capstyle="round", joinstyle="round", **kw)
    P = lambda *pts: cv.create_polygon(*pts, outline=col, fill="", width=w, joinstyle="round")
    if name == "play":
        P(cx - h * .55, cy - h * .8, cx + h * .8, cy, cx - h * .55, cy + h * .8)
    elif name == "pause":
        for dx in (-h * .4, h * .4):
            cv.create_rectangle(cx + dx - h * .15, cy - h * .75, cx + dx + h * .15, cy + h * .75, outline=col, width=w)
    elif name == "stop":
        cv.create_rectangle(cx - h * .65, cy - h * .65, cx + h * .65, cy + h * .65, outline=col, width=w)
    elif name in ("prev", "next"):
        s = -1 if name == "prev" else 1
        P(cx - s * h * .55, cy - h * .7, cx + s * h * .45, cy, cx - s * h * .55, cy + h * .7)
        L(cx + s * h * .7, cy - h * .7, cx + s * h * .7, cy + h * .7)
    elif name in ("back", "fwd"):
        s = -1 if name == "back" else 1               # an arc over the number, arrowhead on the leading end
        ox, oy, rx, ry = cx, cy - h * .05, h * .72, h * .62
        cv.create_arc(ox - rx, oy - ry, ox + rx, oy + ry, start=15, extent=150, style="arc", outline=col, width=w)
        ex = ox + s * rx * .966
        ey = oy - ry * .259
        L(ex - h * .28, ey - h * .12, ex, ey + h * .14, ex + h * .26, ey - h * .14)
        cv.create_text(cx, cy + h * .5, text="10" if s < 0 else "30", fill=col,
                       font=("TkDefaultFont", max(6, int(size * .26)), "bold"))
    elif name in ("vol", "mute"):
        P(cx - h * .85, cy - h * .3, cx - h * .45, cy - h * .3, cx - h * .05, cy - h * .7,
          cx - h * .05, cy + h * .7, cx - h * .45, cy + h * .3, cx - h * .85, cy + h * .3)
        if name == "vol":
            for r in (.4, .75):
                cv.create_arc(cx - h * r, cy - h * r, cx + h * r, cy + h * r, start=-45, extent=90, style="arc",
                              outline=col, width=w)
        else:
            L(cx + h * .25, cy - h * .3, cx + h * .85, cy + h * .3)
            L(cx + h * .25, cy + h * .3, cx + h * .85, cy - h * .3)
    elif name in ("full", "unfull"):
        k = h * .35
        for sx in (-1, 1):
            for sy in (-1, 1):
                if name == "full":
                    x, y = cx + sx * h * .75, cy + sy * h * .75
                    L(x, y - sy * k, x, y, x - sx * k, y)
                else:
                    x, y = cx + sx * h * .25, cy + sy * h * .25
                    L(x + sx * k, y, x, y, x, y + sy * k)
    elif name == "subs":
        cv.create_rectangle(cx - h * .85, cy - h * .6, cx + h * .85, cy + h * .6, outline=col, width=w)
        L(cx - h * .5, cy + h * .15, cx + h * .1, cy + h * .15)
        L(cx + h * .3, cy + h * .15, cx + h * .5, cy + h * .15)
        L(cx - h * .5, cy - h * .15, cx - h * .2, cy - h * .15)
    elif name == "chapters":
        for dy in (-.55, 0, .55):
            cv.create_oval(cx - h * .8 - w, cy + dy * h - w, cx - h * .8 + w, cy + dy * h + w, fill=col, outline="")
            L(cx - h * .45, cy + dy * h, cx + h * .8, cy + dy * h)
    elif name == "aspect":
        cv.create_rectangle(cx - h * .85, cy - h * .6, cx + h * .85, cy + h * .6, outline=col, width=w)
        L(cx - h * .5, cy - h * .25, cx - h * .5, cy + h * .25)
        L(cx + h * .5, cy - h * .25, cx + h * .5, cy + h * .25)
    elif name == "camera":
        cv.create_rectangle(cx - h * .85, cy - h * .45, cx + h * .85, cy + h * .7, outline=col, width=w)
        L(cx - h * .3, cy - h * .45, cx - h * .15, cy - h * .75, cx + h * .15, cy - h * .75, cx + h * .3, cy - h * .45)
        cv.create_oval(cx - h * .32, cy - h * .2, cx + h * .32, cy + h * .44, outline=col, width=w)
    elif name == "mini":
        cv.create_rectangle(cx - h * .85, cy - h * .65, cx + h * .85, cy + h * .65, outline=col, width=w)
        cv.create_rectangle(cx + h * .05, cy + h * .05, cx + h * .6, cy + h * .4, outline=col, fill=col, width=1)
    elif name in ("detach", "attach"):
        cv.create_rectangle(cx - h * .8, cy - h * .5, cx + h * .4, cy + h * .8, outline=col, width=w)
        if name == "detach":
            L(cx - h * .05, cy + h * .15, cx + h * .8, cy - h * .7)
            L(cx + h * .25, cy - h * .7, cx + h * .8, cy - h * .7, cx + h * .8, cy - h * .15)
        else:
            L(cx + h * .85, cy - h * .75, cx - h * .05, cy + h * .15)
            L(cx - h * .05, cy - h * .35, cx - h * .05, cy + h * .15, cx + h * .45, cy + h * .15)



def _mix(a, b, t):
    """Colour between a and b (t = 0..1 towards b)."""
    a, b = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _elide(text, font, room):
    if room <= 0 or font.measure(text) <= room:
        return text
    while text and font.measure(text + "\u2026") > room:
        text = text[:-1]
    return text + "\u2026"


_x11_handler = []


def _x11_errors_not_fatal():
    """On X11 the video engine creates and destroys its own window inside one of ours; Tk can then ask the X server
    about a window that just went away. Xlib's default answer to that BadWindow error is to end the program - ignore
    BadWindow / BadDrawable instead (Linux only, installed once)."""
    if not sys.platform.startswith("linux") or _x11_handler:
        return
    try:
        import ctypes
        import ctypes.util

        class XErrorEvent(ctypes.Structure):
            _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p), ("resourceid", ctypes.c_ulong),
                        ("serial", ctypes.c_ulong), ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                        ("minor_code", ctypes.c_ubyte)]
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(XErrorEvent))
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        prev = []

        def handler(display, ev):
            if ev.contents.error_code in (3, 9):            # BadWindow, BadDrawable: a window that is already gone
                return 0
            return HANDLER(prev[0])(display, ev) if prev and prev[0] else 0
        cb = HANDLER(handler)
        prev.append(x11.XSetErrorHandler(ctypes.cast(cb, ctypes.c_void_p)))
        _x11_handler.append(cb)                             # keep the callback alive
    except Exception:
        pass


class _Volume:
    """The volume (0-130) the drawn slider shows; set() also changes mpv's volume."""

    def __init__(self, panel):
        self.p, self.v = panel, 80

    def get(self):
        return self.v

    def set(self, v, push=True):
        self.v = max(0, min(130, int(round(float(v)))))
        if push:
            self.p._vol(self.v)
        self.p._redraw()

    def configure(self, **kw):
        pass


def series_key(entry):
    """What 'the same series' means for remembering audio / subtitle choices: the show name before SxxEyy in the
    title, else the torrent (all files of one torrent)."""
    t = (entry or {}).get("title", "")
    m = re.match(r"(.+?)[\s._\-\[(]*[Ss]\d{1,2}[\s._-]*[Ee]\d{1,3}", t)
    if m:
        return "s:" + re.sub(r"[\s._-]+", " ", m.group(1)).strip().lower()
    k = (entry or {}).get("key", "")
    return ("t:" + k.split(":")[0]) if k else None


def load_prefs(state_dir):
    try:
        with open(os.path.join(state_dir, PREFS_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def save_prefs(state_dir, prefs):
    if not state_dir:
        return
    try:
        p = os.path.join(state_dir, PREFS_FILE)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        os.replace(p + ".tmp", p)
    except OSError:
        pass


def fmt_time(s):
    s = max(0, int(s or 0))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


class NoPlayer(Exception):
    """No video player (VLC, mpv) was found on this computer."""


def find_external():
    cands = ["mpv", "vlc"]
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            cands += [os.path.join(base, "VideoLAN", "VLC", "vlc.exe"), os.path.join(base, "mpv", "mpv.exe"),
                      os.path.join(base, "K-Lite Codec Pack", "MPC-HC64", "mpc-hc64.exe"),     # installed by K-Lite
                      os.path.join(base, "K-Lite Codec Pack", "MPC-HC", "mpc-hc.exe"),
                      os.path.join(base, "MPC-HC", "mpc-hc64.exe"), os.path.join(base, "MPC-BE x64", "mpc-be64.exe")]
        cands.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "mpv.exe"))
    for c in cands:
        exe = c if os.path.isabs(c) and os.path.isfile(c) else shutil.which(c)
        if exe:
            return exe
    return None


def external_launch(target, exe=None, wid=None, tab=False):
    """Start the player program `exe` (chosen in Settings), else VLC / mpv / MPC-HC. Returns (process, name); raises NoPlayer.
    tab=True adds the options that suit showing it inside the main window (see embed.launch_args)."""
    exe = exe if exe and os.path.isfile(exe) else find_external()
    if not exe:
        raise NoPlayer("no VLC or mpv found")
    if tab:
        import embed
        cmd = embed.launch_args(exe, target, wid)
    else:
        cmd = [exe, target]
    return subprocess.Popen(cmd, close_fds=True), os.path.basename(exe)


def external_player(target, exe=None):
    """Hand a file/URL to an external player. Raises NoPlayer if none."""
    return external_launch(target, exe)[1]


def default_player(target):
    """Open a stream with whatever Windows uses for playlists (never a web browser). Returns a description."""
    import tempfile
    pl = os.path.join(tempfile.gettempdir(), "BitTorrentClient-stream.m3u")
    with open(pl, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n%s\n" % target)
    if sys.platform == "win32":
        os.startfile(pl)
        return "the default media app"
    opener = shutil.which("xdg-open") or shutil.which("open")
    if not opener:
        raise NoPlayer("no player found")
    subprocess.Popen([opener, pl])
    return os.path.basename(opener)


def install_vlc():
    """Install VLC with winget in a visible console window."""
    if not shutil.which("winget"):
        raise NoPlayer("winget is not available")
    subprocess.Popen(["winget", "install", "-e", "--id", "VideoLAN.VLC", "--accept-source-agreements",
                      "--accept-package-agreements"], creationflags=0x00000010)      # CREATE_NEW_CONSOLE


HELP = """Space, K / click   Pause / play
Left, Right        Back / forward 5 s   (Shift: 30 s)
J, L               Back / forward 10 s
0 - 9              Jump to 0 % - 90 % of the video
Up, Down / wheel   Volume
PageUp, PageDown   Previous / next chapter
N, P               Next / previous file in the queue
A                  Next aspect ratio (or the Aspect menu)
S                  Stop (Play starts it again)
M                  Mute
Z, X               Subtitle delay -/+ 0.1 s
Shift+Z, Shift+X   Audio delay -/+ 0.1 s (sound out of sync)
, and .            Subtitle size smaller / larger
Shift+S            Save a picture of the frame (Pictures)
F, Esc             Full screen / leave   (double-click too)
Ctrl+O             Open a file or link
?, F1              This help"""


class PlayerPanel:
    """The whole player (video + controls) inside any Tk container: a window of its own or a tab of the main window.

    queue: list of dicts {url, title, key, map, hint, prefetch, subs[]} played in order (the next one is fetched in
    advance); `session` carries settings (see main()). on_fullscreen(on) / on_popout() let the host decide what the
    Full button does."""

    stopped = False

    def __init__(self, parent, top, queue=(), index=0, session=None, lib=None, options=None, on_fullscreen=None,
                 on_popout=None, on_end=None, key_widget=None, popout_label="Detach", on_mini=None, key_scope=None,
                 mini_tip="Mini player (stays on top in a corner)", is_full=None):
        self.popout_label = popout_label
        self.on_mini, self.mini_tip, self.is_full = on_mini, mini_tip, is_full
        self.key_scope = key_scope         # fn() -> bool: may the shortcuts act now (the Player tab is showing)?
        self.next_cancelled = False
        self._last_pos = 0.0
        self._eof_done = False
        self._afters = set()
        self.parent, self.top = parent, top
        self.sess = session or {}
        self.queue, self.index = list(queue), index
        self.on_fullscreen, self.on_popout, self.on_end = on_fullscreen, on_popout, on_end
        self.state_dir = self.sess.get("state_dir") or ""
        self.mpv = None
        self.seeking = False
        self.dur = 0.0
        self.ranges, self.size = [], 0
        self.chapters = []
        self.cur = None
        self.rate_hist = []
        self.net_note = ""
        self.prefetched = False
        self._saved = 0.0
        self._resume_to = 0.0
        self.hover_x = None
        self.grabber = None
        self.thumb_win = None
        self._thumb_img = None
        self._thumb_t = -1
        self._build()
        opts = dict(options or {})
        o = self.sess.get("opts", {})
        if o.get("hwdec", True):
            opts.setdefault("hwdec", "auto-safe")
        opts.setdefault("window-dragging", "no")   # a click on the video is play/pause, not "move mpv's window"
        if os.environ.get("PLAYER_VO"):             # (tests / CI: a video output that works on a virtual display)
            opts.setdefault("vo", os.environ["PLAYER_VO"])
        _x11_errors_not_fatal()
        for k, key in (("alang", "alang"), ("slang", "slang")):
            if o.get(k):
                opts.setdefault(key, o[k])
        try:
            self.mpv = mpvlib.Mpv(wid=self.video.winfo_id(), lib=lib, options=opts)
        except mpvlib.MpvError as e:
            self.status.set(str(e))
            self.error = str(e)
            return
        self.error = None
        # Keys and clicks while the video window itself has the focus (it is a separate native window, so Tk never sees them).
        # (mouse clicks / wheel on the video are handled by Tk - see _build; mpv does not get them when embedded)
        binds = [("f", "script-message tk-fs"), ("ESC", "script-message tk-fs-off"),
                 ("n", "script-message tk-next"), ("p", "script-message tk-prev"), ("?", "script-message tk-help"),
                 ("F1", "script-message tk-help"), ("SPACE", "cycle pause"),
                 ("LEFT", "seek -5"), ("RIGHT", "seek 5"), ("Shift+LEFT", "seek -30"), ("Shift+RIGHT", "seek 30"),
                 ("UP", "add volume 5"), ("DOWN", "add volume -5"),
                 ("PGUP", "add chapter -1"), ("PGDWN", "add chapter 1"), ("m", "cycle mute"), ("z", "add sub-delay -0.1"),
                 ("x", "add sub-delay 0.1"), ("a", "script-message tk-aspect"), ("s", "script-message tk-stop"), (",", "add sub-scale -0.1"), (".", "add sub-scale 0.1"),
                 ("j", "seek -10"), ("k", "cycle pause"), ("l", "seek 10"), ("S", "script-message tk-shot"),
                 ("Z", "add audio-delay -0.1"), ("X", "add audio-delay 0.1")] + \
                [(str(n), "seek %d absolute-percent" % (n * 10)) for n in range(10)]
        if sys.platform != "win32":       # on X11 mpv's video window does get the mouse (Tk never sees those clicks)
            binds += [("MBTN_LEFT", "cycle pause"), ("MBTN_LEFT_DBL", "script-message tk-fs"),
                      ("WHEEL_UP", "add volume 5"), ("WHEEL_DOWN", "add volume -5")]
        for key, cmd in binds:
            try:
                self.mpv.command("keybind", key, cmd)
            except mpvlib.MpvError:
                pass
        # key_widget "all": the player sits in a tab of the main window - its keys work wherever the keyboard is in
        # that window while the tab shows (key_scope), except in text fields
        self.bind_keys(key_widget or self.top, everywhere=key_widget == "all")
        self.bind_keys(self.osd)
        if self.queue:
            self.play_index(self.index)
        self._tick()
        threading.Thread(target=self._map_loop, daemon=True).start()

    # ---- layout: the controls float over the bottom of the video ------------------------------------------------
    def px(self, n):
        return int(round(n * self.scale))

    def _build(self):
        p = self.parent
        try:
            self.scale = max(1.0, p.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self.scale = 1.0
        self.video = tk.Frame(p, bg="black", highlightthickness=0)
        self.video.pack(fill="both", expand=True)
        # the mouse on the video: click = play / pause, double-click = full screen, wheel = volume
        self.video.bind("<Button-1>", lambda e: (self.toggle(), self._wake()))
        self.video.bind("<Double-Button-1>", lambda e: self.fullscreen())
        self.video.bind("<MouseWheel>", lambda e: (self.vol.set(self.vol.get() + (5 if e.delta > 0 else -5)), self._wake()))
        families = set(tkfont.families(p))
        icon_family = next((f for f in ICON_FONTS if f in families), None)
        self.vector_icons = icon_family is None             # no Windows icon font (Linux): draw the icons instead
        ui_family = "Segoe UI" if "Segoe UI" in families else tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_icon = tkfont.Font(p, family=icon_family or ui_family, size=13)
        self.f_big = tkfont.Font(p, family=icon_family or ui_family, size=17)
        self.f_ui = tkfont.Font(p, family=ui_family, size=10)
        self.f_bold = tkfont.Font(p, family=ui_family, size=10, weight="bold")
        self.f_small = tkfont.Font(p, family=ui_family, size=8)
        # A borderless strip above mpv's native video window, slightly see-through: Tk widgets cannot sit *on* the
        # video, but a window of its own can. It never takes the keyboard focus (WS_EX_NOACTIVATE).
        osd = self.osd = tk.Toplevel(p)
        osd.withdraw()
        osd.overrideredirect(True)
        try:
            osd.transient(p.winfo_toplevel())
            osd.attributes("-alpha", 0.9)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(osd, bg=OSD_BG, highlightthickness=0, bd=0, height=self.px(BAR_H))
        self.cv.pack(fill="both", expand=True)
        self.seek = self.cv                       # (older name: the seek bar is the top row of this canvas)
        self.bar = osd
        self._items, self._hot, self._down, self._drag = [], None, None, None
        self._osd_on, self._last_move, self._last_ptr = False, time.time(), None
        self.status, self.time = tk.StringVar(), tk.StringVar(value="0:00 / 0:00")
        self.status.trace_add("write", lambda *a: self._redraw())
        self.vol = _Volume(self)
        self._speed_label, self._aspect_name = "1x", "Original"
        self.aspect_i = tk.IntVar(value=0)
        self.night = tk.BooleanVar(value=False)
        self.tracks_menu = tk.Menu(self.cv, tearoff=0)
        self.chap_menu = tk.Menu(self.cv, tearoff=0)
        self.speed_menu = tk.Menu(self.cv, tearoff=0)
        for sp in SPEEDS:
            self.speed_menu.add_command(label="%gx" % sp, command=lambda v=sp: self.set_speed(v))
        self.aspect_menu = tk.Menu(self.cv, tearoff=0)
        for i, a in enumerate(ASPECTS):
            self.aspect_menu.add_radiobutton(label=a[0], variable=self.aspect_i, value=i, command=lambda i=i: self.set_aspect(i))
        cv = self.cv
        cv.bind("<Motion>", self._cv_motion)
        cv.bind("<Enter>", self._cv_motion)
        cv.bind("<Leave>", lambda e: self._cv_leave())
        cv.bind("<ButtonPress-1>", self._cv_press)
        cv.bind("<B1-Motion>", self._cv_drag)
        cv.bind("<ButtonRelease-1>", self._cv_release)
        cv.bind("<MouseWheel>", lambda e: self.vol.set(self.vol.get() + (5 if e.delta > 0 else -5)))
        cv.bind("<Configure>", lambda e: self._redraw())
        self._tip = ui.Tooltip(cv, lambda: (self._hot or {}).get("tip", ""), lambda: ui.DARK, above=True,
                               region=self._tip_region)
        self._card = None                          # the "Up next" card
        self.after(150, self._osd_tick)

    def after(self, ms, fn):
        """A timer on the player's window that close() cancels (only ours: other timers of the app keep running).
        A timer left to fire on a destroyed window raised an error."""
        holder = []

        def run():
            if holder:
                self._afters.discard(holder[0])
            fn()
        try:
            aid = self.top.after(ms, run)
        except tk.TclError:
            return None
        holder.append(aid)
        self._afters.add(aid)
        return aid

    def _wake(self):
        self._last_move = time.time()

    def _no_activate(self, w):
        """Clicking the strip must not take the keyboard away from the player / main window."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            u.GetParent.restype = ctypes.c_void_p
            u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            h = u.GetParent(w.winfo_id()) or w.winfo_id()
            u.SetWindowLongPtrW(h, -20, u.GetWindowLongPtrW(ctypes.c_void_p(h), -20) | 0x08000000)
        except Exception:
            pass

    # which controls are shown (narrow windows - the mini player - drop the less important ones)
    def _layout(self, w):
        px = self.px
        paused = self.stopped or not self.mpv or self.mpv.get_flag("pause")
        left = [("play", ICON["play"] if paused else ICON["pause"], self.toggle, "Play / pause  (Space, K)", True),
                ("stop", ICON["stop"], self.stop, "Stop  (S)", False),
                ("prev", ICON["prev"], lambda: self.step(-1), "Previous in the queue  (P)", False),
                ("next", ICON["next"], lambda: self.step(1), "Next in the queue  (N)", False),
                ("back", ICON["back"], lambda: self.rel(-10), "Back 10 s  (J)", False),
                ("fwd", ICON["fwd"], lambda: self.rel(30), "Forward 30 s", False)]
        right = [("full", ICON["unfull"] if self._is_full() else ICON["full"], lambda: self.fullscreen(),
                  "Full screen  (F, double-click)", False)]
        if self.on_popout:
            right.append(("pop", ICON["attach"] if self.popout_label == "Attach" else ICON["detach"], self.on_popout,
                          "Back into the Player tab" if self.popout_label == "Attach" else "Play in a window of its own", False))
        if self.on_mini:
            right.append(("mini", ICON["mini"], self.on_mini, self.mini_tip, False))
        right += [("aspect", ICON["aspect"], lambda: self._popup(self.aspect_menu, "aspect"), "Picture shape  (A)", False),
                  ("speed", None, lambda: self._popup(self.speed_menu, "speed"), "Speed", False),
                  ("chap", ICON["chapters"], lambda: self._popup(self.chap_menu, "chap", self._fill_chapters), "Chapters", False),
                  ("tracks", ICON["subs"], lambda: self._popup(self.tracks_menu, "tracks", self._fill_tracks),
                   "Audio, subtitles, sync and night mode", False),
                  ("shot", ICON["camera"], self.screenshot, "Save a picture of this frame  (Shift+S)", False)]
        if w < px(620):                                         # mini player: just the essentials
            left = [x for x in left if x[0] in ("play", "back", "fwd")]
            right = [x for x in right if x[0] in ("full", "pop", "mini", "tracks")]
        return left, right

    def _redraw(self, pos=None):
        cv = getattr(self, "cv", None)
        if cv is None:
            return
        try:
            cv.delete("all")
            w = max(1, cv.winfo_width())
        except tk.TclError:
            return
        px = self.px
        h = px(BAR_H)
        # soft fade into the video above
        for i in range(6):
            cv.create_rectangle(0, i * px(3), w, (i + 1) * px(3), width=0, fill=_mix(OSD_BG, "#000000", 0.5 - i * 0.08))
        self._items = []
        # ---- seek row
        sx0, sx1, sy = px(18), w - px(18), px(26)
        self._seek_box = (sx0, sy - px(10), sx1, sy + px(10))
        hot = self.hover_x is not None or self.seeking
        th = px(3) if not hot else px(5)
        cv.create_rectangle(sx0, sy - th, sx1, sy + th, fill=TRACK, width=0)
        if self.size and self.ranges:                            # downloaded parts of a streaming file
            for a, b in self.ranges:
                cv.create_rectangle(sx0 + (sx1 - sx0) * a / self.size, sy - th, sx0 + (sx1 - sx0) * b / self.size,
                                    sy + th, fill=BUF, width=0)
        if self.dur:
            if pos is None:
                try:
                    pos = self.mpv.get_double("time-pos") if self.mpv else 0
                except Exception:
                    pos = 0
            x = sx0 + (sx1 - sx0) * min(1.0, max(0.0, (pos or 0) / self.dur))
            cv.create_rectangle(sx0, sy - th, x, sy + th, fill=ACC, width=0)
            for t, _title in self.chapters[1:]:                  # chapter marks
                cx = sx0 + (sx1 - sx0) * t / self.dur
                cv.create_line(cx, sy - th - px(2), cx, sy + th + px(2), fill="#8a93a5")
            r = px(7) if hot else px(5)
            cv.create_oval(x - r, sy - r, x + r, sy + r, fill=FG, width=0)
            thumb_up = self.thumb_win is not None and self.thumb_win.winfo_ismapped()
            if self.hover_x is not None and not thumb_up:        # (the preview picture shows the time itself)
                t = self._x_to_time(self.hover_x)
                label = fmt_time(t)
                for ct, title in reversed(self.chapters):
                    if ct <= t:
                        label += "  " + title[:30]
                        break
                tx = min(max(self.hover_x, sx0 + px(40)), sx1 - px(40))
                cv.create_text(tx, sy - px(16), text=label, anchor="s", fill=FG, font=self.f_small)
        # ---- control row
        cy = px(62)
        left, right = self._layout(w)
        x = px(10)
        for key, glyph, cmd, tip, big in left:
            bw = px(48) if big else px(40)
            self._icon(key, glyph, cmd, tip, x, cy, bw, big)
            x += bw
        # volume: icon (mute toggle) + slim bar
        v = self.vol.get()
        muted = bool(self.mpv and self.mpv.get_flag("mute"))
        self._icon("mute", ICON["mute"] if muted or v == 0 else ICON["vol"], self.toggle_mute, "Mute  (M)", x + px(4), cy, px(36), False)
        x += px(40)
        vx0, vx1 = x, x + px(80)
        self._vol_box = (vx0, cy - px(10), vx1, cy + px(10))
        cv.create_rectangle(vx0, cy - px(2), vx1, cy + px(2), fill=TRACK, width=0)
        vf = vx0 + (vx1 - vx0) * min(1.0, v / 130.0)
        cv.create_rectangle(vx0, cy - px(2), vf, cy + px(2), fill=FG if not muted else MUTED, width=0)
        cv.create_oval(vf - px(5), cy - px(5), vf + px(5), cy + px(5), fill=FG, width=0)
        self._items.append(dict(key="volbar", box=self._vol_box, tip="Volume  (Up / Down, mouse wheel)", cmd=None))
        x = vx1 + px(16)
        tt = self.time.get()
        cv.create_text(x, cy, text=tt, anchor="w", fill=FG, font=self.f_ui)
        x += int(self.f_ui.measure(tt)) + px(16)
        xr = w - px(10)
        for key, glyph, cmd, tip, big in right:
            if key == "speed":
                text = self._speed_label
                bw = max(px(40), int(self.f_ui.measure(text)) + px(16))
                self._text_btn(key, text, cmd, tip, xr - bw, cy, bw)
            else:
                bw = px(40)
                self._icon(key, glyph, cmd, tip, xr - bw, cy, bw, False)
            xr -= bw
        msg = self.status.get()
        if msg and xr - x > px(60):
            cv.create_text(x, cy, text=_elide(msg, self.f_ui, xr - x - px(10)), anchor="w", fill=MUTED, font=self.f_ui)

    def _icon(self, key, glyph, cmd, tip, x, cy, bw, big):
        cv, px = self.cv, self.px
        lit = self._hot is not None and self._hot.get("key") == key
        f = self.f_big if big else self.f_icon
        col = "#ffffff" if lit else FG
        if self.vector_icons:
            draw_glyph(cv, GLYPH_NAME.get(glyph, key), x + bw // 2, cy, px(26) if big else px(20), col, bold=lit)
        else:
            cv.create_text(x + bw // 2, cy, text=glyph, font=f, fill=col)
            if lit:                                               # icons have no bold: draw it twice, 1 px apart
                cv.create_text(x + bw // 2 + 1, cy, text=glyph, font=f, fill=col)
        self._items.append(dict(key=key, box=(x, cy - px(18), x + bw, cy + px(18)), cmd=cmd, tip=tip))

    def _text_btn(self, key, text, cmd, tip, x, cy, bw):
        lit = self._hot is not None and self._hot.get("key") == key
        self.cv.create_text(x + bw // 2, cy, text=text, font=self.f_bold if lit else self.f_ui, fill=FG)
        self._items.append(dict(key=key, box=(x, cy - self.px(18), x + bw, cy + self.px(18)), cmd=cmd, tip=tip))

    def _item_at(self, x, y):
        for it in self._items:
            x0, y0, x1, y1 = it["box"]
            if x0 <= x < x1 and y0 <= y < y1:
                return it
        return None

    def _in(self, box, e):
        return box[0] <= e.x <= box[2] and box[1] <= e.y <= box[3]

    def _tip_region(self):
        it = self._hot
        if not it:
            return None
        x, y = self.cv.winfo_rootx(), self.cv.winfo_rooty()
        return x + it["box"][0], y + it["box"][1], x + it["box"][2], y + it["box"][3]

    def _cv_motion(self, e):
        self._last_move = time.time()
        if self._drag:
            return
        if self._in(self._seek_box, e):
            self.hover_x = e.x
            self._want_thumb(e)
        elif self.hover_x is not None:
            self.hover_x = None
            self._hide_thumb()
        it = self._item_at(e.x, e.y)
        if (it or {}).get("key") != (self._hot or {}).get("key"):
            self._hot = it
            self.cv.configure(cursor="hand2" if it or self.hover_x is not None else "")
            self._tip._hide()
            if it and it.get("tip"):
                self._tip._schedule()
        elif self.hover_x is not None:
            self.cv.configure(cursor="hand2")
        self._redraw()

    def _cv_leave(self):
        self._hot, self.hover_x = None, None
        self._hide_thumb()
        self._redraw()

    def _cv_press(self, e):
        self._tip._hide()
        if self._in(self._seek_box, e):
            self._drag = "seek"
            self.seeking = True
            self._seek_to(e.x)
        elif self._in(self._vol_box, e):
            self._drag = "vol"
            self._vol_at(e.x)
        else:
            self._down = self._item_at(e.x, e.y)

    def _cv_drag(self, e):
        if self._drag == "seek":
            self.hover_x = e.x
            self._seek_to(e.x)
        elif self._drag == "vol":
            self._vol_at(e.x)

    def _cv_release(self, e):
        d, self._drag = self._drag, None
        self.seeking = False
        down, self._down = self._down, None
        # compare by name: the strip is redrawn several times a second while playing, and each redraw makes new
        # item records - an identity check dropped every click that had a redraw between press and release
        it = self._item_at(e.x, e.y)
        if d is None and down is not None and it is not None and it["key"] == down["key"] and it.get("cmd"):
            it["cmd"]()
        self._redraw()

    def _vol_at(self, x):
        x0, _y0, x1, _y1 = self._vol_box
        self.vol.set(round(130 * min(1.0, max(0.0, (x - x0) / max(1, x1 - x0)))))

    def _popup(self, menu, key, fill=None):
        if fill:
            fill()
        it = next((i for i in self._items if i["key"] == key), None)
        x0, y0 = (it["box"][0], it["box"][1]) if it else (0, 0)
        try:
            menu.tk_popup(self.cv.winfo_rootx() + x0, self.cv.winfo_rooty() + y0 - self.px(4))
        finally:
            menu.grab_release()

    def _is_full(self):
        try:
            return bool(self.is_full()) if self.is_full else False
        except Exception:
            return False

    # ---- showing / hiding the strip (and the Up next card) -------------------------------------------------------
    def _osd_tick(self):
        """Every 120 ms: keep the strip over the bottom of the video; show it while the mouse moves over the video,
        while paused or stopped, and hide it 2.5 s after the mouse stopped while playing."""
        osd = getattr(self, "osd", None)
        try:
            if osd is None or not osd.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            v = self.video
            shown_ok = v.winfo_viewable() and v.winfo_toplevel().state() in ("normal", "zoomed") and v.winfo_height() > 60
            vx, vy, vw, vh = v.winfo_rootx(), v.winfo_rooty(), v.winfo_width(), v.winfo_height()
            ptr = v.winfo_pointerxy()
            inside = vx <= ptr[0] < vx + vw and vy <= ptr[1] < vy + vh
            if inside and ptr != self._last_ptr:
                self._last_move = time.time()
            self._last_ptr = ptr
            h = min(self.px(BAR_H), vh)
            over_bar = inside and ptr[1] >= vy + vh - h
            idle = time.time() - self._last_move > 2.5
            paused = self.stopped or not self.mpv or not self.cur or self.mpv.get_flag("pause")
            want = shown_ok and inside and (not idle or over_bar or paused)
            if want:
                geo = "%dx%d+%d+%d" % (vw, h, vx, vy + vh - h)
                if geo != getattr(self, "_osd_geo", None):
                    self._osd_geo = geo
                    osd.geometry(geo)
                if not self._osd_on:
                    try:                               # the mini player is always on top: so are its controls
                        top_most = bool(int(v.winfo_toplevel().attributes("-topmost")))
                        osd.attributes("-topmost", top_most)
                        if self._card is not None:
                            self._card.attributes("-topmost", top_most)
                    except (tk.TclError, ValueError):
                        pass
                    osd.deiconify()
                    osd.lift()
                    if not getattr(self, "_noact_done", False):
                        self._no_activate(osd)
                        self._noact_done = True
                    self._osd_on = True
                    self._redraw()
            elif self._osd_on:
                osd.withdraw()
                self._osd_on = False
                self._hot, self.hover_x = None, None
                self._hide_thumb()
                self._tip._hide()
            self._card_tick(shown_ok, vx, vy, vw, vh)
        except (tk.TclError, Exception):
            pass
        try:
            self.after(120, self._osd_tick)
        except tk.TclError:
            pass

    def _card_tick(self, shown_ok, vx, vy, vw, vh):
        """Near the end of a file with another one queued: a small 'Up next' card with Play now / Cancel."""
        nxt = self.queue[self.index + 1] if self.mpv and self.cur and self.index + 1 < len(self.queue) else None
        left = None
        if nxt and self.dur > 60 and not self.stopped and not self.next_cancelled:
            try:
                left = self.dur - (self.mpv.get_double("time-pos") or 0)
            except Exception:
                left = None
        if left is None or not 0 < left <= UP_NEXT_S or not shown_ok:
            if self._card is not None:
                self._card.destroy()
                self._card = None
            return
        px = self.px
        if self._card is None:
            c = self._card = tk.Toplevel(self.parent)
            c.overrideredirect(True)
            try:
                c.transient(self.parent.winfo_toplevel())
                c.attributes("-alpha", 0.94)
            except tk.TclError:
                pass
            c.cv = tk.Canvas(c, bg=OSD_BG, highlightthickness=0, bd=0, width=px(340), height=px(92))
            c.cv.pack()
            c.cv.bind("<ButtonRelease-1>", self._card_click)
            c.cv.bind("<Motion>", lambda e: self._card_draw(e.x, e.y))
            self.after(50, lambda: self._no_activate(c))
        cw, ch = px(340), px(92)
        c = self._card
        geo = "%dx%d+%d+%d" % (cw, ch, vx + vw - cw - px(20), vy + vh - ch - px(BAR_H) - px(12))
        if geo != getattr(c, "geo", None):
            c.geo = geo
            c.geometry(geo)
            c.lift()
        c.left, c.title = int(left + 0.99), nxt.get("title", "")
        self._card_draw()

    def _card_draw(self, mx=-1, my=-1):
        c = self._card
        if c is None:
            return
        cv, px = c.cv, self.px
        cv.delete("all")
        cv.create_text(px(16), px(18), anchor="w", text="Up next", fill=MUTED, font=self.f_small)
        cv.create_text(px(16), px(40), anchor="w", text=_elide(c.title, self.f_bold, px(310)), fill=FG, font=self.f_bold)
        c.boxes = {}
        x = px(16)
        for key, text in (("now", "Play now"), ("cancel", "Cancel")):
            w = int(self.f_ui.measure(text)) + px(4)
            box = (x, px(60), x + w, px(82))
            lit = box[0] <= mx < box[2] and box[1] <= my < box[3]
            cv.create_text(x, px(71), anchor="w", text=text, fill=FG if key == "now" else MUTED,
                           font=self.f_bold if lit else self.f_ui)
            c.boxes[key] = box
            x += w + px(22)
        cv.create_text(px(324), px(71), anchor="e", text="in %d s" % c.left, fill=MUTED, font=self.f_ui)
        cv.configure(cursor="hand2" if any(b[0] <= mx < b[2] and b[1] <= my < b[3] for b in c.boxes.values()) else "")

    def _card_click(self, e):
        c = self._card
        if c is None:
            return
        for key, b in c.boxes.items():
            if b[0] <= e.x < b[2] and b[1] <= e.y < b[3]:
                if key == "now":
                    self.step(1)
                else:
                    self.next_cancelled = True
                c.destroy()
                self._card = None
                return

    def bind_keys(self, w, everywhere=False):
        def key(seq, fn):
            handler = lambda e: (fn(), "break")[1] if self._key_ok(e) else None
            if everywhere:
                self.top.bind_all(seq, handler)      # (replaces the previous player's: one player per window)
            else:
                w.bind(seq, handler)
        key("<space>", self.toggle)
        key("<Left>", lambda: self.rel(-5)); key("<Right>", lambda: self.rel(5))
        key("<Shift-Left>", lambda: self.rel(-30)); key("<Shift-Right>", lambda: self.rel(30))
        key("<Up>", lambda: self.vol.set(self.vol.get() + 5)); key("<Down>", lambda: self.vol.set(self.vol.get() - 5))
        key("<Prior>", lambda: self.chapter(-1)); key("<Next>", lambda: self.chapter(1))
        key("f", lambda: self.fullscreen()); key("<Escape>", lambda: self.fullscreen(False))
        key("m", lambda: self.mpv.set("mute", not self.mpv.get_flag("mute")))
        key("a", self.set_aspect); key("s", self.stop)
        key("n", lambda: self.step(1)); key("p", lambda: self.step(-1))
        key("z", lambda: self.sub_delay(-0.1)); key("x", lambda: self.sub_delay(0.1))
        key("<comma>", lambda: self.sub_scale(-0.1)); key("<period>", lambda: self.sub_scale(0.1))
        key("<Control-o>", self.open_dialog); key("<F1>", self.help); key("<question>", self.help)
        key("j", lambda: self.rel(-10)); key("k", self.toggle); key("l", lambda: self.rel(10))
        key("S", self.screenshot); key("Z", lambda: self.audio_delay(-0.1)); key("X", lambda: self.audio_delay(0.1))
        for n in range(10):
            key(str(n), lambda n=n: self.seek_percent(n * 10))

    def _key_ok(self, e):
        """Shortcuts apply while this player is open (and its tab shows), except while typing in a text field."""
        if not self.mpv:
            return False
        if self.key_scope is not None:
            try:
                if not self.key_scope():
                    return False
            except Exception:
                return False
        w = getattr(e, "widget", None)
        try:                                   # keys typed in a dialog (Settings ...) are not the player's
            if w is not None and str(w.winfo_toplevel()) not in (str(self.top.winfo_toplevel()), str(self.osd)):
                return False
        except (tk.TclError, AttributeError):
            pass
        return not isinstance(w, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry, ttk.Combobox, ttk.Spinbox))

    # ---- queue
    def play_index(self, i):
        if not 0 <= i < len(self.queue):
            return
        self._save_pos()
        self.index, self.cur = i, self.queue[i]
        self.prefetched = False
        self.stopped = False
        self.next_cancelled = False
        self._last_pos = 0.0
        self._eof_done = False
        self.ranges, self.size, self.rate_hist = [], 0, []
        self.dur = 0.0
        e = self.cur
        self.top_title(e.get("title", ""))
        self._resume_to = media_state.resume_pos(self.state_dir, e.get("key")) if self.state_dir and e.get("key") else 0.0
        self.mpv.command("loadfile", e["url"])
        self.mpv.set("pause", False)
        self.status.set("Loading…")

    def top_title(self, t):
        if t:
            try:
                chrome.set_title(self.top, t)
            except Exception:
                pass

    def step(self, d):
        if self.mpv and 0 <= self.index + d < len(self.queue):
            self.play_index(self.index + d)

    def add_target(self, target, title=""):
        self.queue = [dict(url=target, title=title or os.path.basename(target), key="")]
        self.play_index(0)

    def open_dialog(self):
        p = filedialog.askopenfilename(parent=self.top, title="Open media")
        if p:
            self.add_target(p)

    # ---- actions
    def toggle(self):
        if not self.mpv:
            return
        if getattr(self, "stopped", False):                  # after Stop, Play starts the current file again
            self.stopped = False
            self.play_index(self.index)
            return
        self.mpv.set("pause", not self.mpv.get_flag("pause"))

    def stop(self):
        """Stop playback (the position is remembered, Play resumes from it) and show a black screen."""
        if not self.mpv or not self.queue:
            return
        self._save_pos()
        self.stopped = True
        try:
            self.mpv.command("stop")
        except mpvlib.MpvError:
            pass
        self.time.set("0:00 / 0:00")
        self.status.set("Stopped")

    def set_aspect(self, i=None):
        """Change the picture shape (None = next in the list); also bound to the A key."""
        if not self.mpv:
            return
        i = (self.aspect_i.get() + 1) % len(ASPECTS) if i is None else i
        self.aspect_i.set(i)
        name, ov, pan, keep = ASPECTS[i]
        self.mpv.set("video-aspect-override", ov)
        self.mpv.set("panscan", pan)
        self.mpv.set("keepaspect", keep)
        self._aspect_name = name
        self.status.set("Aspect: %s" % name)

    def rel(self, secs):
        if self.mpv:
            try:
                self.mpv.command("seek", secs, "relative")
            except mpvlib.MpvError:
                pass

    def chapter(self, d):
        if self.mpv:
            try:
                self.mpv.command("add", "chapter", d)
            except mpvlib.MpvError:
                pass

    def set_speed(self, v):
        self.mpv.set("speed", v)
        self._speed_label = "%gx" % v
        self._redraw()

    def seek_percent(self, pct):
        if self.mpv:
            try:
                self.mpv.command("seek", pct, "absolute-percent")
            except mpvlib.MpvError:
                pass

    def toggle_mute(self):
        if self.mpv:
            self.mpv.set("mute", not self.mpv.get_flag("mute"))
            self._redraw()

    def audio_delay(self, d):
        """Sound out of sync with the picture: move it earlier (-) or later (+)."""
        if self.mpv:
            self.mpv.set("audio-delay", self.mpv.get_double("audio-delay") + d)
            self.status.set("Audio delay %+.1f s" % self.mpv.get_double("audio-delay"))

    def set_night(self, on=None):
        """Night mode: evens out loud and quiet parts (speech louder, explosions softer)."""
        if not self.mpv:
            return
        on = self.night.get() if on is None else on
        self.night.set(on)
        try:
            self.mpv.set("af", "lavfi=[dynaudnorm=f=150:g=15:p=0.9]" if on else "")
        except mpvlib.MpvError:
            pass
        self.status.set("Night mode on" if on else "Night mode off")

    def screenshot(self):
        """Save the current frame (with subtitles) as a PNG in Pictures\\Bit Torrent Client."""
        if not self.mpv or not self.cur:
            return
        pics = os.path.join(os.path.expanduser("~"), "Pictures")
        if sys.platform.startswith("linux"):
            import linux
            pics = linux.pictures_dir()
        d = os.path.join(pics, "Bit Torrent Client")
        try:
            os.makedirs(d, exist_ok=True)
            name = re.sub(r'[\\/:*?"<>|]+', "_", self.cur.get("title") or "frame")[:80]
            pos = self.mpv.get_double("time-pos")
            path = os.path.join(d, "%s %s.png" % (name, fmt_time(pos).replace(":", "-")))
            self.mpv.command("screenshot-to-file", path, "subtitles")
            self.status.set("Picture saved: %s" % os.path.basename(path))
        except (OSError, mpvlib.MpvError) as e:
            self.status.set("Picture not saved: %s" % e)

    def sub_delay(self, d):
        self.mpv.set("sub-delay", self.mpv.get_double("sub-delay") + d)
        self.status.set("Subtitle delay %+.1f s" % self.mpv.get_double("sub-delay"))

    def sub_scale(self, d):
        self.mpv.set("sub-scale", max(0.3, self.mpv.get_double("sub-scale", 1.0) + d))
        self.status.set("Subtitle size %d%%" % round(self.mpv.get_double("sub-scale", 1.0) * 100))

    def _vol(self, v):
        if self.mpv:
            self.mpv.set("volume", v)
            if v and self.mpv.get_flag("mute"):
                self.mpv.set("mute", False)

    def fullscreen(self, on=None):
        if self.on_fullscreen:
            self.on_fullscreen(on)

    def help(self):
        messagebox.showinfo("Player keys", HELP, parent=self.top)

    def _fill_tracks(self):
        m = self.tracks_menu
        m.delete(0, "end")
        for kind, prop, label in (("audio", "aid", "Audio"), ("sub", "sid", "Subtitles")):
            m.add_command(label=label, state="disabled")
            if kind == "sub":
                m.add_command(label="  Off", command=lambda: self.pick_track("sub", None))
            for t in self.mpv.tracks() if self.mpv else []:
                if t["type"] == kind:
                    name = " ".join(x for x in (t["lang"], t["title"]) if x) or "Track %d" % t["id"]
                    m.add_command(label=("\u25cf " if t["selected"] else "  ") + name,
                                  command=lambda k=kind, t=t: self.pick_track(k, t))
            m.add_separator()
        m.add_command(label="Sound earlier (0.1 s)   Shift+Z", command=lambda: self.audio_delay(-0.1))
        m.add_command(label="Sound later (0.1 s)   Shift+X", command=lambda: self.audio_delay(0.1))
        m.add_checkbutton(label="Night mode (even out loud and quiet)", variable=self.night, command=self.set_night)
        m.add_separator()
        m.add_command(label="Load subtitle file\u2026", command=self._load_sub)
        m.add_command(label="Download subtitles (OpenSubtitles)\u2026", command=self.find_subs)
        m.add_separator()
        m.add_command(label="Subtitles smaller", command=lambda: self.sub_scale(-0.1))
        m.add_command(label="Subtitles larger", command=lambda: self.sub_scale(0.1))
        m.add_command(label="Subtitles earlier (0.1 s)", command=lambda: self.sub_delay(-0.1))
        m.add_command(label="Subtitles later (0.1 s)", command=lambda: self.sub_delay(0.1))
        m.add_separator()
        m.add_command(label="Keyboard shortcuts\u2026", command=self.help)

    def pick_track(self, kind, t):
        """Choose an audio / subtitle track (None = subtitles off) and remember it for the rest of the series."""
        if not self.mpv:
            return
        self.mpv.set("aid" if kind == "audio" else "sid", t["id"] if t else "no")
        key = series_key(self.cur)
        if key:
            prefs = load_prefs(self.state_dir) if self.state_dir else getattr(self, "_mem_prefs", {})
            prefs.setdefault(key, {})[kind] = {"off": True} if t is None else {"lang": t.get("lang", ""),
                                                                                "title": t.get("title", "")}
            if self.state_dir:
                save_prefs(self.state_dir, prefs)
            else:
                self._mem_prefs = prefs

    def _apply_track_prefs(self):
        """A new file of the same series: pick the audio / subtitles chosen last time (by language and name)."""
        key = series_key(self.cur)
        prefs = (load_prefs(self.state_dir) if self.state_dir else getattr(self, "_mem_prefs", {})).get(key or "", {})
        if not prefs or not self.mpv:
            return
        tracks = self.mpv.tracks()
        for kind, prop in (("audio", "aid"), ("sub", "sid")):
            want = prefs.get(kind)
            if not want:
                continue
            if want.get("off"):
                self.mpv.set(prop, "no")
                continue
            same = [t for t in tracks if t["type"] == kind and t.get("lang", "") == want.get("lang", "")]
            best = next((t for t in same if t.get("title", "") == want.get("title", "")), same[0] if same else None)
            if best is not None:
                self.mpv.set(prop, best["id"])

    def _fill_chapters(self):
        m = self.chap_menu
        m.delete(0, "end")
        if not self.chapters:
            m.add_command(label="No chapters in this file", state="disabled")
        for t, title in self.chapters:
            m.add_command(label="%s  %s" % (fmt_time(t), title), command=lambda t=t: self.seek_abs(t))

    def _load_sub(self):
        p = filedialog.askopenfilename(parent=self.top, title="Subtitle file",
                                       filetypes=[("Subtitles", "*.srt *.ass *.ssa *.vtt *.sub"), ("All files", "*.*")])
        if p:
            self.mpv.command("sub-add", p)

    def find_subs(self):
        o = self.sess.get("opts", {})
        name = (self.cur or {}).get("title", "")
        if not o.get("os_key"):
            messagebox.showinfo("Subtitles", "Add your OpenSubtitles API key in the client's Settings > General > "
                                "Media player to download subtitles.", parent=self.top)
            return
        self.status.set("Searching subtitles\u2026")

        def work():
            try:
                res = subs_mod.search(name, o.get("os_lang") or "en", o["os_key"])
                if not res:
                    raise subs_mod.SubError("no subtitles found for \"%s\"" % subs_mod.clean_query(name))
                path = subs_mod.download(res[0]["id"], os.path.join(tempfile.gettempdir(), "BitTorrentClient-subs"),
                                         o["os_key"], user=o.get("os_user", ""), password=o.get("os_pass", ""))
                self._later(lambda: (self.mpv.command("sub-add", path, "select"), self.status.set("Subtitles loaded: %s" % res[0]["name"][:50])))
            except Exception as e:
                self._later(lambda: self.status.set("Subtitles: %s" % e))
        threading.Thread(target=work, daemon=True).start()

    def _later(self, fn):
        try:
            self.top.after(0, fn)
        except Exception:
            pass

    # ---- seek bar
    def _x_to_time(self, x):
        x0, _y0, x1, _y1 = getattr(self, "_seek_box", (0, 0, max(1, self.cv.winfo_width()), 0))
        return self.dur * min(1.0, max(0.0, (x - x0) / max(1, x1 - x0)))

    def seek_abs(self, t):
        if self.mpv:
            try:
                self.mpv.command("seek", "%.2f" % t, "absolute+exact" if False else "absolute")
            except mpvlib.MpvError:
                pass

    def _seek_press(self, e):
        self.seeking = True
        self._seek_to(e.x)

    def _seek_drag(self, e):
        self._seek_to(e.x)

    def _seek_hover(self, e):
        self.hover_x = e.x
        self._redraw()
        self._want_thumb(e)

    def _thumb_ok(self, t):
        """A preview needs the data around t: always for local/complete files, for streams only where it is downloaded."""
        e = self.cur or {}
        if not e.get("map") or not self.size or not self.dur:
            return True
        if sum(b - a for a, b in self.ranges) >= self.size:
            return True
        bp = t / self.dur * self.size
        return any(a <= bp <= b - 2_000_000 for a, b in self.ranges)

    def _want_thumb(self, ev):
        if not self.mpv or not self.dur or not self.cur or not self.sess.get("opts", {}).get("thumbs", True):
            return
        t = self._x_to_time(ev.x)
        if not self._thumb_ok(t):
            self._hide_thumb()
            return
        if self.grabber is None:
            self.grabber = thumbs.Grabber(self.mpv.path)
        url = self.cur["url"]
        if "://" in url:
            url += "?nt=1"                                # a preview must not move the download's playhead
        x_root, y_root = self.cv.winfo_rootx() + ev.x, self.cv.winfo_rooty()
        self.grabber.request(url, t, lambda path, x=x_root, y=y_root, t=t: self._later(lambda: self._show_thumb(path, x, y, t)))

    def _show_thumb(self, path, x, y, t):
        if self.hover_x is None or not path or not self.mpv:
            return
        try:
            img = tk.PhotoImage(file=path)
        except tk.TclError:
            return
        self._thumb_img = img
        if self.thumb_win is None:
            w = tk.Toplevel(self.top)
            w.overrideredirect(True)
            try:
                w.attributes("-topmost", True)
            except tk.TclError:
                pass
            self.thumb_lbl = tk.Label(w, bg="black", bd=1, relief="solid", compound="top", fg=FG, font=("Segoe UI", 8))
            self.thumb_lbl.pack()
            self.thumb_win = w
        self.thumb_lbl.configure(image=img, text=fmt_time(t))
        self.thumb_win.update_idletasks()
        self.thumb_win.geometry("+%d+%d" % (x - img.width() // 2, y - img.height() - 34))
        self.thumb_win.deiconify()

    def _hide_thumb(self):
        if self.thumb_win is not None:
            self.thumb_win.withdraw()

    def _seek_to(self, x):
        if self.mpv and self.dur:
            t = self._x_to_time(x)
            try:
                self.mpv.command("seek", "%.2f" % t, "absolute+keyframes")
            except mpvlib.MpvError:
                return
            self._draw_seek(t)

    def _draw_seek(self, pos=None):
        self._redraw(pos)

    # ---- background: availability map, download speed vs bitrate
    def _map_loop(self):
        last = None
        while self.mpv:
            e = self.cur
            if e and e.get("map"):
                try:
                    with urllib.request.urlopen(e["map"], timeout=5) as r:
                        d = json.loads(r.read())
                    self.size, self.ranges = d["size"], d["ranges"]
                    have = sum(b - a for a, b in self.ranges)
                    now = time.time()
                    self.rate_hist = (self.rate_hist + [(now, have)])[-6:]
                except Exception:
                    pass
            time.sleep(2)

    def _net_status(self, pos, paused_cache):
        """Short text about buffering: seconds ready, or a warning when the download is slower than the video plays."""
        m = self.mpv
        if not self.size or not self.dur or not self.ranges:
            return ""
        bitrate = self.size / self.dur
        byte_pos = pos * bitrate
        ahead_bytes = 0
        for a, b in self.ranges:
            if a <= byte_pos <= b:
                ahead_bytes = b - byte_pos
                break
        ready = ahead_bytes / bitrate if bitrate else 0
        if sum(b - a for a, b in self.ranges) >= self.size:
            return ""
        note = ""
        if len(self.rate_hist) >= 3:
            (t0, h0), (t1, h1) = self.rate_hist[0], self.rate_hist[-1]
            rate = (h1 - h0) / max(1e-6, t1 - t0)
            if rate < bitrate * 1.05 and ready < 120:
                note = " \u2014 download is slower than the video (%.1f MB/s needed)" % (bitrate / 1e6)
        if paused_cache or ready < 5:
            return "Buffering: %d s ready%s" % (ready, note)
        return ("%d s downloaded ahead" % ready) + note

    # ---- periodic update
    def _tick(self):
        m = self.mpv
        if not m:
            return
        try:
            for ev in m.poll_events():
                if ev == "file-loaded":
                    self._loaded()
                elif ev == "end-file" and self.dur and max(m.get_double("time-pos"), self._last_pos) >= self.dur - 2:
                    self._ended()                  # (mpv has usually cleared time-pos by now: use the last one seen)
                elif ev.startswith("msg:"):
                    {"tk-fs": lambda: self.fullscreen(), "tk-fs-off": lambda: self.fullscreen(False),
                     "tk-aspect": self.set_aspect, "tk-stop": self.stop, "tk-shot": self.screenshot, "tk-next": lambda: self.step(1), "tk-prev": lambda: self.step(-1), "tk-help": self.help}.get(ev[4:], lambda: None)()
            self.dur = m.get_double("duration") or self.dur
            pos = m.get_double("time-pos")
            if pos:
                self._last_pos = pos
            # keep-open holds the last frame (no end-file event): the end of the file is 'eof-reached'
            if self.cur and not self.stopped and not self._eof_done and m.get_flag("eof-reached"):
                self._ended()
            paused = m.get_flag("pause")
            v = m.get_double("volume", self.vol.get())          # changed with the keys / wheel inside the video
            if abs(v - float(self.vol.get())) >= 1:
                self.vol.set(v, push=False)
            self.time.set("%s / %s" % (fmt_time(pos), fmt_time(self.dur)))
            waiting = m.get_flag("paused-for-cache")
            if waiting:                                    # over the picture itself, where you are looking
                pct = int(m.get_double("cache-buffering-state"))
                try:
                    m.command("show-text", "Buffering\u2026 %d%%" % pct if pct else "Buffering\u2026", 600)
                except mpvlib.MpvError:
                    pass
            msg = self._net_status(pos, waiting) if self.cur and self.cur.get("map") else ""
            if msg or waiting:
                self.status.set(msg or "Waiting for the download\u2026")
            elif self.status.get().startswith(("Loading", "Buffering", "Waiting")) or "downloaded ahead" in self.status.get():
                self.status.set("")
            if not self.seeking and self._osd_on:
                self._redraw(pos)
            if self.dur and pos > self.dur * 0.85 and not self.prefetched:
                self._prefetch_next()
            if time.time() - self._saved > 5:
                self._save_pos()
        except Exception:
            pass
        self.after(250, self._tick)

    def _loaded(self):
        m, e = self.mpv, self.cur or {}
        self.dur = m.get_double("duration")
        self.chapters = m.chapters()
        try:
            self._apply_track_prefs()
        except Exception:
            pass
        if self._resume_to:
            self.seek_abs(self._resume_to)
            self.status.set("Resumed at %s" % fmt_time(self._resume_to))
            self._resume_to = 0.0
        for i, u in enumerate(e.get("subs", [])):
            try:
                m.command("sub-add", u, "select" if i == 0 else "auto")
            except mpvlib.MpvError:
                pass
        if e.get("hint") and self.dur:
            self._fire(e["hint"] + "?dur=%.2f" % self.dur)

    def _fire(self, url):
        def go():
            try:
                urllib.request.urlopen(url, timeout=5).read()
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    def _prefetch_next(self):
        self.prefetched = True
        if self.index + 1 < len(self.queue) and self.queue[self.index + 1].get("prefetch"):
            self._fire(self.queue[self.index + 1]["prefetch"])

    def _ended(self):
        if self._eof_done:
            return
        self._eof_done = True
        self._save_pos(final=True)
        if self.index + 1 < len(self.queue) and not self.next_cancelled:
            self.play_index(self.index + 1)
        elif self.on_end:
            self.on_end()

    def _save_pos(self, final=False):
        self._saved = time.time()
        m, e = self.mpv, self.cur
        if m and e and e.get("key") and self.state_dir and self.dur:
            pos = self.dur if final else m.get_double("time-pos")
            if pos:
                media_state.update(self.state_dir, e["key"], pos, self.dur, e.get("title", ""))

    def close(self):
        self._save_pos()
        m, self.mpv = self.mpv, None        # stop the video first: its window lives inside ours (X11 errors otherwise)
        if m:
            m.close()
        for aid in list(self._afters):
            try:
                self.top.after_cancel(aid)
            except tk.TclError:
                pass
        self._afters.clear()
        for w in (getattr(self, "_card", None), getattr(self, "osd", None)):
            try:
                if w is not None:
                    w.destroy()
            except tk.TclError:
                pass
        self._card = self.osd = None
        if self.grabber:
            self.grabber.close()
        if self.thumb_win is not None:
            try:
                self.thumb_win.destroy()
            except tk.TclError:
                pass
        m, self.mpv = self.mpv, None
        if m:
            m.close()


class PlayerWindow:
    """A player in a window of its own (also the standalone program). `master` None makes its own root window."""

    def __init__(self, master=None, target=None, title="", map_url=None, lib=None, options=None, on_close=None,
                 queue=None, index=0, session=None, on_dock=None, mini=False, full=False):
        self.own_root = master is None
        self.win = tk.Tk() if self.own_root else tk.Toplevel(master)
        self.win.title(title or "Player")
        self.win.configure(bg=BG)
        self.win.geometry("960x600")
        self.win.minsize(480, 320)
        self.chrome = None
        if chrome.enabled(options):
            try:
                scale = self.win.winfo_fpixels("1i") / 96.0
                self.chrome = chrome.attach(self.win, ui.DARK, lambda v: int(round(v * scale)), title or "Player",
                                            resizable=True, buttons=("close",))
                self.win.geometry("%dx%d" % (int(960 * scale), int(600 * scale)))
            except Exception:
                self.chrome = None
        self.on_close = on_close
        self.full = False
        self.last_move = 0
        q = queue or ([dict(url=target, title=title or os.path.basename(target), key="", map=map_url)] if target else [])
        self.mini = False
        self._normal_geo = None
        self.panel = PlayerPanel(self.win, self.win, q, index, session, lib, options, on_fullscreen=self.fullscreen,
                                 on_popout=(lambda: on_dock(self)) if on_dock else None, popout_label="Attach",
                                 on_mini=lambda: self.set_mini(not self.mini), is_full=lambda: self.full)
        self.mpv = self.panel.mpv
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        if self.panel.error:
            self.panel.after(100, lambda: self._no_lib(target, self.panel.error))
            return
        self.win.bind("<Motion>", self._motion)
        self.panel.after(500, self._hide_bar_tick)
        if mini:
            self.panel.after(50, lambda: self.set_mini(True))
        # opened full screen from the Player tab: leaving full screen brings it back into the tab
        self._back_to_tab = bool(full and on_dock)
        self._on_dock = on_dock
        if full:
            self.panel.after(80, lambda: self.fullscreen(True))
        if master is not None:            # a new window from the main one: give it the keyboard (Esc, Space ...)
            self.panel.after(150, self._take_focus)

    # kept for callers/tests that used the old single-class API
    def __getattr__(self, name):
        if name in ("panel",):
            raise AttributeError(name)
        return getattr(self.panel, name)

    def _no_lib(self, target, why):
        msg = ("The built-in player needs libmpv, which was not found.\n\n%s\n\nPlace libmpv-2.dll next to the program "
               "(see README), or open the file in another player now?" % why)
        if target and messagebox.askyesno("Player", msg, parent=self.win):
            try:
                try:
                    external_player(target)
                except NoPlayer:
                    default_player(target)
            except Exception as e:
                messagebox.showerror("Player", str(e), parent=self.win)
        self.close()

    def fullscreen(self, on=None):
        self.full = (not self.full) if on is None else on
        if self.chrome:
            self.chrome.fullscreen(self.full)
        else:
            self.win.attributes("-fullscreen", self.full)
        self._cursor(False)
        if self.panel.mpv:
            self.panel.mpv.set("cursor-autohide", 1000 if self.full else "no")
        self.last_move = time.time()
        self.panel._last_move = time.time()
        self.panel._redraw()
        if not self.full and getattr(self, "_back_to_tab", False):
            self._back_to_tab = False
            self.panel.after(50, lambda: self._on_dock(self))

    def _take_focus(self):
        try:
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            pass

    def set_mini(self, on):
        """Mini player: a small window that stays on top in the bottom-right corner (the controls shrink to the
        essentials); the same button makes it big again."""
        if on == self.mini:
            return
        if self.full:
            self.fullscreen(False)
        self.mini = on
        try:
            if on:
                self._normal_geo = self.win.geometry()
                x, y, w, h = ui.work_area(self.win)
                s = self.panel.scale
                mw, mh = int(480 * s), int(300 * s)
                self.win.geometry("%dx%d+%d+%d" % (mw, mh, x + w - mw - int(16 * s), y + h - mh - int(16 * s)))
                self.win.update_idletasks()        # (making it topmost before the move is applied undoes the move)
                self.win.attributes("-topmost", True)
                self.panel.mini_tip = "Back to full size"
            else:
                self.win.attributes("-topmost", False)
                self.win.update_idletasks()
                if self._normal_geo:
                    self.win.geometry(self._normal_geo)
                self.panel.mini_tip = "Mini player (stays on top in a corner)"
        except tk.TclError:
            pass
        self.panel._redraw()

    def _cursor(self, hidden):
        """Hide / show the mouse pointer over the whole player (Tk widgets and mpv's own video window)."""
        if hidden == getattr(self, "_cursor_hidden", False):
            return
        self._cursor_hidden = hidden
        for w in (self.win, self.panel.video):
            try:
                w.configure(cursor="none" if hidden else "")
            except tk.TclError:
                pass
        if self.panel.mpv:
            self.panel.mpv.set("cursor-autohide", 1000 if self.full else "no")

    def _motion(self, e):
        self._cursor(False)
        self.last_move = time.time()
        self.panel._last_move = self.last_move

    def _hide_bar_tick(self):
        """In full screen the mouse pointer hides with the controls (the panel shows / hides the strip itself)."""
        p = self.panel
        self.last_move = max(self.last_move, p._last_move)
        if self.full and time.time() - self.last_move > 2.5:
            self._cursor(True)
        elif self.full:
            self._cursor(False)
        if p.mpv:
            self.panel.after(500, self._hide_bar_tick)

    def close(self):
        self.panel.close()                # (also cancels the timers the panel and this window started)
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()

    def run(self):
        self.win.mainloop()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Media player (libmpv)")
    ap.add_argument("target", nargs="?")
    ap.add_argument("--map")
    ap.add_argument("--title", default="")
    ap.add_argument("--lib", default="")
    ap.add_argument("--session", default="", help="JSON file with a play queue and settings (written by the client)")
    a = ap.parse_args(argv)
    opts = {}
    if os.environ.get("PLAYER_VO"):                       # for headless tests
        opts["vo"] = os.environ["PLAYER_VO"]
    if os.environ.get("PLAYER_AO"):
        opts["ao"] = os.environ["PLAYER_AO"]
    session, queue, index = {}, None, 0
    if a.session:
        try:
            with open(a.session, encoding="utf-8") as f:
                session = json.load(f)
        except (OSError, ValueError):
            session = {}
        try:
            os.remove(a.session)                           # it may hold a password: read once, then delete
        except OSError:
            pass
        queue, index = session.get("queue"), session.get("index", 0)
        a.lib = a.lib or session.get("lib", "")
        a.title = a.title or (queue[index].get("title", "") if queue else "")
    PlayerWindow(None, a.target, a.title, a.map, a.lib or None, opts, queue=queue, index=index, session=session).run()


if __name__ == "__main__":
    main()
