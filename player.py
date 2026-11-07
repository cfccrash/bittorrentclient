"""Media player built on libmpv. Works on its own (python player.py [file-or-url]) and is what the torrent client opens
for "Play": it can show which parts of a streaming file are already downloaded on its seek bar.

    python player.py movie.mkv
    python player.py http://127.0.0.1:port/s/... --map http://127.0.0.1:port/map/... --title "Movie"
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
from tkinter import filedialog, messagebox, ttk

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
BG, FG, ACC, BUF = "#101216", "#e6e8ec", "#4c9aff", "#3a4150"


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


HELP = """Space / click      Pause / play
Left, Right        Back / forward 5 s   (Shift: 30 s)
Up, Down           Volume
PageUp, PageDown   Previous / next chapter
N, P               Next / previous file in the queue
A                  Next aspect ratio (or the Aspect menu)
S                  Stop (Play starts it again)
M                  Mute
Z, X               Subtitle delay -/+ 0.1 s
, and .            Subtitle size smaller / larger
F, Esc             Full screen / leave   (double-click too)
Ctrl+O             Open a file or link
?, F1              This help"""


def _btn(parent, text, cmd):
    return tk.Button(parent, text=text, command=cmd, bg="#1d2129", fg=FG, activebackground="#2b3140",
                     activeforeground=FG, relief="flat", padx=10, pady=3, bd=0)


def _mb(parent, text):
    return tk.Menubutton(parent, text=text, bg="#1d2129", fg=FG, activebackground="#2b3140", activeforeground=FG,
                         relief="flat", padx=10, pady=3, bd=0)


class PlayerPanel:
    """The whole player (video + controls) inside any Tk container: a window of its own or a tab of the main window.

    queue: list of dicts {url, title, key, map, hint, prefetch, subs[]} played in order (the next one is fetched in
    advance); `session` carries settings (see main()). on_fullscreen(on) / on_popout() let the host decide what the
    Full button does."""

    stopped = False

    def __init__(self, parent, top, queue=(), index=0, session=None, lib=None, options=None, on_fullscreen=None,
                 on_popout=None, on_end=None, key_widget=None, popout_label="Detach"):
        self.popout_label = popout_label
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
        binds = [("f", "script-message tk-fs"), ("ESC", "script-message tk-fs-off"), ("MBTN_LEFT_DBL", "script-message tk-fs"),
                 ("n", "script-message tk-next"), ("p", "script-message tk-prev"), ("?", "script-message tk-help"),
                 ("F1", "script-message tk-help"), ("MBTN_LEFT", "cycle pause"), ("SPACE", "cycle pause"),
                 ("LEFT", "seek -5"), ("RIGHT", "seek 5"), ("Shift+LEFT", "seek -30"), ("Shift+RIGHT", "seek 30"),
                 ("UP", "add volume 5"), ("DOWN", "add volume -5"), ("WHEEL_UP", "add volume 5"), ("WHEEL_DOWN", "add volume -5"),
                 ("PGUP", "add chapter -1"), ("PGDWN", "add chapter 1"), ("m", "cycle mute"), ("z", "add sub-delay -0.1"),
                 ("x", "add sub-delay 0.1"), ("a", "script-message tk-aspect"), ("s", "script-message tk-stop"), (",", "add sub-scale -0.1"), (".", "add sub-scale 0.1")]
        for key, cmd in binds:
            try:
                self.mpv.command("keybind", key, cmd)
            except mpvlib.MpvError:
                pass
        self.bind_keys(key_widget or self.top)
        if self.queue:
            self.play_index(self.index)
        self._tick()
        threading.Thread(target=self._map_loop, daemon=True).start()

    # ---- layout
    def _build(self):
        p = self.parent
        self.video = tk.Frame(p, bg="black", highlightthickness=0)
        self.video.pack(fill="both", expand=True)
        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", side="bottom")
        self.bar = bar
        self.seek = tk.Canvas(bar, height=22, bg=BG, highlightthickness=0, cursor="hand2")
        self.seek.pack(fill="x", padx=10, pady=(8, 2))
        self.seek.bind("<Button-1>", self._seek_press)
        self.seek.bind("<B1-Motion>", self._seek_drag)
        self.seek.bind("<ButtonRelease-1>", lambda e: setattr(self, "seeking", False))
        self.seek.bind("<Motion>", self._seek_hover)
        self.seek.bind("<Leave>", lambda e: (setattr(self, "hover_x", None), self._hide_thumb(), self._draw_seek()))
        self.seek.bind("<Configure>", lambda e: self._draw_seek())
        row = tk.Frame(bar, bg=BG)
        row.pack(fill="x", padx=10, pady=(0, 8))
        self.btn_play = _btn(row, "Play", self.toggle)
        self.btn_play.pack(side="left")
        _btn(row, "Stop", self.stop).pack(side="left", padx=(6, 0))
        _btn(row, "|<", lambda: self.step(-1)).pack(side="left", padx=(6, 0))
        _btn(row, ">|", lambda: self.step(1)).pack(side="left", padx=(6, 0))
        _btn(row, "-10s", lambda: self.rel(-10)).pack(side="left", padx=(6, 0))
        _btn(row, "+10s", lambda: self.rel(10)).pack(side="left", padx=(6, 0))
        self.time = tk.StringVar(value="0:00 / 0:00")
        tk.Label(row, textvariable=self.time, bg=BG, fg=FG, width=14).pack(side="left", padx=10)
        self.status = tk.StringVar()
        tk.Label(row, textvariable=self.status, bg=BG, fg="#9aa3b2", anchor="w").pack(side="left", fill="x", expand=True)
        _btn(row, "Open…", self.open_dialog).pack(side="right")
        _btn(row, "Full", lambda: self.fullscreen()).pack(side="right", padx=(0, 6))
        if self.on_popout:
            _btn(row, self.popout_label, self.on_popout).pack(side="right", padx=(0, 6))
        self.mb_aspect = _mb(row, "Aspect")
        self.mb_aspect.pack(side="right", padx=(0, 6))
        am = tk.Menu(self.mb_aspect, tearoff=0)
        self.aspect_i = tk.IntVar(value=0)
        for i, a in enumerate(ASPECTS):
            am.add_radiobutton(label=a[0], variable=self.aspect_i, value=i, command=lambda i=i: self.set_aspect(i))
        self.mb_aspect["menu"] = am
        self.mb_tracks = _mb(row, "Tracks")
        self.mb_tracks.pack(side="right", padx=(0, 6))
        self.tracks_menu = tk.Menu(self.mb_tracks, tearoff=0, postcommand=self._fill_tracks)
        self.mb_tracks["menu"] = self.tracks_menu
        self.mb_chap = _mb(row, "Chapters")
        self.mb_chap.pack(side="right", padx=(0, 6))
        self.chap_menu = tk.Menu(self.mb_chap, tearoff=0, postcommand=self._fill_chapters)
        self.mb_chap["menu"] = self.chap_menu
        self.mb_speed = _mb(row, "1x")
        self.mb_speed.pack(side="right", padx=(0, 6))
        sm = tk.Menu(self.mb_speed, tearoff=0)
        for sp in SPEEDS:
            sm.add_command(label="%gx" % sp, command=lambda v=sp: self.set_speed(v))
        self.mb_speed["menu"] = sm
        self.vol = tk.Scale(row, from_=0, to=130, orient="horizontal", length=90, showvalue=False, bg=BG, fg=FG,
                            troughcolor="#1d2129", highlightthickness=0, bd=0, command=self._vol)
        self.vol.set(80)
        self.vol.pack(side="right", padx=(0, 10))
        tk.Label(row, text="Vol", bg=BG, fg="#9aa3b2").pack(side="right")

    def bind_keys(self, w):
        def key(seq, fn):
            w.bind(seq, lambda e: (fn(), "break")[1] if self._key_ok(e) else None)
        key("<space>", self.toggle)
        key("<Left>", lambda: self.rel(-5)); key("<Right>", lambda: self.rel(5))
        key("<Shift-Left>", lambda: self.rel(-30)); key("<Shift-Right>", lambda: self.rel(30))
        key("<Up>", lambda: self.vol.set(min(130, self.vol.get() + 5))); key("<Down>", lambda: self.vol.set(max(0, self.vol.get() - 5)))
        key("<Prior>", lambda: self.chapter(-1)); key("<Next>", lambda: self.chapter(1))
        key("f", lambda: self.fullscreen()); key("<Escape>", lambda: self.fullscreen(False))
        key("m", lambda: self.mpv.set("mute", not self.mpv.get_flag("mute")))
        key("a", self.set_aspect); key("s", self.stop)
        key("n", lambda: self.step(1)); key("p", lambda: self.step(-1))
        key("z", lambda: self.sub_delay(-0.1)); key("x", lambda: self.sub_delay(0.1))
        key("<comma>", lambda: self.sub_scale(-0.1)); key("<period>", lambda: self.sub_scale(0.1))
        key("<Control-o>", self.open_dialog); key("<F1>", self.help); key("<question>", self.help)

    def _key_ok(self, e):
        """Shortcuts apply when this player has the focus (not while typing in another widget of the main window)."""
        try:
            w = self.top.focus_get()
        except Exception:
            return True
        return w is None or isinstance(w, (tk.Frame, tk.Canvas, tk.Toplevel, tk.Tk, tk.Button)) or str(w).startswith(str(self.parent))

    # ---- queue
    def play_index(self, i):
        if not 0 <= i < len(self.queue):
            return
        self._save_pos()
        self.index, self.cur = i, self.queue[i]
        self.prefetched = False
        self.stopped = False
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
        self.mb_aspect["text"] = "Aspect" if i == 0 else name.split(" ")[0]
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
        self.mb_speed["text"] = "%gx" % v

    def sub_delay(self, d):
        self.mpv.set("sub-delay", self.mpv.get_double("sub-delay") + d)
        self.status.set("Subtitle delay %+.1f s" % self.mpv.get_double("sub-delay"))

    def sub_scale(self, d):
        self.mpv.set("sub-scale", max(0.3, self.mpv.get_double("sub-scale", 1.0) + d))
        self.status.set("Subtitle size %d%%" % round(self.mpv.get_double("sub-scale", 1.0) * 100))

    def _vol(self, v):
        if self.mpv:
            self.mpv.set("volume", v)

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
                m.add_command(label="  Off", command=lambda: self.mpv.set("sid", "no"))
            for t in self.mpv.tracks() if self.mpv else []:
                if t["type"] == kind:
                    name = " ".join(x for x in (t["lang"], t["title"]) if x) or "Track %d" % t["id"]
                    m.add_command(label=("\u25cf " if t["selected"] else "  ") + name,
                                  command=lambda p=prop, i=t["id"]: self.mpv.set(p, i))
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
        return self.dur * min(1.0, max(0.0, x / max(1, self.seek.winfo_width())))

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
        self._draw_seek()
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
        x_root, y_root = self.seek.winfo_rootx() + ev.x, self.seek.winfo_rooty()
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
        c = self.seek
        c.delete("all")
        w, h = max(1, c.winfo_width()), c.winfo_height()
        y0, y1 = h // 2 - 3, h // 2 + 3
        c.create_rectangle(0, y0, w, y1, fill="#1d2129", width=0)
        if self.size and self.ranges:                      # downloaded parts of a streaming file
            for a, b in self.ranges:
                c.create_rectangle(w * a / self.size, y0, w * b / self.size, y1, fill=BUF, width=0)
        if self.dur:
            if pos is None:
                pos = self.mpv.get_double("time-pos") if self.mpv else 0
            x = w * min(1.0, pos / self.dur)
            c.create_rectangle(0, y0, x, y1, fill=ACC, width=0)
            for t, _title in self.chapters[1:]:             # chapter marks
                cx = w * t / self.dur
                c.create_line(cx, y0 - 2, cx, y1 + 2, fill="#8a93a5")
            c.create_oval(x - 6, h // 2 - 6, x + 6, h // 2 + 6, fill=FG, width=0)
            if self.hover_x is not None:
                t = self._x_to_time(self.hover_x)
                label = fmt_time(t)
                for ct, title in reversed(self.chapters):
                    if ct <= t:
                        label += "  " + title[:30]
                        break
                tx = min(max(self.hover_x, 50), w - 50)
                c.create_text(tx, 1, text=label, anchor="n", fill=FG, font=("Segoe UI", 8))

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
                elif ev == "end-file" and self.dur and m.get_double("time-pos") >= self.dur - 2:
                    self._ended()
                elif ev.startswith("msg:"):
                    {"tk-fs": lambda: self.fullscreen(), "tk-fs-off": lambda: self.fullscreen(False),
                     "tk-aspect": self.set_aspect, "tk-stop": self.stop, "tk-next": lambda: self.step(1), "tk-prev": lambda: self.step(-1), "tk-help": self.help}.get(ev[4:], lambda: None)()
            self.dur = m.get_double("duration") or self.dur
            pos = m.get_double("time-pos")
            paused = m.get_flag("pause")
            v = m.get_double("volume", self.vol.get())          # changed with the keys / wheel inside the video
            if abs(v - float(self.vol.get())) >= 1:
                self.vol.configure(command=lambda x: None)
                self.vol.set(v)
                self.vol.configure(command=self._vol)
            self.btn_play["text"] = "Play" if (paused or self.stopped) else "Pause"
            self.time.set("%s / %s" % (fmt_time(pos), fmt_time(self.dur)))
            waiting = m.get_flag("paused-for-cache")
            msg = self._net_status(pos, waiting) if self.cur and self.cur.get("map") else ""
            if msg or waiting:
                self.status.set(msg or "Waiting for the download\u2026")
            elif self.status.get().startswith(("Loading", "Buffering", "Waiting")) or "downloaded ahead" in self.status.get():
                self.status.set("")
            if not self.seeking:
                self._draw_seek(pos)
            if self.dur and pos > self.dur * 0.85 and not self.prefetched:
                self._prefetch_next()
            if time.time() - self._saved > 5:
                self._save_pos()
        except Exception:
            pass
        self.top.after(250, self._tick)

    def _loaded(self):
        m, e = self.mpv, self.cur or {}
        self.dur = m.get_double("duration")
        self.chapters = m.chapters()
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
        self._save_pos(final=True)
        if self.index + 1 < len(self.queue):
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
                 queue=None, index=0, session=None, on_dock=None):
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
        self.panel = PlayerPanel(self.win, self.win, q, index, session, lib, options, on_fullscreen=self.fullscreen,
                                 on_popout=(lambda: on_dock(self)) if on_dock else None, popout_label="Attach")
        self.mpv = self.panel.mpv
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        if self.panel.error:
            self.win.after(100, lambda: self._no_lib(target, self.panel.error))
            return
        self.win.bind("<Motion>", self._motion)
        self.panel.video.bind("<Double-Button-1>", lambda e: self.fullscreen())
        self.panel.video.bind("<Button-1>", lambda e: self.panel.toggle())
        self.win.after(500, self._hide_bar_tick)

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
        p = self.panel
        self._cursor(False)
        if self.panel.mpv:
            self.panel.mpv.set("cursor-autohide", 1000 if self.full else "no")
        self.last_move = time.time()
        if self.full:
            p.bar.pack_forget()
        else:
            p.bar.pack(fill="x", side="bottom")
            p.video.pack_forget(); p.video.pack(fill="both", expand=True)

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
        p = self.panel
        if self.full and not p.bar.winfo_ismapped():
            p.bar.pack(fill="x", side="bottom")
        self._cursor(False)
        self.last_move = time.time()

    def _hide_bar_tick(self):
        p = self.panel
        if self.full and p.bar.winfo_ismapped() and time.time() - self.last_move > 2.5:
            p.bar.pack_forget()
        if self.full and time.time() - self.last_move > 2.5:
            self._cursor(True)
        if p.mpv:
            self.win.after(500, self._hide_bar_tick)

    def close(self):
        self.panel.close()
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
