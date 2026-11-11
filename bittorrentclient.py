"""Bit Torrent Client (GUI).
Run: python bittorrentclient.py [file.torrent | magnet:?xt=... ...]"""
import collections
import datetime
import json
import os
import queue
import random
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import arttable
import autostart as autostart_mod
import crashlog
import dnd
import firewall
import geo
import netcheck
import panels
import rss
import schedule
import shutil
import media_state
import single_instance
import tempfile
import threading
import tray
import chrome
import embed
import ui
import version as version_mod
import dialogs
from dialogs import (AboutDialog, BannedDialog, FeedEditDialog, FindFeedsDialog, RSSDialog, ENC_CHOICES, THEME_CHOICES, UTP_CHOICES, AddDialog, CreateTorrentDialog, DiagnosticsDialog,
                     FilesDialog, SearchWindow, SettingsDialog, TorrentLimitDialog)
from torrent_core import LOW_SPACE, Manager, Session, Torrent
from ui import ToolButton, Tooltip, fmt_eta, fmt_rate, fmt_size

APP = "Bit Torrent Client"
COLS = (("name", "Name", "w"), ("size", "Size", "w"), ("progress", "Progress", "w"), ("status", "Status", "w"),
        ("down", "Down", "w"), ("up", "Up", "w"), ("peers", "Peers", "w"), ("eta", "ETA", "w"))   # all left-aligned
SORT_KEYS = {
    "name": lambda s: s["name"].casefold(), "size": lambda s: s["size"], "progress": lambda s: s["progress"],
    "status": lambda s: s["raw_state"], "category": lambda s: (s.get("category") or "\uffff").casefold(), "down": lambda s: s["down"], "up": lambda s: s["up"],
    "peers": lambda s: s["peers"], "eta": lambda s: s["eta"] if s["eta"] is not None else float("inf"),
}
RUNNING = ("Downloading", "Finished", "Seeding", "Checking", "Fetching metadata")
BAR_KIND = {"Downloading": "Down", "Finished": "Seed", "Seeding": "Seed", "Checking": "Check", "Fetching metadata": "Check",
            "Paused": "Idle", "Queued": "Idle", "Error": "Err"}


APP_DIR = "BitTorrentClient"   # folder name under %APPDATA% (the display name has spaces)
LEGACY_DIRS = ("PyTorrent",)   # earlier name of this app: its saved torrents and settings are carried over


def data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_DIR)
    if not os.path.exists(d):
        for old in LEGACY_DIRS:
            old_path = os.path.join(base, old)
            if os.path.isdir(old_path):
                try:
                    os.rename(old_path, d)       # keep the user's torrents, progress and settings
                except OSError:
                    import shutil
                    try:
                        shutil.copytree(old_path, d)
                    except OSError:
                        pass
                break
    os.makedirs(os.path.join(d, "torrents"), exist_ok=True)
    os.makedirs(os.path.join(d, "resume"), exist_ok=True)
    return d


def random_port():
    # below Windows' dynamic range (49152+), where Hyper-V / WSL / Docker reserve port blocks that cannot be bound
    return random.randint(20000, 40000)


def rate0(r):
    return fmt_rate(r) if r >= 1 else "0 B/s"


class _DrawnButton:
    """A button drawn on a canvas over the window's artwork: no box of its own at rest (a `raised` one keeps a light
    face), a light fill when pointed at or pressed. Has the ToolButton bits the App uses (restyle, set_enabled)."""

    def __init__(self, app, key, icon, text, command, tip, raised=False):
        self.app, self.key, self.icon, self.text, self.command, self.tip = app, key, icon, text, command, tip
        self.raised, self.enabled, self.P = raised, True, None
        self.flat = True                 # never a fill - pointed at, its text (or icon) turns bold instead
        self.x0 = self.y0 = self.x1 = self.y1 = 0
        self.on_change = None            # called when it needs redrawing (enabled state changed)

    @property
    def clickable(self):
        return self.enabled

    def restyle(self, P):
        self.P = P                       # its canvas is redrawn by the App

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            if self.on_change:
                self.on_change()

    def need(self):
        F = self.app.F
        if not self.text:
            return int(F.icon.measure(self.icon)) + 28
        w = F.ui.measure(self.text) + 14
        w += (12 + F.icon.measure(self.icon) + 6) if self.icon else 14
        return int(w)

    def draw(self, cv, hover=False, down=False, tags=()):
        app, F = self.app, self.app.F
        P = app.P
        x0, y0, x1, y1 = self.x0, self.y0, self.x1, self.y1
        fill = None
        if self.flat:
            pass
        elif not self.enabled:
            fill = P["hover"] if self.raised else None
        elif self.raised:
            fill = P["sel"] if down else P["press"] if hover else P["hover"]
        elif down or hover:
            fill = P["press"] if down else P["hover"]
        if fill:
            cv.create_rectangle(x0, y0, x1, y1, width=0, fill=fill, tags=tags)
        fg = P["fg"] if self.enabled else P["disabled"]
        bold = self.flat and self.enabled and (hover or down)
        mid = (y0 + y1) // 2
        if not self.text:
            cx = (x0 + x1) // 2
            cv.create_text(cx, mid, text=self.icon, font=F.icon, fill=fg, tags=tags)
            if bold:                                            # icons have no bold: draw it twice, 1 px apart
                cv.create_text(cx + 1, mid, text=self.icon, font=F.icon, fill=fg, tags=tags)
            return
        x = x0 + (12 if self.icon else 14)
        if self.icon:
            cv.create_text(x, mid, anchor="w", text=self.icon, font=F.icon, fill=fg, tags=tags)
            x += int(F.icon.measure(self.icon)) + 6
        cv.create_text(x, mid, anchor="w", text=self.text, font=F.bold if bold else F.ui, fill=fg, tags=tags)


class _DrawnText:
    """A piece of status-bar text drawn on the artwork. Label-like configure/cget (text, fg); optional click + tip."""

    def __init__(self, redraw, fg_key, text="", command=None, tip=None):
        self.opts, self.fg_key, self._redraw = {"text": text}, fg_key, redraw
        self.command, self.tip = command, tip
        self.x0 = self.y0 = self.x1 = self.y1 = 0

    clickable = property(lambda self: self.command is not None)

    def configure(self, **kw):
        changed = any(self.opts.get(k) != v for k, v in kw.items())
        self.opts.update(kw)
        if changed:
            self._redraw()
    config = configure

    def cget(self, k):
        return self.opts.get(k)

    def colour(self, P):
        return self.opts.get("fg") or P[self.fg_key]


class _DrawnBar:
    """Pointer handling for things drawn on a canvas (toolbar buttons, status-bar links): hover and press fills, a
    click on release over the same item, the hand cursor, and one tooltip placed under (or above) the item."""

    def __init__(self, app, cv, items, redraw, above=False):
        self.app, self.cv, self.items, self.redraw = app, cv, items, redraw
        self.hover = self.down = None
        self.tip = Tooltip(cv, self._tip_text, lambda: app.P, above=above, region=self._region)
        cv.bind("<Motion>", self._motion, add="+")
        cv.bind("<Leave>", lambda e: self._motion(None), add="+")
        cv.bind("<ButtonPress-1>", self._press, add="+")
        cv.bind("<ButtonRelease-1>", self._release, add="+")

    def _tip_text(self):
        t = getattr(self.hover, "tip", None)
        return t() if callable(t) else (t or "")

    def item_at(self, e):
        if e is None:
            return None
        for it in self.items():
            if it.clickable and it.x0 <= e.x < it.x1 and it.y0 <= e.y < it.y1:
                return it
        return None

    def _region(self):
        it = self.hover
        if it is None:
            return None
        x, y = self.cv.winfo_rootx(), self.cv.winfo_rooty()
        return x + it.x0, y + it.y0, x + it.x1, y + it.y1

    def _motion(self, e):
        it = self.item_at(e)
        if it is not self.hover:
            self.hover = it
            self.cv.configure(cursor="hand2" if it else "")
            if self.down is not None and it is not self.down:
                self.down = None
            self.tip._hide()
            if it is not None:
                self.tip._schedule()
            self.redraw()

    def _press(self, e):
        self.tip._hide()
        it = self.item_at(e)
        if it is not None:
            self.down = it
            self.redraw()

    def _release(self, e):
        d, self.down = self.down, None
        if d is None:
            return
        self.redraw()
        if self.item_at(e) is d:
            d.command()


class _SideText:
    """Text of a drawn sidebar row. Label-like configure/cget, so counts update the same way; a change redraws."""

    def __init__(self, text, redraw=None):
        self.opts, self._redraw = {"text": text}, redraw

    def configure(self, **kw):
        changed = any(self.opts.get(k) != v for k, v in kw.items())
        self.opts.update(kw)
        if changed and self._redraw is not None:
            self._redraw()
    config = configure

    def cget(self, k):
        return self.opts.get(k)


class App:
    MIN_COL = 55   # narrowest a column may get

    def __init__(self, root, inbox):
        self.root, self.inbox = root, inbox
        self.dir = data_dir()
        crashlog.install(self.dir, version_mod.VERSION, root)
        self.state_file = os.path.join(self.dir, "state.json")
        self._media_cache, self._offered_lib = (0, {}), False
        self.cfg = {"download_dir": os.path.join(os.path.expanduser("~"), "Downloads"), "torrents": [],
                    "down_limit": 0, "up_limit": 0, "encryption": "light", "utp": "fallback",
                    "portmap": True, "theme": "dark", "port": 0, "tray": True, "notify": True, "max_active": 0,
                    "seed_ratio": 0, "seed_minutes": 0, "seed_action": "pause", "done_dir": "", "categories": {},
                    "update_repo": "", "update_check": True, "update_last": 0, "search_source": "",
                    "alt_down": 0, "alt_up": 0, "alt_on": False, "sched_on": False, "sched_start": "08:00",
                    "sched_end": "23:00", "sched_days": list(range(7)), "banned": [], "rss": [], "rss_seen": {},
                    "player": "builtin", "libmpv": "", "player_exe": "", "player_where": "tab",
                    "hwdec": True, "skip_extras": True, "lan_share": False, "alang": "", "slang": "", "os_lang": "en",
                    "os_key": "", "os_user": "", "os_pass": "", "tmdb_key": "", "lan_hours": 8, "thumbs": True, "notify_sound": True, "native_frame": False}
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
            if "portmap" not in saved and "upnp" in saved:  # setting from an older version
                saved["portmap"] = saved.pop("upnp")
            self.cfg.update(saved)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):                 # unreadable settings: keep the file instead of overwriting it on the next save
            try:
                shutil.copyfile(self.state_file, self.state_file + ".damaged-%d" % int(time.time()))
            except OSError:
                pass
        else:
            try:
                shutil.copyfile(self.state_file, self.state_file + ".bak")      # last settings that loaded fine
            except OSError:
                pass
        if self.cfg["encryption"] not in ENC_CHOICES.values():
            self.cfg["encryption"] = "light"
        if self.cfg["utp"] not in UTP_CHOICES.values():
            self.cfg["utp"] = "fallback"
        try:
            self.cfg["port"] = int(self.cfg["port"])
        except (TypeError, ValueError):
            self.cfg["port"] = 0
        if not 1024 <= self.cfg["port"] <= 65535:   # first run (or a bad value): pick a random high port once, then keep it
            self.cfg["port"] = random_port()
            self._port_new = True
        if self.cfg["theme"] not in THEME_CHOICES.values():
            self.cfg["theme"] = "dark"
        if self.cfg.get("ui_ver", 0) < 3:             # 1.9.2: play inside the Player tab by default (Detach pops it out)
            self.cfg["player_where"], self.cfg["ui_ver"] = "tab", 3
        if self.cfg.get("theme_ver", 0) < 2:          # 1.9: Aurora is the look of the app; switch once, the Settings choice still works
            self.cfg["theme"], self.cfg["theme_ver"] = "dark", 2

        root.title(APP)
        self.F = ui.Fonts(root)
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)   # high-DPI displays
        self.P = ui.palette(self.cfg["theme"])
        self._themed = []         # (widget, {option: palette key}) repainted on theme change
        self.sort_col, self.sort_rev = None, False
        self.progress, self.bar_kind = {}, {}     # per torrent: percent done, bar colour kind (drawn by the list)
        self._snap_by_hash = {}
        self._empty_shown = None
        self._pf_full = ""
        self.filter_key = "all"
        self.side_rows = {}
        self._hidden = set()          # rows hidden by the sidebar filter / search box
        self.graph = panels.SpeedGraph()
        self._tab = "Overview"
        self._tab_hash = None
        self._tab_cache = {}
        self._peer_prev = {}
        self.log_lines = collections.deque(maxlen=500)     # (time text, message), newest last
        self._log_state = {}                               # per torrent / per topic: last state we logged
        self._speed_mode = None
        self.tray = tray.Tray(self.show_window, self.quit,
                              icon_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), "BitTorrentClient.ico"))
        self._tray_told = False
        self.finished = queue.Queue()
        self.seedq = queue.Queue()
        self.updq = queue.Queue()
        self.rssq = queue.Queue()
        self._rss_busy = set()
        self.fw_state = None      # 'allowed' | 'blocked' | 'missing' | None (unknown)
        self.net_state = None     # netcheck.check() result once the background check has run
        self._set_icon()
        ui.style_ttk(root, self.P, self.F, self.px)
        dialogs.install_messageboxes(self)
        root.bind("<Unmap>", lambda e: ui.Tooltip.hide_all() if e.widget is root else None, add="+")
        root.bind("<FocusOut>", lambda e: ui.Tooltip.hide_all() if e.widget is root else None, add="+")
        self.chrome = chrome.attach(root, self.P, self.px, APP, main=True, font=self.F.bold, snap=True,
                                    buttons=("full", "min", "max", "close")) \
            if chrome.enabled(self.cfg) else None       # snap: real Windows frame behind the Aurora strip (Snap Layouts etc.)
        if self.chrome is not None:
            self.chrome.art = lambda c: self._art_at(c, self.root)   # the title strip shows its slice of the artwork

        # The status bar and details panel are packed against the bottom *before* the table, so when the window is
        # short it is the table that shrinks - the info bar can never be pushed off the bottom of the window.
        # No lines between the parts: one artwork runs behind the whole window, title bar to status bar.
        self._build_toolbar()
        self._build_statusbar()
        self._build_details()
        main = self._main = self._frame(self.root, "bg")
        main.pack(fill="both", expand=True)
        self._build_sidebar(main)
        self._fit_column()
        self._build_table(main)          # no dividing line: the aurora runs from the column straight into the list
        root.bind("<Configure>", lambda e: self._later("art", self._art_moved) if e.widget is root else None, add="+")
        self._build_menu()
        for seq, fn in (("<Control-o>", self.add_dialog), ("<Control-v>", self.paste_magnet),
                        ("<Control-f>", lambda: self.search_entry.focus_set()),
                        ("<Control-F>", self.search_online)):
            root.bind(seq, lambda e, fn=fn: fn())
        self.tree.bind("<Delete>", lambda e: self.remove())
        self.tree.bind("<space>", self._on_space)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        # clicks on the drawn parts (canvases) do not take the keyboard focus: make sure the window has it
        root.bind("<ButtonPress>", lambda e: root.focus_set() if root.focus_get() is None else None, add="+")
        root.bind("<Escape>", lambda e: self.toggle_fullscreen(False) if self.is_fullscreen() and
                  e.widget is not getattr(self, "search_entry", None) else None, add="+")
        self.apply_theme()

        self.mgr = Manager(self.cfg["port"], encryption=self.cfg["encryption"],
                           utp=self.cfg["utp"], down_limit=self.cfg["down_limit"],
                           up_limit=self.cfg["up_limit"], portmap=self.cfg["portmap"])
        self.mgr.banned = {str(ip) for ip in self.cfg.get("banned", [])}
        self.mgr.on_metadata = self.on_metadata
        self.mgr.on_finished = lambda h, name: self.finished.put((h, name))   # network thread -> queue -> poll()
        self.mgr.on_seed_limit = lambda h, action: self.seedq.put((h, action))
        self.mgr.set_seed_rules(self.cfg["seed_ratio"], self.cfg["seed_minutes"], self.cfg["seed_action"])
        self.mgr.set_max_active(self.cfg.get("max_active", 0))
        self._place_window()
        root.after(400, lambda: dnd.enable(root, self.on_drop))
        self.restore()
        self.log("Started version %s" % version_mod.VERSION)
        self._apply_speed()
        if getattr(self, "_port_new", False):
            self.save()   # remember the random port so it stays the same on every start
        self.refresh()
        self.poll()
        self.check_firewall()
        self.root.after(5000, self.check_network)
        self.root.after(30000, self._resume_tick)
        self.root.after(8000, lambda: self.check_updates(manual=False))

    # -- activity log -----------------------------------------------------
    def log(self, text):
        self.log_lines.append((datetime.datetime.now().strftime("%H:%M:%S"), text))
        if getattr(self, "_tab", "") == "Log":
            self._update_tabs()

    def _log_changes(self, snap):
        """Note errors that appear, and port-forwarding changes, in the activity log."""
        seen = self._log_state
        for s in snap:
            key = ("err", s["hash"])
            if s["error"] and seen.get(key) != s["error"]:
                self.log("%s: %s" % (s["name"], s["error"]))
            seen[key] = s["error"]
        pm = self._pf_summary()[0]
        if seen.get("pm") != pm:
            if "pm" in seen:
                self.log("Router: %s" % pm)
            seen["pm"] = pm

    # -- bandwidth schedule / alternative speed ----------------------------
    def _apply_speed(self):
        """Use the normal limits, the alternative limits, or whatever the schedule says right now."""
        if self.cfg.get("sched_override") and not schedule.in_window(self.cfg):
            self.cfg["sched_override"] = False          # the window the user switched off has ended
        down, up, mode = schedule.effective(self.cfg)
        if (down, up) != (self.mgr.down.rate // 1024, self.mgr.up.rate // 1024):
            self.mgr.set_limits(down, up)
        if mode != self._speed_mode:
            if self._speed_mode is not None:
                self.log({"normal": "Normal speed limits restored", "alt": "Alternative speed limits switched on",
                          "schedule": "Alternative speed limits switched on by the schedule"}[mode])
            self._speed_mode = mode
        self._update_alt_label()

    def _update_alt_label(self):
        if not hasattr(self, "s_alt"):
            return
        mode = self._speed_mode or "normal"
        text = {"normal": "Alt speed: off", "alt": "Alt speed: on", "schedule": "Alt speed: scheduled"}[mode]
        self.s_alt.configure(text=text, fg=self.P["fg"] if mode != "normal" else self.P["muted"])

    def toggle_alt(self):
        """Status-bar switch: manual alternative speed on/off (off also overrides a running schedule window)."""
        if self._speed_mode == "normal":
            self.cfg["alt_on"], self.cfg["sched_override"] = True, False
        else:
            self.cfg["alt_on"] = False
            self.cfg["sched_override"] = self._speed_mode == "schedule"
        self._apply_speed()
        self.save()

    # -- peers ------------------------------------------------------------
    def ban_peer(self, addr):
        ip = addr.rsplit(":", 1)[0]
        self.mgr.ban(ip)
        self.log("Banned peer %s" % ip)
        self.save()

    def unban(self, ip):
        self.mgr.unban(ip)
        self.log("Unbanned peer %s" % ip)
        self.save()

    # -- disk space -------------------------------------------------------
    @staticmethod
    def free_space(path):
        path = os.path.abspath(path)
        while path and not os.path.isdir(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def _space_ok(self, raw, folder, skip, ask=True):
        """True if the torrent fits (or the user says go ahead anyway)."""
        try:
            t = Torrent(raw)
        except (KeyError, TypeError, IndexError, ValueError):
            return True        # the add step reports bad torrents
        need = sum(f[1] for i, f in enumerate(t.files) if i not in set(skip or ()))
        free = self.free_space(folder)
        if free is None or need + LOW_SPACE <= free:
            return True
        if not ask:
            self.log("Skipped %s: needs %s but only %s is free" % (t.name, fmt_size(need), fmt_size(free)))
            return False
        return messagebox.askyesno(APP, "'%s' needs %s but only %s is free on that drive.\n\nAdd it anyway?"
                                   % (t.name, fmt_size(need), fmt_size(free)), icon="warning", parent=self.root)

    # -- small helpers ----------------------------------------------------
    def px(self, n):
        return int(round(n * self.scale))

    def _place_window(self):
        """Size the window so everything (including the info bar) fits, and open it in the middle of the usable
        screen area (the part not covered by the taskbar)."""
        r = self.root
        r.update_idletasks()
        ax, ay, aw, ah = ui.work_area(r)
        need_h = r.winfo_reqheight()
        w = min(self.px(1120), aw - self.px(40))
        h = min(max(self.px(740), need_h), ah - self.px(20))
        # small enough to snap side by side / into quarters (half a 1080p-class screen at any scaling); the sidebar
        # scrolls and the list shrinks when the window is that small
        r.minsize(min(self.px(700), w), min(self.px(400), h))
        r.geometry("%dx%d+%d+%d" % (w, h, ax + (aw - w) // 2, ay + (ah - h) // 2))

    def _set_icon(self):
        try:
            import icon_data
            self._icon = tk.PhotoImage(data=icon_data.PNG_B64)
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    def _frame(self, parent, bg, **kw):
        f = tk.Frame(parent, bd=0, highlightthickness=0, **kw)
        self._themed.append((f, {"bg": bg}))
        return f

    def _label(self, parent, bg, fg, font, **kw):
        lb = tk.Label(parent, font=font, bd=0, **kw)
        self._themed.append((lb, {"bg": bg, "fg": fg}))
        return lb

    def _line(self, side="top"):
        self._frame(self.root, "border", height=1).pack(fill="x", side=side)

    # -- layout -----------------------------------------------------------
    def _tool(self, parent, key, icon, text, cmd, tip, accent=False, pady=8, raised=False):
        b = ToolButton(parent, self.F, icon, text, cmd, accent, pady=self.px(pady) if pady != 8 else pady, raised=raised)
        Tooltip(b, tip, lambda: self.P)
        self.btn[key] = b
        return b

    def _later(self, name, fn):
        """Run fn once after this round of updates, however often it is asked for (coalesced redraws)."""
        pend = self.__dict__.setdefault("_pending_draws", set())
        if name in pend:
            return
        pend.add(name)

        def go():
            pend.discard(name)
            fn()
        try:
            self.root.after_idle(go)
        except (tk.TclError, AttributeError):
            pend.discard(name)

    def _btn_h(self):
        F = self.F
        return max(int(F.ui.metrics("linespace")), int(F.icon.metrics("linespace")) if F.has_icons else 0) + 16

    def _build_toolbar(self):
        """The toolbar is a canvas over the window's one artwork: Exit / Restart / Settings over the column, the
        torrent actions from where the list starts, and the search box (a real entry) on the right."""
        cv = self.toolbar = tk.Canvas(self.root, bd=0, highlightthickness=0, height=self._btn_h() + 16)
        self._themed.append((cv, {"bg": "bg"}))
        cv.pack(fill="x")
        self.btn = {}
        self._tb_items = []

        def add(key, icon, text, cmd, tip, raised=False, left=False):
            b = _DrawnButton(self, key, icon, text, cmd, tip, raised=raised)
            b.left, b.on_change = left, lambda: self._later("toolbar", self._draw_toolbar)
            self.btn[key] = b
            self._tb_items.append(b)

        # over the column: exit / restart / settings (icons only, no boxes)
        add("exit", ui.ICON_EXIT, "" if self.F.has_icons else "Exit", self.exit_app,
            "Exit Bit Torrent Client completely (the window's close button only hides it to the tray)", left=True)
        add("restart", ui.ICON_RESTART, "" if self.F.has_icons else "Restart", self.restart, "Restart Bit Torrent Client",
            left=True)
        add("settings", ui.ICON_SETTINGS, "" if self.F.has_icons else "Settings", self.settings_dialog,
            "Settings: speed limits, encryption, network, theme", left=True)
        # over the list: actions on the selected torrents
        add("resume", ui.ICON_PLAY, "Resume", self.resume, "Resume the selected torrents")
        add("pause", ui.ICON_PAUSE, "Pause", self.pause, "Pause the selected torrents  (Space)")
        add("remove", ui.ICON_DELETE, "Remove", self.remove, "Remove the selected torrents  (Delete)")
        add("files", ui.ICON_FILES, "Files", self.files_dialog, "Choose which files of the selected torrent to download")
        add("folder", ui.ICON_FOLDER, "Open folder", self.open_folder, "Show the download folder")

        # search box, top right: magnifier icon + entry (a real widget, placed on the canvas)
        self.search = tk.StringVar(value="")
        box = tk.Frame(cv, bd=0, highlightthickness=1)
        self._themed.append((box, {"bg": "bg", "highlightbackground": "border", "highlightcolor": "muted"}))
        if self.F.has_icons:
            icon = tk.Label(box, text=ui.ICON_SEARCH, font=self.F.icon, bd=0)
        else:
            icon = tk.Label(box, text="⌕", font=self.F.big, bd=0)
        self._themed.append((icon, {"bg": "bg", "fg": "muted"}))
        entry = tk.Entry(box, textvariable=self.search, bd=0, relief="flat", width=26, font=self.F.ui,
                         highlightthickness=0)
        self._themed.append((entry, {"bg": "bg", "fg": "fg", "insertbackground": "fg"}))
        icon.pack(side="left", padx=(10, 4))
        entry.pack(side="left", fill="y", padx=(0, 10), pady=6, ipady=3)
        self._search_item = cv.create_window(0, 0, window=box, anchor="e")
        icon.bind("<Button-1>", lambda e: entry.focus_set())
        entry.bind("<Escape>", lambda e: self.search.set(""))
        entry.bind("<Return>", lambda e: self.search_online(self.search.get().strip()) if self.search.get().strip() else None)
        self.search_entry = entry
        self.search.trace_add("write", lambda *_: self.refresh_now())
        Tooltip(box, lambda: "Filter the list by name  (Ctrl+F).  Press Enter to search for it online.", lambda: self.P)
        self._tb_bar = _DrawnBar(self, cv, lambda: self._tb_items, self._draw_toolbar)
        cv.bind("<Configure>", lambda e: self._draw_toolbar(), add="+")

    def _draw_toolbar(self):
        cv, P = self.toolbar, self.P
        try:
            cv.delete("deco")
            self._art_at(cv, self.root, tags="deco")
            w, bh = cv.winfo_width(), self._btn_h()
            top = (int(cv.cget("height")) - bh) // 2
            side = getattr(self, "_side", None)
            colw = side.winfo_width() if side is not None and side.winfo_width() > 1 else self.px(176)
            left = [b for b in self._tb_items if b.left]
            slot = colw / max(1, len(left))                   # three equal slots over the column, buttons centred
            for i, b in enumerate(left):
                bw = b.need()
                b.x0 = int(slot * i + (slot - bw) / 2)
                b.x1, b.y0, b.y1 = b.x0 + bw, top, top + bh
            x = colw + self.px(8)
            acts = [b for b in self._tb_items if not b.left]
            tree = getattr(self, "tree", None)
            if acts and tree is not None and tree.winfo_width() > 1:
                # the first action's icon starts exactly above the list's "Name" heading
                name_x = tree.winfo_rootx() - cv.winfo_rootx() + int(getattr(tree, "inset", 0)) + 8
                x = name_x - (12 if acts[0].icon else 14)
            for b in acts:
                if b.key == "files":                          # a little gap (no line) between the two groups
                    x += 17
                b.x0, b.y0, b.x1, b.y1 = x, top, x + b.need(), top + bh
                x = b.x1 + 4
            bar = self._tb_bar
            for b in self._tb_items:
                b.draw(cv, hover=bar.hover is b, down=bar.down is b, tags="deco")
            cv.coords(self._search_item, w - 12, (top + top + bh) // 2)
            cv.tag_lower("deco")
        except (tk.TclError, TypeError, ValueError, AttributeError):
            pass

    def _fit_column(self):
        """The sidebar (and the toolbar part above it) is as wide as its widest item plus a little margin."""
        try:
            w = max(self.px(140), self._side_need() + self.px(14))
            self._side.configure(width=w)
            self._later("toolbar", self._draw_toolbar)      # its left buttons sit over the column
        except (tk.TclError, TypeError, ValueError, AttributeError):
            pass

    def _side_need(self):
        """Width the column's contents ask for: the widest button, or the widest filter row (bold name + count)."""
        need = max([b.need() + 2 * self.px(10) for b in self._side_btns] or [0])
        for _row, name, _count in self.side_rows.values():
            need = max(need, int(self.F.bold.measure(name.cget("text"))) + int(self.F.small.measure("000"))
                       + 2 * (self.px(6) + self.px(14)))
        return need

    def _build_sidebar(self, parent):
        """The left column is one canvas with nothing opaque on it: the aurora runs under it and on into the list.
        Buttons, separators and filter rows are all drawn; only a pointed-at or selected item gets a fill."""
        side = self._side = self._frame(parent, "bg", width=self.px(176))       # width: _fit_column()
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        # the column scrolls (mouse wheel) when the window is too short for all of it
        cv = self.side_cv = tk.Canvas(side, bd=0, highlightthickness=0, width=1, height=1)
        self._themed.append((cv, {"bg": "bg"}))
        cv.pack(fill="both", expand=True)
        self._side_btns, self._side_hits, self._side_hover, self._side_h = [], [], None, 0
        self._side_down = None
        for key, icon, text, cmd, tip in (
                ("add", ui.ICON_ADD, "Add torrent", self.add_dialog, "Add .torrent files  (Ctrl+O)"),
                ("magnet", ui.ICON_LINK, "Magnet link", self.add_magnet_dialog, "Add a magnet link  (Ctrl+V)"),
                ("search", ui.ICON_SEARCH, "Search online", self.search_online, "Search for torrents online  (Ctrl+Shift+F)"),
                ("create", ui.ICON_CREATE, "Create torrent", self.create_torrent,
                 "Make a .torrent from a file or folder you want to share"),
                ("rss", ui.ICON_RSS, "RSS feeds", self.rss_dialog, "Add torrents automatically from RSS / Atom feeds"),
                ("library", ui.ICON_LIBRARY, "Library", self.library_dialog, "Everything you can watch, with continue-watching")):
            b = _DrawnButton(self, key, icon if self.F.has_icons else "", text, cmd, tip)
            b.flat = True                       # the column has no boxes: pointed at, the text turns bold
            self._side_btns.append(b)
            self.btn[key] = b
        for key, text in panels.FILTERS:
            self._side_row(key, text)
        self._cat_names = ()
        self.side = side
        # one tooltip for the canvas: it shows the pointed-at button's text, under that button
        self._side_tip = Tooltip(cv, lambda: self._side_hover.tip if isinstance(self._side_hover, _DrawnButton) else "",
                                 lambda: self.P, region=self._side_tip_region)

        def wheel(e):
            if self._side_h > cv.winfo_height():
                cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
                self._draw_sidebar()
        cv.configure(yscrollincrement=self.px(30))
        self._side_wheel = lambda w=cv: self._bind_wheel(w, wheel)
        cv.bind("<Configure>", lambda e: self._layout_sidebar())
        cv.bind("<Motion>", lambda e: self._side_motion(e))
        cv.bind("<Leave>", lambda e: self._side_motion(None))
        cv.bind("<ButtonPress-1>", lambda e: self._side_press(e))
        cv.bind("<ButtonRelease-1>", lambda e: self._side_release(e))
        self._side_wheel()

    def _side_tip_region(self):
        """Screen rectangle of the pointed-at button (where its tooltip belongs), or None."""
        b = self._side_hover
        if not isinstance(b, _DrawnButton):
            return None
        cv = self.side_cv
        x, y = cv.winfo_rootx(), cv.winfo_rooty() - int(cv.canvasy(0))
        return x + self.px(10), y + b.y0, x + cv.winfo_width() - self.px(10), y + b.y1

    def _layout_sidebar(self):
        """Work out where the buttons, separators and filter rows go (drawn by _draw_sidebar)."""
        cv, px = self.side_cv, self.px
        try:
            w = cv.winfo_width()
            y = px(12)
            bh = max(int(self.F.ui.metrics("linespace")), int(self.F.icon.metrics("linespace")) if self.F.has_icons else 0) \
                + 2 * px(6)
            for i, b in enumerate(self._side_btns):
                b.y0, b.y1 = y, y + bh
                y += bh + (px(4) if i < len(self._side_btns) - 1 else 0)
            self._side_lines = []
            y += px(10)
            self._side_lines.append(y)
            y += px(10)
            self._side_hits = []
            rh = int(self.F.bold.metrics("linespace")) + 2 * px(3)
            last_cat = False
            for key in self.side_rows:
                if key.startswith("cat:") and not last_cat:      # categories under their own separator
                    y += px(8) - 1
                    self._side_lines.append(y)
                    y += px(8)
                    last_cat = True
                self._side_hits.append((y, y + rh, key))
                y += rh + 2
            self._side_h = y + px(8)
            h = cv.winfo_height()
            cv.configure(scrollregion=(0, 0, w, max(self._side_h, h)))
            if self._side_h <= h:
                cv.yview_moveto(0)
            self._draw_sidebar()
        except (tk.TclError, TypeError, ValueError, AttributeError):
            pass

    def _draw_sidebar(self, hover=None):
        """Art, buttons, separators and filter rows. Only the selected filter and the pointed-at item get a fill."""
        cv, P, px = self.side_cv, self.P, self.px
        try:
            cv.delete("all")
            self._art_at(cv, self.root)
            w = cv.winfo_width()
            pad = px(10)
            for b in self._side_btns:
                b.x0, b.x1 = pad, w - pad
                b.draw(cv, hover=self._side_hover is b, down=self._side_down is b)
            for y in getattr(self, "_side_lines", ()):
                cv.create_line(pad, y, w - pad, y, fill=P["border"])
            for y0, y1, key in self._side_hits:
                _row, name, count = self.side_rows[key]
                on = key == self.filter_key
                # no boxes: the chosen filter and the pointed-at one are bold white text
                lit = on or key == self._side_hover
                mid = (y0 + y1) // 2
                cv.create_text(px(6) + px(14), mid, anchor="w", text=name.cget("text"),
                               font=self.F.bold if lit else self.F.ui, fill=P["fg"])
                cv.create_text(w - px(6) - px(14), mid, anchor="e", text=count.cget("text"),
                               font=self.F.small, fill=P["muted"])
        except (tk.TclError, TypeError, ValueError, AttributeError, KeyError):
            pass

    def _side_item_at(self, e):
        """The button (a _DrawnButton) or filter key under the pointer, or None."""
        if e is None:
            return None
        try:
            y = self.side_cv.canvasy(e.y)
        except (tk.TclError, TypeError):
            return None
        for b in self._side_btns:
            if b.y0 <= y < b.y1:
                return b
        for y0, y1, key in self._side_hits:
            if y0 <= y < y1:
                return key
        return None

    def _side_motion(self, e):
        item = self._side_item_at(e)
        if item != self._side_hover:
            self._side_hover = item
            self.side_cv.configure(cursor="hand2" if item else "")
            if self._side_down is not None and item is not self._side_down:
                self._side_down = None
            self._side_tip._hide()
            if isinstance(item, _DrawnButton):
                self._side_tip._schedule()
            self._draw_sidebar()

    def _side_press(self, e):
        self._side_tip._hide()
        item = self._side_item_at(e)
        if isinstance(item, _DrawnButton):
            self._side_down = item              # runs on release over the same button, like a real button
            self._draw_sidebar()
        elif item:
            self.set_filter(item)

    def _side_release(self, e):
        down, self._side_down = self._side_down, None
        if down is None:
            return
        self._draw_sidebar()
        if self._side_item_at(e) is down:
            down.command()

    def _bind_wheel(self, w, fn):
        w.bind("<MouseWheel>", fn)
        for c in w.winfo_children():
            self._bind_wheel(c, fn)

    def _side_row(self, key, text):
        self.side_rows[key] = (_SideText(key), _SideText(text, self._side_dirty), _SideText("0", self._side_dirty))

    def _side_dirty(self):
        """A count changed: redraw the column once, after this round of updates."""
        if not getattr(self, "_side_queued", False):
            self._side_queued = True

            def go():
                self._side_queued = False
                self._draw_sidebar()
            self.root.after_idle(go)

    def _sync_categories(self):
        """One sidebar row per category, under the status filters (rebuilt when the category list changes)."""
        names = tuple(sorted(self.cfg.get("categories", {}), key=str.lower))
        if names == self._cat_names:
            return
        self._cat_names = names
        for key in [k for k in self.side_rows if k.startswith("cat:")]:
            self.side_rows.pop(key)
        for n in names:
            self._side_row("cat:" + n, n)
        if self.filter_key.startswith("cat:") and self.filter_key not in self.side_rows:
            self.filter_key = "all"
        self._fit_column()
        self._layout_sidebar()

    def _paint_sidebar(self, hover=None):
        self._draw_sidebar()

    def set_filter(self, key):
        self.filter_key = key
        self._paint_sidebar()
        self.refresh_now()

    def _build_table(self, parent):
        body = self._frame(parent, "bg")
        body.pack(side="left", fill="both", expand=True)
        self.cols = tuple(c for c, _h, _a in COLS)
        # drawn on a canvas so the aurora stays full size behind the rows (a ttk.Treeview paints them solid)
        self.tree = arttable.ArtTable(body, columns=self.cols, show="headings", selectmode="extended", height=10,
                                      rowheight=self.px(40), head_height=self.px(36), palette=lambda: self.P,
                                      font=self.F.ui, head_font=self.F.small, draw_bg=self._draw_list_art,
                                      painters={"progress": self._paint_bar}, head_line=False)
        for c, h, anchor in COLS:   # every column gets the same width (kept equal by _equalize_columns)
            self.tree.heading(c, text=h, anchor=anchor, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=self.MIN_COL, minwidth=self.MIN_COL, stretch=False, anchor=anchor)
        self.sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self._sb_set = ui.autohide(self.sb, side="right", fill="y")
        self.tree.configure(yscrollcommand=self._yscroll)
        self.tree.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.tree.bind("<Configure>", lambda e: self._on_resize(), add="+")  # re-equalise the columns
        self.tree.bind("<Configure>", lambda e: self._later("toolbar", self._draw_toolbar), add="+")  # actions over "Name"
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Button-3>", self._popup)

        # shown while the list is empty: the aurora artwork spreads from the centre of the list and melts into the panel
        self.empty = tk.Canvas(body, bd=0, highlightthickness=0, bg=self.P["bg"])
        self._themed.append((self.empty, {"bg": "bg"}))
        self._art()
        self.empty.bind("<Configure>", lambda e: self._draw_empty())
        self.empty.bind("<Button-1>", lambda e: self.tree.selection_set(()))
        self.empty.bind("<Button-3>", lambda e: self._show_menu(e.x_root, e.y_root))
        self.tree.bind("<Configure>", lambda e: self.root.after_idle(self._layout_art), add="+")

    def _draw_empty(self):
        c, P = self.empty, self.P
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        self._art_at(c, self.root)
        if self._empty_shown:
            c.create_text(w // 2, h // 2 + self.px(60), text="No torrents yet", font=self.F.title, fill=P["fg"])
            c.create_text(w // 2, h // 2 + self.px(112), justify="center", font=self.F.big, fill=P["muted"],
                          text="Click  Add torrent  to pick a .torrent file,\nor copy a magnet link and press Ctrl+V.")

    def _art(self):
        """The aurora artwork for the current theme (list backdrop + details pane); None if it cannot be loaded.
        The light one is generated from the dark one by tools/make_light_backdrop.py."""
        dark = self.P["bg"][1:2] < "8"
        key = "_backdrop" if dark else "_backdrop_light"
        if not hasattr(self, key):
            img = None
            try:
                import assets_ui
                img = tk.PhotoImage(data=assets_ui.BACKDROP_WIDE if dark else assets_ui.BACKDROP_WIDE_LIGHT)   # 2400 x 1100
                if self.scale < 1.5:
                    img = img.subsample(2, 2)
                img = img.zoom(3, 2).subsample(2, 1)   # 1.5x wide, 2x tall: one picture behind the whole window
            except Exception:
                pass
            setattr(self, key, img)
        return getattr(self, key)

    def _build_details(self):
        det = self.det = self._frame(self.root, "bg")
        det.pack(fill="x", side="bottom")
        # The aurora runs through the whole pane: the tab row and the Overview page are canvases that each draw the
        # same artwork centred on the pane, so the pieces line up as one picture. The ttk Notebook keeps doing the
        # page switching; its own tab row is hidden (style Bare.TNotebook) and replaced by the drawn one.
        self.tabstrip = tk.Canvas(det, bd=0, highlightthickness=0, height=self.px(48), bg=self.P["bg"], cursor="hand2")
        self._themed.append((self.tabstrip, {"bg": "bg"}))
        self.tabstrip.pack(fill="x")
        self._tab_hits, self._tab_hover = [], None
        self.tabstrip.bind("<Configure>", lambda e: self._redraw_details())
        self.tabstrip.bind("<Button-1>", self._tabstrip_click)
        self.tabstrip.bind("<Motion>", self._tabstrip_hover)
        self.tabstrip.bind("<Leave>", lambda e: self._tabstrip_hover(None))
        self.tabs = ttk.Notebook(det, style="Bare.TNotebook")
        self.tabs.pack(fill="x")    # edge to edge: the artwork must not be cut by an inset
        # -- overview
        ov = ttk.Frame(self.tabs)
        self.tabs.add(ov, text="Overview")
        self.ov_canvas = tk.Canvas(ov, bd=0, highlightthickness=0, bg=self.P["bg"])
        self._themed.append((self.ov_canvas, {"bg": "bg"}))
        self.ov_canvas.pack(fill="both", expand=True)
        self.ov_canvas.bind("<Configure>", lambda e: self._redraw_details())
        det.bind("<Configure>", lambda e: self._redraw_details())
        self.d_name = tk.StringVar(value="")
        self.d_name.trace_add("write", lambda *a: self._redraw_details())
        self.d_vars = {}
        self._ov_fields = (("save", "Save to"), ("size", "Size"), ("down", "Downloaded (session)"),
                           ("up", "Uploaded (session)"), ("ratio", "Ratio"), ("peers", "Peers"), ("hash", "Info hash"))
        for key, title in self._ov_fields:
            var = tk.StringVar(value="-")
            var.trace_add("write", lambda *a: self._redraw_details())
            self.d_vars[key] = var
        # -- files / peers / trackers
        self.tab_trees = {}
        for title, cols in (
                ("Files", (("name", "Name", 380, True), ("size", "Size", 90, False), ("done", "Done", 70, False),
                           ("want", "Priority", 90, False))),
                ("Peers", (("addr", "Address", 170, False), ("country", "Country", 170, True), ("client", "Client", 160, False),
                           ("flags", "Flags", 60, False),
                           ("done", "Done", 60, False), ("down", "Down", 90, False), ("up", "Up", 90, False))),
                ("Trackers", (("url", "Tracker", 420, True), ("status", "Status", 260, False),
                              ("when", "Updated", 100, False))),
                ("Log", (("when", "Time", 90, False), ("msg", "Event", 700, True)))):
            f = ttk.Frame(self.tabs)
            self.tabs.add(f, text=title)
            tv = arttable.ArtTable(f, columns=[c[0] for c in cols], show="tree headings" if title == "Peers" else "headings",
                                   height=4, rowheight=self.px(24), head_height=self.px(28), inset=self.px(12),
                                   selectmode="extended" if title == "Files" else "browse", palette=lambda: self.P,
                                   font=self.F.ui, head_font=self.F.small, draw_bg=self._draw_art)
            if title == "Peers":    # the first (tree) column carries the national flag
                tv.heading("#0", text="", anchor="w")
                tv.column("#0", width=self.px(44), minwidth=self.px(44), stretch=False, anchor="w")
            for cid, head, width, stretch in cols:
                tv.heading(cid, text=head, anchor="w")
                tv.column(cid, width=self.px(width), minwidth=self.px(40), anchor="w", stretch=stretch)
            sb = ttk.Scrollbar(f, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=ui.autohide(sb, side="right", fill="y"))
            tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.tab_trees[title] = tv
            if title == "Files":
                tv.bind("<Button-3>", self._files_popup)
                tv.bind("<Double-1>", lambda e: self.play_file_row() if tv.identify_row(e.y) else None)
            elif title == "Peers":
                tv.bind("<Button-3>", self._peers_popup)
        self.files_menu = tk.Menu(self.root, tearoff=0, bd=0, relief="flat")
        self.files_menu.add_command(label="Play", command=self.play_file_row)
        self.files_menu.add_command(label="Mark as watched", command=lambda: self.mark_watched(True))
        self.files_menu.add_command(label="Mark as not watched", command=lambda: self.mark_watched(False))
        self.files_menu.add_command(label="Cast to TV\u2026", command=self.cast_dialog)
        self.files_menu.add_command(label="Copy link for TV / phone\u2026", command=self.copy_lan_link)
        self.files_menu.add_separator()
        for label, level in (("High priority", "high"), ("Normal priority", "normal"), ("Don't download", "skip")):
            self.files_menu.add_command(label=label, command=lambda lv=level: self.set_file_prio(lv))
        self.peers_menu = tk.Menu(self.root, tearoff=0, bd=0, relief="flat")
        self.peers_menu.add_command(label="Copy address", command=self.copy_peer)
        self.peers_menu.add_command(label="Ban this peer", command=self.ban_selected_peer)
        self.peers_menu.add_separator()
        self.peers_menu.add_command(label="Banned peers\u2026", command=lambda: BannedDialog(self).show())
        # -- speed graph
        gf = ttk.Frame(self.tabs)
        self.tabs.add(gf, text="Speed")
        self.speed_canvas = tk.Canvas(gf, height=self.px(100), highlightthickness=0, bd=0)
        self._themed.append((self.speed_canvas, {"bg": "bg"}))
        self.speed_canvas.pack(fill="both", expand=True)
        self.speed_canvas.bind("<Configure>", lambda e: self._update_tabs() if self._tab == "Speed" else None)
        self.player_tab = tk.Frame(self.tabs, bg="black")
        self.tabs.add(self.player_tab, text="Player")
        self._player_idle("Press Play on a torrent to watch it here.\nUse Detach to move the video into its own window.")
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._on_tab())
        self.root.bind("<Configure>", self._size_later, add="+")

    def _on_tab(self):
        try:
            self._tab = self.tabs.tab(self.tabs.select(), "text")
        except tk.TclError:
            return
        self._redraw_details()
        self._update_tabs()

    # -- details pane artwork: drawn tab row + Overview page ------------------------------------------------
    def _redraw_details(self):
        """Coalesce redraws (every detail field change and resize asks for one)."""
        if not getattr(self, "_det_job", None):
            self._det_job = self.root.after_idle(self._draw_details)

    def _draw_details(self):
        self._det_job = None
        try:
            self._draw_tabstrip()
            self._draw_overview()
            self._draw_player_idle()
        except tk.TclError:
            pass

    def _player_idle(self, text):
        """Player tab with nothing playing: the aurora with a short hint instead of a black box."""
        for w in self.player_tab.winfo_children():
            w.destroy()
        c = self._player_hint = tk.Canvas(self.player_tab, bd=0, highlightthickness=0, bg=self.P["bg"])
        c.hint = text
        c.place(x=0, y=0, relwidth=1, relheight=1)
        c.bind("<Configure>", lambda e: self._redraw_details())
        self._redraw_details()

    def _draw_player_idle(self):
        c = getattr(self, "_player_hint", None)
        if c is None or not c.winfo_exists():
            return
        c.delete("all")
        c.configure(bg=self.P["bg"])
        self._draw_art(c)
        c.create_text(c.winfo_width() // 2, c.winfo_height() // 2, text=c.hint, justify="center",
                      font=self.F.ui, fill=self.P["muted"])

    def _draw_art(self, c):
        """Draw the window's artwork on canvas `c`: every canvas shows its own slice of the one picture."""
        self._art_at(c, self.root)

    def _art_at(self, c, ref, tags=()):
        """Draw the artwork on canvas `c` centred on widget `ref` (scrolled canvases included)."""
        art = self._art()
        if art is None:
            return
        try:
            ox, oy = int(c.canvasx(0)), int(c.canvasy(0))
        except (tk.TclError, TypeError, ValueError, AttributeError):
            ox = oy = 0
        x = ref.winfo_rootx() + ref.winfo_width() // 2 - c.winfo_rootx() + ox
        y = ref.winfo_rooty() + ref.winfo_height() // 2 - c.winfo_rooty() + oy
        c.create_image(x, y, image=art, tags=tags)

    def _draw_tabstrip(self):
        c, P, px = self.tabstrip, self.P, self.px
        c.delete("all")
        self._draw_art(c)
        try:
            cur = self.tabs.index(self.tabs.select())
        except tk.TclError:
            cur = -1
        top, h, pad = px(8), c.winfo_height(), px(18)
        x, mid = px(12), (top + h) // 2      # first label lines up with the page text
        self._tab_hits = []
        for i, tid in enumerate(self.tabs.tabs()):
            text = self.tabs.tab(tid, "text")
            w = self.F.ui.measure(text) + 2 * pad
            on = i == cur
            lit = on or i == self._tab_hover                # no underline bar: bold white instead
            c.create_text(x + w // 2, mid, text=text, font=self.F.bold if lit else self.F.ui,
                          fill=P["fg"] if lit else P["muted"])
            self._tab_hits.append((x, x + w, i))
            x += w

    def _tab_at(self, e):
        if e is not None:
            for x0, x1, i in self._tab_hits:
                if x0 <= e.x < x1:
                    return i
        return None

    def _tabstrip_click(self, e):
        i = self._tab_at(e)
        if i is not None:
            self.tabs.select(i)        # fires <<NotebookTabChanged>> -> _on_tab -> redraw

    def _tabstrip_hover(self, e):
        i = self._tab_at(e)
        if i != self._tab_hover:
            self._tab_hover = i
            self._redraw_details()

    def _draw_overview(self):
        c, P, F, px = self.ov_canvas, self.P, self.F, self.px
        c.delete("all")
        self._draw_art(c)
        m, w = px(24), c.winfo_width()
        y = px(10)
        c.create_text(m, y, anchor="nw", text=self._elide(self.d_name.get(), F.bold, w - 2 * m), font=F.bold, fill=P["fg"])
        y += F.bold.metrics("linespace") + px(8)
        y2 = y + F.small.metrics("linespace") + px(2)
        # "Save to" on the left, the other fields as a group on the right (each as wide as its title or value)
        x = w - m
        for key, title in reversed(self._ov_fields[1:]):
            val = self.d_vars[key].get()
            x -= max(F.small.measure(title), F.ui.measure(val))
            c.create_text(x, y, anchor="nw", text=title, font=F.small, fill=P["muted"])
            c.create_text(x, y2, anchor="nw", text=val, font=F.ui, fill=P["fg"])
            x -= px(28)
        key, title = self._ov_fields[0]
        c.create_text(m, y, anchor="nw", text=title, font=F.small, fill=P["muted"])
        c.create_text(m, y2, anchor="nw", text=self._elide(self.d_vars[key].get(), F.ui, x - m), font=F.ui, fill=P["fg"])

    @staticmethod
    def _elide(text, font, room):
        if room <= 0 or font.measure(text) <= room:
            return text
        while text and font.measure(text + "\u2026") > room:
            text = text[:-1]
        return text + "\u2026"

    def _size_tabs(self):
        """Give the details pane only what is left once the whole sidebar fits (never grows for the Player tab)."""
        try:
            self.root.update_idletasks()
            self._layout_sidebar()
            body_need = self._side_h + self.px(6)
            # what the list area has now + what the pane has now = the room to share; the sidebar gets first call
            strip = self.tabstrip.winfo_height()            # the drawn tab row sits above the Notebook
            room = self.tree.master.winfo_height() + self.tabs.winfo_height() + strip
            total = max(self.px(190), min(self.px(340), room - body_need)) - strip   # tab row included
            if abs(total - self.tabs.winfo_height()) > self.px(4):
                conf = int(float(str(self.tabs.cget("height")) or 0))
                edge = self.tabs.winfo_height() - conf if conf else 0   # Notebook borders around the page
                self.tabs.configure(height=max(self.px(100), total - max(edge, 0)))
        except (tk.TclError, AttributeError):
            pass

    def _size_later(self, e=None):
        if e is not None and e.widget is not self.root:
            return
        if getattr(self, "_size_job", None):
            self.root.after_cancel(self._size_job)
        self._size_job = self.root.after(120, self._size_tabs)

    def _sync_rows(self, tv, rows):
        """Bring a tab's table in line with `rows` [(iid, values)] changing only what differs."""
        cache = self._tab_cache.setdefault(tv, {})
        keep = {row[0] for row in rows}
        for iid in [i for i in cache if i not in keep]:
            tv.delete(iid)
            del cache[iid]
        for row in rows:
            iid, vals = row[0], row[1]
            img = row[2] if len(row) > 2 and row[2] is not None else ""
            state = (vals, str(img))
            if iid not in cache:
                tv.insert("", "end", iid=iid, values=vals, image=img)
            elif cache[iid] != state:
                tv.item(iid, values=vals, image=img)
            cache[iid] = state

    def _update_tabs(self):
        sel = self._selected()
        tab = self._tab
        if tab == "Speed":
            key = sel[0] if len(sel) == 1 else None
            title = self._snap_by_hash[key]["name"] if key else "All torrents"
            self.graph.draw(self.speed_canvas, key, title, self.P, self.px, bg=self._draw_art)
            return
        if tab not in self.tab_trees:
            return
        tv = self.tab_trees[tab]
        if tab == "Log":
            self._sync_rows(tv, [("l%d" % i, line) for i, line in reversed(list(enumerate(self.log_lines)))])
            return
        d = self.mgr.details(sel[0]) if len(sel) == 1 else None
        if d is None:
            self._sync_rows(tv, [])
            return
        now = time.time()
        if tab == "Files":
            st = self.media_state_cached()
            rows = [("f%d" % i, (("\u2713 " if (st.get("%s:%d" % (sel[0], i)) or {}).get("watched") else "") + name,
                                 fmt_size(size), "%d%%" % int(prog * 100),
                                 "Skipped" if skipped else "High" if high else "Normal"))
                    for i, (name, size, prog, skipped, high) in enumerate(d["files"][:2000])]
        elif tab == "Peers":
            rows, prev, seen = [], self._peer_prev, {}
            for p in d["peers"]:
                k = p["key"]
                rx0, tx0, t0 = prev.get(k, (p["rx"], p["tx"], now))
                dt = max(now - t0, 0.5)
                seen[k] = (p["rx"], p["tx"], now) if now - t0 >= 0.9 else prev.get(k, (p["rx"], p["tx"], now))
                code = self._geo(p["addr"])
                rows.append((str(k), (p["addr"], geo.name(code) if code else "Local / unknown", p["client"], p["flags"],
                                      "%d%%" % int(p["progress"] * 100),
                                      fmt_rate((p["rx"] - rx0) / dt), fmt_rate((p["tx"] - tx0) / dt)),
                             geo.flag_image(code, self.scale >= 1.5) if code else None))
            self._peer_prev = seen
        else:
            rows = [("t%d" % i, (url, text, "%d s ago" % max(int(now - when), 0) if now - when < 120
                                  else "%d min ago" % ((now - when) // 60)))
                    for i, (url, text, when) in enumerate(d["trackers"])]
        self._sync_rows(tv, rows)

    # -- playing while downloading ---------------------------------------
    def _stream_server(self):
        if getattr(self, "_streamer", None) is None:
            import stream
            self._streamer = stream.StreamServer(self.mgr)
            self._streamer.start()
        return self._streamer

    def play_file_row(self):
        sel = self._selected()
        idx = [int(i[1:]) for i in self.tab_trees["Files"].selection()]
        if len(sel) == 1 and idx:
            self.play(sel[0], idx[0])

    def play_selected(self):
        sel = self._selected()
        if len(sel) == 1:
            self.play(sel[0], None)

    def _media_opts(self):
        c = self.cfg
        return dict(hwdec=c.get("hwdec", True), alang=c.get("alang", ""), slang=c.get("slang", ""), os_key=c.get("os_key", ""),
                    os_lang=c.get("os_lang", "en"), os_user=c.get("os_user", ""), os_pass=c.get("os_pass", ""),
                    thumbs=c.get("thumbs", True))

    def _build_queue(self, h, sess, file_idx):
        """Play queue (this file, then the following episodes) with stream addresses and subtitle files."""
        import stream
        srv = self._stream_server()
        files = [(f[0][1:] if sess.t.multi else f[0], f[1]) for f in sess.t.files]
        queue = []
        for i in stream.playlist_for(files, file_idx)[:60]:
            name = files[i][0][-1]
            subs = []
            for si in stream.subtitles_for(files, i)[:6]:
                try:
                    sname, _ = self.mgr.stream_prepare(h, si)
                    subs.append(srv.url(h, si, sname))
                except ValueError:
                    pass
            queue.append(dict(url=srv.url(h, i, name), title=name, key="%s:%d" % (h, i), map=srv.map_url(h, i),
                              hint=srv.hint_url(h, i), prefetch=srv.prefetch_url(h, i), subs=subs))
        return queue

    def play(self, h, file_idx=None):
        """Start the player on a file of torrent h; it is streamed from the download (largest video by default)."""
        import stream, mpvlib, player as player_mod
        sess = self.mgr.sessions.get(h)
        if not hasattr(sess, "t"):
            messagebox.showinfo(APP, "The file list of this torrent is not known yet. Try again in a moment.", parent=self.root)
            return
        files = sess.t.files
        if file_idx is None:
            media = [(f[1], i) for i, f in enumerate(files) if stream.media_kind(f[0][-1]) == "video" and not stream.is_sample(f[0])] or \
                    [(f[1], i) for i, f in enumerate(files) if stream.media_kind(f[0][-1]) == "audio"]
            if not media:
                messagebox.showinfo(APP, "This torrent has no video or audio files to play.", parent=self.root)
                return
            file_idx = max(media)[1]
        if self.cfg.get("skip_extras", True) and not sess.complete:       # samples, .nfo, .jpg ... are not needed to watch
            prog = sess.file_progress()
            skip = [i for i in stream.suggest_skips([(f[0], f[1], prog[i]) for i, f in enumerate(files)], keep=(file_idx,))
                    if i not in sess.skip]
            if skip:
                self.mgr.set_file_prio(h, skip, "skip")
                self.log("%s: not downloading %d sample/extra file(s) while you watch" % (self._snap_by_hash.get(h, {}).get("name", ""), len(skip)))
        try:
            name, size = self.mgr.stream_prepare(h, file_idx)
            queue = self._build_queue(h, sess, file_idx)
        except (ValueError, OSError) as e:
            messagebox.showerror(APP, "Cannot play this file: %s" % e, parent=self.root)
            return
        lib = mpvlib.find_library(self.cfg.get("libmpv", ""))
        if self.cfg.get("player", "builtin") == "builtin" and not lib and not self._offered_lib:
            self._offered_lib = True
            ans = messagebox.askyesnocancel(
                APP, "The built-in player needs libmpv (about 30 MB, downloaded from GitHub).\n\n"
                     "Yes: download it now\nNo: use another player instead (VLC, mpv, MPC-HC...)", parent=self.root)
            if ans is None:
                return
            if ans:
                self.get_libmpv(done=lambda path: self.play(h, file_idx) if path else None)
                return
        use_builtin = self.cfg.get("player", "builtin") == "builtin" and lib
        try:
            if use_builtin and self.cfg.get("player_where") == "tab":
                self._play_in_tab(queue, lib)
                self.log("Playing %s in the Player tab" % name)
            elif use_builtin:
                self._launch_player(queue, lib)
                self.log("Playing %s with the built-in player" % name)
            else:
                try:
                    if self.cfg.get("player_where") == "tab" and embed.available():
                        host = self._embed_host()
                        try:
                            proc, how = player_mod.external_launch(queue[0]["url"], self.cfg.get("player_exe"),
                                                                   wid=host.winfo_id(), tab=True)
                        except Exception:
                            self._ext_ended()
                            raise
                        self._embed_external(proc, how, host)
                    else:
                        proc, how = player_mod.external_launch(queue[0]["url"], self.cfg.get("player_exe"))
                except player_mod.NoPlayer:
                    ans = messagebox.askyesnocancel(
                        APP, "No video player that can play this was found.\n\n"
                             "Yes: install VLC now (free; Windows will ask permission, then press Play again)\n"
                             "No: try the default Windows media app (may not handle 4K / HEVC)\n\n"
                             "Or use Settings > Media > Get libmpv for the built-in player.",
                        parent=self.root)
                    if ans is None:
                        return
                    if ans:
                        try:
                            player_mod.install_vlc()
                            self.log("Installing VLC with winget")
                        except Exception as e:
                            messagebox.showerror(APP, "Could not start the VLC install: %s\nGet it from videolan.org." % e, parent=self.root)
                        return
                    how = player_mod.default_player(queue[0]["url"])
                self.log("Playing %s with %s" % (name, how))
        except Exception as e:
            messagebox.showerror(APP, "Could not start the player: %s" % e, parent=self.root)
            return
        self.refresh_now()

    def _embed_host(self):
        """Empty the Player tab and return the black frame an external player will draw into."""
        old = getattr(self, "_ext", None)
        if old:
            old.stop()
            self._ext = None
        panel = getattr(self, "_panel", None)
        if panel:
            panel._save_pos(); panel.close(); self._panel = None
        for w in self.player_tab.winfo_children():
            w.destroy()
        self._ext_bar = bar = tk.Frame(self.player_tab, bg=self.P["surface"])
        bar.pack(fill="x", side="bottom")
        host = tk.Frame(self.player_tab, bg="black", highlightthickness=0)
        host.pack(fill="both", expand=True)
        self.tabs.select(self.player_tab)
        self.root.update_idletasks()
        return host

    def _embed_external(self, proc, name, host):
        """Show an external player (VLC, MPC-HC ...) in the Player tab; Detach gives it back its own window."""
        bar, kind = self._ext_bar, embed.player_kind(name)
        tk.Label(bar, text="%s (in the Player tab)" % name, bg=self.P["surface"], fg=self.P["muted"]).pack(side="left", padx=10, pady=4)
        state = {"min": True}

        def minimal():                                  # MPC-HC: Ctrl+1 = no toolbar/seek bar/status, just the picture
            if kind == "mpc" and self._ext:
                self._ext.keys(embed.VK_CONTROL, embed.VK_1)

        def toggle_controls():
            if kind == "mpc" and self._ext:
                state["min"] = not state["min"]
                self._ext.keys(embed.VK_CONTROL, embed.VK_1 if state["min"] else embed.VK_3)

        def full():                                     # its own window, then its full-screen key (Alt+Enter)
            ext = self._ext
            if not ext:
                return
            self._ext_ended(detach=True)
            chord = (embed.VK_MENU, embed.VK_RETURN) if kind == "mpc" else (0x46,) if kind == "vlc" else None   # Alt+Enter / F
            if chord and ext.hwnd:
                self.root.after(500, lambda: ext.win.keys(ext.hwnd, chord))

        ext = embed.Embedded(self.root, host, proc, on_end=lambda: self._ext_ended(), on_fail=lambda why: self._ext_failed(why),
                             attach=(kind != "mpv"), on_attached=minimal)
        self._ext = ext
        for text, fn in (("Stop", lambda: self._ext_ended(stop=True)), ("Detach", lambda: self._ext_ended(detach=True)),
                         ("Full screen", full)) + ((("Controls", toggle_controls),) if kind == "mpc" else ()):
            ttk.Button(bar, text=text, command=fn).pack(side="right", padx=(0, 6), pady=3)
        self.tabs.select(self.player_tab)

    def _ext_ended(self, stop=False, detach=False):
        ext = getattr(self, "_ext", None)
        if ext:
            (ext.stop if stop else ext.release if detach else ext._stop_watching)()
        self._ext = None
        self._player_idle("Press Play on a torrent to watch it here.")

    def _ext_failed(self, why):
        self.log("Could not show the player in the tab: %s It stays in its own window." % why)
        self._ext_ended()

    def _session(self, queue, lib):
        return dict(queue=queue, index=0, state_dir=self.dir, lib=lib or "", opts=self._media_opts())

    def _launch_player(self, queue, lib):
        import json as _json
        fd, path = tempfile.mkstemp(prefix="btc-play-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(self._session(queue, lib), f)
        cmd = ([sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(sys.argv[0])]) + \
              ["--player", "--session", path]
        subprocess.Popen(cmd, close_fds=True)

    def _play_in_tab(self, queue, lib):
        import player as player_mod
        panel = getattr(self, "_panel", None)
        if panel and panel.mpv:
            panel.queue = list(queue)
            panel.play_index(0)
        else:
            for w in self.player_tab.winfo_children():
                w.destroy()
            panel = player_mod.PlayerPanel(self.player_tab, self.root, queue, 0, self._session(queue, lib), lib,
                                           on_fullscreen=lambda on=None: self.popout_player(full=True) if on is not False else None,
                                           on_popout=self.popout_player, key_widget="all", popout_label="Detach",
                                           key_scope=lambda: self._tab == "Player" and getattr(self, "_panel", None) is not None,
                                           on_mini=lambda: self.popout_player(mini=True))
            if panel.error:
                messagebox.showerror(APP, panel.error, parent=self.root)
                return
            self._panel = panel
        self.tabs.select(self.player_tab)
        self._tab = "Player"
        self.root.update_idletasks()

    def popout_player(self, mini=False, full=False):
        """Move the Player tab's video into a window of its own (continues where it was); mini: the small
        always-on-top player in the corner."""
        import player as player_mod
        panel = getattr(self, "_panel", None)
        if not panel or not panel.mpv:
            return
        queue, idx, sess, lib = panel.queue, panel.index, panel.sess, panel.mpv.path
        panel._save_pos()
        panel.close()
        self._panel = None
        self._player_idle("The player is in its own window. Press Attach there to bring it back here.")
        player_mod.PlayerWindow(self.root, queue=queue, index=idx, session=sess, lib=lib, on_dock=self.dock_player,
                                mini=mini, full=full)

    def dock_player(self, win):
        """Bring a detached player window back into the Player tab, continuing where it was."""
        panel = win.panel
        queue, idx, sess, lib = panel.queue, panel.index, panel.sess, panel.mpv.path if panel.mpv else ""
        panel._save_pos()
        win.close()
        if lib:
            self._play_in_tab(queue[idx:] + queue[:0], lib)

    def get_libmpv(self, parent=None, done=None):
        """Download libmpv with a small progress window; done(path or None) is called on the Tk thread."""
        import libmpv_get
        parent = parent or self.root
        win = tk.Toplevel(parent)
        win.title("Downloading libmpv")
        win.transient(parent)
        win.resizable(False, False)
        win.configure(bg=self.P["bg"])
        if chrome.enabled(self.cfg):
            chrome.attach(win, self.P, self.px, "Downloading libmpv", resizable=False, font=self.F.bold)
            ui.center_over(win, parent)
        msg = tk.StringVar(value="Looking for the newest libmpv build\u2026")
        ttk.Label(win, textvariable=msg, padding=(18, 16, 18, 6), width=52).pack()
        bar = ttk.Progressbar(win, length=self.px(380), mode="determinate")
        bar.pack(padx=18, pady=(0, 16))
        box = queue.Queue()

        def work():
            def prog(done_b, total):
                box.put(("p", done_b, total))
            try:
                box.put(("ok", libmpv_get.install(progress=prog)))
            except Exception as e:
                box.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()

        def poll():
            last = None
            try:
                while True:
                    last = box.get_nowait()
                    if last[0] == "p":
                        bar["maximum"], bar["value"] = max(last[2], 1), last[1]
                        msg.set("Downloading libmpv\u2026 %.1f of %.1f MB" % (last[1] / 1e6, last[2] / 1e6))
                    else:
                        break
            except queue.Empty:
                pass
            if last and last[0] == "ok":
                win.destroy()
                self.cfg["libmpv"] = last[1]
                self.log("libmpv installed: %s" % last[1])
                if done:
                    done(last[1])
            elif last and last[0] == "err":
                win.destroy()
                messagebox.showerror(APP, "Could not download libmpv:\n%s\n\nYou can download it yourself (see the README) "
                                          "and choose the file in Settings > Media." % last[1], parent=parent)
                if done:
                    done(None)
            else:
                win.after(150, poll)
        win.after(150, poll)

    def _media_key(self):
        sel = self._selected()
        idx = [int(i[1:]) for i in self.tab_trees["Files"].selection()]
        return (sel[0], idx) if len(sel) == 1 and idx else (None, [])

    def mark_watched(self, on):
        h, idx = self._media_key()
        if h:
            for i in idx:
                media_state.set_watched(self.dir, "%s:%d" % (h, i), on)
            self._media_cache = (0, {})
            self.refresh_now()

    def _pick_video(self):
        """(hash, file index) of the selected file row, else the selected torrent's largest video; None if nothing."""
        import stream
        h, idx = self._media_key()
        if h:
            return h, idx[0]
        sel = self._selected()
        if len(sel) != 1:
            return None
        sess = self.mgr.sessions.get(sel[0])
        vids = [(f[1], i) for i, f in enumerate(sess.t.files) if stream.media_kind(f[0][-1])] if hasattr(sess, "t") else []
        if not vids:
            messagebox.showinfo(APP, "This torrent has no video or audio files.", parent=self.root)
            return None
        return sel[0], max(vids)[1]

    def _lan_link(self, h, idx):
        """Address other devices on the network can open for one file (asks once to allow sharing). None if declined."""
        import socket
        if not self.cfg.get("lan_share"):
            if not messagebox.askyesno(APP, "Allow other devices on your network to play streams from this program?\n\n"
                                       "Anyone on your network who has the link can watch that file until the link expires "
                                       "(%d hours) or you choose Stop sharing. Windows may ask you to allow network access. "
                                       "You can turn this off in Settings > Media." % self.cfg.get("lan_hours", 8), parent=self.root):
                return None
            self.cfg["lan_share"] = True
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("10.255.255.255", 1))                      # no traffic is sent: this only picks the network address
            ip = sk.getsockname()[0]
            sk.close()
            name, _size = self.mgr.stream_prepare(h, idx)
            srv = self._stream_server()
            if not srv.lan_active():
                srv.stop_lan()
                srv.start_lan(ip, self.cfg.get("lan_hours", 8))
            return srv.lan_url(h, idx, name), name
        except (OSError, ValueError) as e:
            messagebox.showerror(APP, "Could not share the stream: %s" % e, parent=self.root)
            return None

    def copy_lan_link(self):
        """Stream address other devices on the network (TV, phone) can open."""
        pick = self._pick_video()
        got = self._lan_link(*pick) if pick else None
        if not got:
            return
        link = got[0]
        self.root.clipboard_clear(); self.root.clipboard_append(link)
        messagebox.showinfo(APP, "Link copied. Open it in VLC (Open network stream), Kodi, or a TV/phone player on the same network. "
                            "It stops working after %d hours or when you choose Stop sharing:\n\n%s" % (self.cfg.get("lan_hours", 8), link),
                            parent=self.root)

    def cast_dialog(self):
        """Pick a TV / Chromecast on the network and play the selected video on it."""
        pick = self._pick_video()
        if not pick:
            return
        from dialogs import CastDialog
        CastDialog(self, *pick).show()

    def stop_sharing(self):
        srv = getattr(self, "_streamer", None)
        if srv:
            srv.stop_lan()
        self.log("Stopped sharing streams on the network")
        messagebox.showinfo(APP, "Sharing stopped. Links you gave out no longer work.", parent=self.root)

    def media_state_cached(self):
        t, d = self._media_cache
        if time.time() - t > 4:
            d = media_state.load(self.dir)
            self._media_cache = (time.time(), d)
        return d

    def library_dialog(self):
        from dialogs import LibraryDialog
        LibraryDialog(self).show()

    def _files_popup(self, e):
        tv = self.tab_trees["Files"]
        iid = tv.identify_row(e.y)
        if iid and iid not in tv.selection():
            tv.selection_set(iid)
        if tv.selection():
            self.files_menu.tk_popup(e.x_root, e.y_root)

    def set_file_prio(self, level):
        sel = self._selected()
        tv = self.tab_trees["Files"]
        idx = [int(i[1:]) for i in tv.selection()]
        if len(sel) != 1 or not idx:
            return
        try:
            self.mgr.set_file_prio(sel[0], idx, level)
        except ValueError as e:
            messagebox.showerror(APP, str(e), parent=self.root)
            return
        self.log("%s: %d file(s) set to %s" % (self._snap_by_hash[sel[0]]["name"], len(idx),
                                              {"skip": "not downloaded", "high": "high priority", "normal": "normal priority"}[level]))
        self.save()
        self.refresh_now()

    def _peers_popup(self, e):
        tv = self.tab_trees["Peers"]
        iid = tv.identify_row(e.y)
        if iid:
            tv.selection_set(iid)
        state = "normal" if tv.selection() else "disabled"
        self.peers_menu.entryconfigure(0, state=state)
        self.peers_menu.entryconfigure(1, state=state)
        self.peers_menu.tk_popup(e.x_root, e.y_root)

    def _selected_peer_addr(self):
        tv = self.tab_trees["Peers"]
        sel = tv.selection()
        return tv.item(sel[0], "values")[0] if sel else None

    def copy_peer(self):
        addr = self._selected_peer_addr()
        if addr:
            self.root.clipboard_clear()
            self.root.clipboard_append(addr)

    def ban_selected_peer(self):
        addr = self._selected_peer_addr()
        if not addr:
            return
        ip = addr.rsplit(":", 1)[0]
        if messagebox.askyesno(APP, "Ban %s?\n\nThis address is disconnected now and refused from then on. You can "
                                    "undo it in Settings > Connection > Banned peers." % ip, parent=self.root):
            self.ban_peer(addr)

    def _geo(self, addr):
        """Country code of a peer address 'ip:port' (cached)."""
        cache = self.__dict__.setdefault("_geo_cache", {})
        ip = addr.rsplit(":", 1)[0]
        if ip not in cache:
            if len(cache) > 5000:
                cache.clear()
            try:
                cache[ip] = geo.country(ip)
            except Exception:
                cache[ip] = None
        return cache[ip]

    def _reset_tabs(self):
        """A different torrent is selected: empty the file/peer/tracker tables."""
        self._peer_prev = {}
        for tv in self.tab_trees.values():
            self._sync_rows(tv, [])

    def _build_statusbar(self):
        """The status bar is drawn on the window's artwork too. Alt speed and the port-forwarding text are links."""
        cv = self.statusbar = tk.Canvas(self.root, bd=0, highlightthickness=0,
                                        height=int(self.F.ui.metrics("linespace")) + 16)
        self._themed.append((cv, {"bg": "bg"}))
        cv.pack(fill="x", side="bottom")
        redraw = lambda: self._later("status", self._draw_status)
        self.s_speed = _DrawnText(redraw, "fg")
        self.s_count = _DrawnText(redraw, "muted")
        self.s_net = _DrawnText(redraw, "muted")
        self.s_alt = _DrawnText(redraw, "muted", "Alt speed: off", command=self.toggle_alt,
                                tip="Click to switch the alternative speed limits (Settings > Speed) on or off")
        self.s_pf = _DrawnText(redraw, "fg", command=self.diagnostics_dialog, tip=lambda: self._pf_full)
        self.s_dot = _DrawnText(redraw, "muted", "●", command=self.diagnostics_dialog, tip=lambda: self._pf_full)
        self._st_items = (self.s_speed, self.s_count, self.s_net, self.s_alt, self.s_pf, self.s_dot)
        self._st_bar = _DrawnBar(self, cv, lambda: self._st_items, self._draw_status, above=True)
        cv.bind("<Configure>", lambda e: self._draw_status(), add="+")

    def _draw_status(self):
        cv, P, F = self.statusbar, self.P, self.F
        try:
            cv.delete("all")
            self._art_at(cv, self.root)
            w, h = cv.winfo_width(), int(cv.cget("height"))
            mid = h // 2

            def put(t, x, anchor):
                text = t.cget("text") or ""
                tw = int(F.ui.measure(text))
                x0 = x if anchor == "w" else x - tw
                t.x0, t.y0, t.x1, t.y1 = x0, 0, x0 + tw, h
                hot = self._st_bar.hover is t and t is not self.s_dot
                cv.create_text(x, mid, anchor=anchor, text=text, font=F.ui, fill=P["fg"] if hot else t.colour(P))
                return tw
            x = 18
            x += put(self.s_speed, x, "w") + 26
            put(self.s_count, x, "w")
            x = w - 18
            x -= put(self.s_net, x, "e") + 22
            x -= put(self.s_alt, x, "e") + 22
            x -= put(self.s_pf, x, "e") + 6
            put(self.s_dot, x, "e")
        except (tk.TclError, TypeError, ValueError, AttributeError):
            pass

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, bd=0, relief="flat")
        for text, cmd in (("Play", self.play_selected), ("Resume", self.resume), ("Pause", self.pause), ("Files\u2026", self.files_dialog),
                          ("Open folder", self.open_folder), ("Verify data", self.recheck)):
            self.menu.add_command(label=text, command=cmd)
        self.queue_menu = tk.Menu(self.menu, tearoff=0, bd=0, relief="flat")
        for text, where in (("Move to top", "top"), ("Move up", "up"), ("Move down", "down"), ("Move to bottom", "bottom")):
            self.queue_menu.add_command(label=text, command=lambda w=where: self.move_in_queue(w))
        self.menu.add_cascade(label="Queue", menu=self.queue_menu)
        self.prio_menu = tk.Menu(self.menu, tearoff=0, bd=0, relief="flat")
        self.cat_menu = tk.Menu(self.menu, tearoff=0, bd=0, relief="flat")
        self.menu.add_cascade(label="Priority", menu=self.prio_menu)
        self.menu.add_cascade(label="Category", menu=self.cat_menu)
        self.menu.add_command(label="Cast to TV\u2026", command=self.cast_dialog)
        self.menu.add_command(label="Copy link for TV / phone\u2026", command=self.copy_lan_link)
        self.menu.add_command(label="Stop sharing on the network", command=self.stop_sharing)
        self.menu.add_command(label="Speed limit\u2026", command=self.limit_dialog)
        self.seq_var = tk.BooleanVar(value=False)
        im = ui.check_images()           # the round tick box here too (green when on)
        extra = dict(image=im["off_tight"], selectimage=im["on_tight"], compound="left", indicatoron=False) \
            if im.get("off_tight") is not None else {}
        self.menu.add_checkbutton(label="  Download in order (for streaming)" if extra else
                                  "Download in order (for streaming)", variable=self.seq_var,
                                  command=self.toggle_sequential, **extra)
        self.menu.add_separator()
        self.menu.add_command(label="Remove", command=self.remove)

    def _rebuild_menus(self):
        """The Priority and Category sub-menus show what the selected torrents currently use."""
        snaps = [self._snap_by_hash[h] for h in self._selected() if h in self._snap_by_hash]
        prios = {s.get("priority", 0) for s in snaps}
        cats = {s.get("category", "") for s in snaps}
        self.seq_var.set(bool(snaps) and all(s.get("sequential") for s in snaps))
        for m in (self.prio_menu, self.cat_menu):
            m.delete(0, "end")
        for label, val in (("High", 1), ("Normal", 0), ("Low", -1)):
            mark = "\u2713  " if prios == {val} else "     "
            self.prio_menu.add_command(label=mark + label, command=lambda v=val: self.set_priority(v))
        names = sorted(self.cfg.get("categories", {}), key=str.lower)
        self.cat_menu.add_command(label=("\u2713  " if cats == {""} else "     ") + "(none)", command=lambda: self.set_category(""))
        for n in names:
            self.cat_menu.add_command(label=("\u2713  " if cats == {n} else "     ") + n, command=lambda n=n: self.set_category(n))
        if not names:
            self.cat_menu.add_command(label="Create categories in Settings > Downloads", state="disabled")

    def _show_menu(self, x, y):
        self._rebuild_menus()
        self.menu.tk_popup(x, y)

    def toggle_sequential(self):
        sel = self._selected()
        if sel:
            self.mgr.set_sequential(sel, self.seq_var.get())
            self.save()
            self.log("Download in order %s for %d torrent(s)" % ("on" if self.seq_var.get() else "off", len(sel)))

    def set_priority(self, val):
        sel = self._selected()
        if sel:
            self.mgr.set_priority(sel, val)
            self.save()
            self.refresh_now()

    def set_category(self, name):
        sel = self._selected()
        if sel:
            self.mgr.set_category(sel, name)
            self.save()
            self.refresh_now()

    def limit_dialog(self):
        sel = [h for h in self._selected() if isinstance(self.mgr.sessions.get(h), Session)]
        if not sel:
            return
        snap = self._snap_by_hash[sel[0]]
        res = TorrentLimitDialog(self, snap["name"], snap.get("limit_down", 0), snap.get("limit_up", 0), len(sel)).show()
        if res:
            for h in sel:
                self.mgr.set_torrent_limits(h, *res)
            self.save()

    # -- theme ------------------------------------------------------------
    def apply_theme(self):
        P = self.P
        ui.style_ttk(self.root, P, self.F, self.px)
        self.root.configure(bg=P["bg"])
        alive = []
        for w, opts in self._themed:
            try:
                w.configure(**{o: P[k] for o, k in opts.items()})
            except tk.TclError:       # destroyed since (e.g. a category row that was removed)
                continue
            alive.append((w, opts))
        self._themed = alive
        for b in self.btn.values():
            b.restyle(P)
        for m in (self.menu, self.queue_menu, self.prio_menu, self.cat_menu, self.files_menu, self.peers_menu):
            m.configure(bg=P["surface"], fg=P["muted"], activebackground=P["surface"], activeforeground=P["fg"],
                        bd=0, relief="flat")
        self.tree.tag_configure("paused", foreground=P["muted"])
        self.tree.tag_configure("error", foreground=P["bad"])
        self.root.after(120, lambda: ui.set_glass(self.root, self.P))  # once the window is mapped
        if getattr(self, "chrome", None):
            self.chrome.restyle(self.P)
        if getattr(self, "empty", None) is not None:
            self.root.after_idle(self._layout_art)
        if getattr(self, "tabstrip", None) is not None:
            self._redraw_details()
        self._paint_sidebar()
        self._draw_toolbar()
        self._update_alt_label()
        self._draw_status()
        self.tree.redraw()

    # -- persistence ------------------------------------------------------
    def save(self):
        entries = []
        for h, s in sorted(self.mgr.sessions.items(), key=lambda kv: kv[1].order):   # queue order
            e = {"hash": h, "dir": s.save_dir, "paused": bool(s.user_paused)}
            if isinstance(s, Session) and s.skip:
                e["skip"] = sorted(s.skip)
            if isinstance(s, Session) and s.high:
                e["high"] = sorted(s.high)
            if isinstance(s, Session) and s.sequential:
                e["seq"] = 1
            if getattr(s, "category", ""):
                e["category"] = s.category
            if getattr(s, "priority", 0):
                e["priority"] = s.priority
            if isinstance(s, Session):
                if s.down_lim.rate or s.up_lim.rate:
                    e["limits"] = [s.down_lim.rate // 1024, s.up_lim.rate // 1024]
                if s.complete:
                    e["up_total"], e["seed_secs"] = int(s.up_base + s.uploaded), int(s.seed_secs)
                    if s.rule_hit or s.rule_ignore:
                        e["rule"] = 1
            if hasattr(s, "uri"):
                e["magnet"] = s.uri
            entries.append(e)
        self.cfg["torrents"] = entries
        self.cfg["banned"] = sorted(self.mgr.banned)
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.cfg, f, indent=1)
        except OSError:
            pass

    def torrent_path(self, h):
        return os.path.join(self.dir, "torrents", h + ".torrent")

    def resume_path(self, h):
        return os.path.join(self.dir, "resume", h + ".json")

    def load_resume(self, h):
        try:
            with open(self.resume_path(h)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save_resume(self):
        """Remember each torrent's verified pieces and file timestamps so the next start can skip the hash check."""
        last = self.__dict__.setdefault("_resume_last", {})
        for h, s in list(self.mgr.sessions.items()):
            data = s.resume_data() if isinstance(s, Session) else None
            if not data or last.get(h) == data:
                continue
            tmp = self.resume_path(h) + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, self.resume_path(h))
                last[h] = data
            except OSError:
                pass

    def _resume_tick(self):
        self.save_resume()
        self.root.after(30000, self._resume_tick)

    def restore(self):
        for e in self.cfg.get("torrents", []):
            try:
                if os.path.exists(self.torrent_path(e["hash"])):
                    with open(self.torrent_path(e["hash"]), "rb") as f:
                        sess, _new = self.mgr.add(f.read(), e["dir"], start=not e.get("paused"), skip=e.get("skip"),
                                                  resume=self.load_resume(e["hash"]), high=e.get("high"),
                                                  sequential=bool(e.get("seq")))
                    sess.up_base, sess.seed_secs = int(e.get("up_total", 0)), float(e.get("seed_secs", 0))
                    if e.get("limits"):
                        sess.set_limits(*e["limits"])
                    sess.rule_hit = bool(e.get("rule"))   # a rule already stopped it: do not fire again
                elif e.get("magnet"):
                    sess, _new = self.mgr.add_magnet(e["magnet"], e["dir"], start=not e.get("paused"))
                else:
                    continue
                sess.category, sess.priority = e.get("category", ""), int(e.get("priority", 0))
            except (OSError, ValueError, KeyError):
                pass

    def on_metadata(self, h, data):
        """Called from the network thread when a magnet link has resolved to a torrent."""
        with open(self.torrent_path(h), "wb") as f:
            f.write(data)
        self.save()

    # -- adding -----------------------------------------------------------
    def add_dialog(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="Select .torrent files",
                                            filetypes=[("Torrent files", "*.torrent"), ("All files", "*.*")])
        if paths:
            self.add_torrents(list(paths))

    def add_torrents(self, paths):
        items, bad = [], []
        for p in paths:
            try:
                with open(p, "rb") as f:
                    raw = f.read()
                items.append((p, raw, Torrent(raw)))
            except Exception as e:
                bad.append("%s: %s" % (os.path.basename(p), e))
        if bad:
            messagebox.showerror(APP, "Could not read:\n\n" + "\n".join(bad), parent=self.root)
        if not items:
            return
        res = AddDialog(self, "torrent", items).show()
        if not res:
            return
        self.cfg["download_dir"] = res["folder"]
        for _p, raw, _t in items:
            self._add_raw(raw, res["folder"], res["start"], skip=res.get("skip") if len(items) == 1 else None,
                          category=res.get("category", ""))

    def _add_raw(self, raw, folder, start=True, skip=None, category="", interactive=True):
        """Add a .torrent. interactive=False (RSS): never opens a dialog; problems go to the log. Returns the session."""
        if start and not self._space_ok(raw, folder, skip, ask=interactive):
            return None
        try:
            s, new = self.mgr.add(raw, folder, start=start, skip=skip)
        except ValueError as e:
            if interactive:
                messagebox.showerror(APP, "Could not add this torrent\n\n%s" % e, parent=self.root)
            else:
                self.log("Could not add a torrent: %s" % e)
            return None
        if not new:
            if interactive:
                messagebox.showinfo(APP, "'%s' is already in the list." % s.t.name, parent=self.root)
            return None
        s.category = category
        with open(self.torrent_path(s.t.hex), "wb") as f:
            f.write(raw)
        self.log("Added %s" % s.t.name)
        self.save()
        return s

    def add_file(self, path, folder):
        """Add a .torrent without asking (files opened from Explorer / forwarded by a second launch)."""
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            messagebox.showerror(APP, "Could not read %s\n\n%s" % (os.path.basename(path), e), parent=self.root)
            return
        self._add_raw(raw, folder)

    def add_magnet_dialog(self, initial=""):
        if not isinstance(initial, str):
            initial = ""
        if not initial:
            try:
                clip = self.root.clipboard_get().strip()
                initial = clip if clip.lower().startswith("magnet:") else ""
            except tk.TclError:
                pass
        res = AddDialog(self, "magnet", initial=initial).show()
        if res:
            self.cfg["download_dir"] = res["folder"]
            self.add_magnet(res["link"], res["folder"], res["start"], res.get("category", ""))

    def create_torrent(self):
        CreateTorrentDialog(self).show()

    def load_search_sources(self):
        """Built-in + plugin sources, read fresh from the plugin folder each time."""
        import search
        sources, errors = search.load_sources(self.dir)
        if self.cfg.get("rss"):       # search the items of your own RSS feeds too
            sources.insert(0, search.Source("My RSS feeds", lambda q, limit: rss.search_feeds(list(self.cfg["rss"]), q, limit)))
        return sources, errors

    def search_online(self, query=""):
        if not isinstance(query, str):
            query = ""
        win = getattr(self, "_search_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.reload_sources(); win.deiconify(); win.lift(); win.focus_force()
                if query:
                    win.query.set(query)
                    win.run()
                return
        except tk.TclError:
            pass
        sources, errors = self.load_search_sources()
        self._search_win = SearchWindow(self, sources, errors, query)
        ui.center_over(self._search_win, self.root)

    def paste_magnet(self):
        try:
            clip = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if clip.lower().startswith("magnet:"):
            self.add_magnet_dialog(clip)
        elif clip.lower().startswith(("http://", "https://")) and "\n" not in clip \
                and not isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox)):
            self.handle_link(clip)            # a website address: look for its RSS feeds

    def add_magnet(self, uri, folder, start=True, category="", interactive=True):
        try:
            s, new = self.mgr.add_magnet(uri, folder, start=start)
        except ValueError as e:
            if interactive:
                messagebox.showerror(APP, "Invalid magnet link\n\n%s" % e, parent=self.root)
            else:
                self.log("Skipped an invalid magnet link: %s" % e)
            return None
        if not new:
            if interactive:
                messagebox.showinfo(APP, "That torrent is already in the list.", parent=self.root)
            return None
        s.category = category
        self.log("Added magnet link")
        if interactive and not s.t.trackers and not s.candidates and not self.mgr.dht:
            messagebox.showwarning(APP, "This magnet link lists no trackers and the DHT is unavailable, so it may "
                                        "never find peers.", parent=self.root)
        self.save()
        return s

    # -- RSS feeds --------------------------------------------------------
    def rss_dialog(self):
        RSSDialog(self).show()

    def check_feed(self, idx, manual=False):
        """Fetch one feed on a worker thread; the result comes back through rssq and is handled in poll()."""
        feeds = self.cfg.get("rss", [])
        if not 0 <= idx < len(feeds):
            return
        feed = dict(feeds[idx])
        url = feed["url"]
        if url in self._rss_busy:
            return
        self._rss_busy.add(url)
        feeds[idx]["last"] = time.time()            # also after a failure, so a broken feed is not hammered
        feeds[idx]["status"] = "Checking\u2026"
        seen = list(self.cfg.get("rss_seen", {}).get(url, []))

        def work():
            try:
                self.rssq.put((url, rss.run_feed(feed, seen), manual))
            except Exception as e:
                self.rssq.put((url, "%s" % e, manual))
        threading.Thread(target=work, daemon=True).start()

    def _rss_tick(self):
        for i, feed in enumerate(self.cfg.get("rss", [])):
            if rss.due(feed) and feed["url"] not in self._rss_busy:
                self.check_feed(i)

    def _handle_rss(self):
        while True:
            try:
                url, res, _manual = self.rssq.get_nowait()
            except queue.Empty:
                return
            self._rss_busy.discard(url)
            feed = next((f for f in self.cfg.get("rss", []) if f["url"] == url), None)
            if feed is None:
                continue
            stamp = time.strftime("%H:%M")
            if isinstance(res, str):
                feed["status"] = "Error: %s" % res
                self.log("RSS %s: %s" % (feed.get("name") or url, res))
                continue
            added = 0
            for item, kind, payload in res["jobs"]:
                folder = feed.get("folder") or self.cfg.get("categories", {}).get(feed.get("category", "")) \
                    or self.cfg["download_dir"]
                start = bool(feed.get("start", True))
                if kind == "magnet":
                    s = self.add_magnet(payload, folder, start, feed.get("category", ""), interactive=False)
                else:
                    s = self._add_raw(payload, folder, start, category=feed.get("category", ""), interactive=False)
                if s is not None:
                    added += 1
                    self.log("RSS %s: added %s" % (feed.get("name") or url, item["title"] or getattr(getattr(s, "t", None), "name", "")))
            for e in res["errors"]:
                self.log("RSS %s: %s" % (feed.get("name") or url, e))
            seen = self.cfg.setdefault("rss_seen", {})
            seen[url] = rss.remember(seen.get(url, []), res["ids"])
            feed["primed"] = True
            if not feed.get("name") and res["title"]:
                feed["name"] = res["title"]
            feed["status"] = "OK at %s: %d item(s) in the feed, %d added%s" % (
                stamp, res["total"], added, ", %d failed" % len(res["errors"]) if res["errors"] else "")
            self.save()

    def poll(self):
        """Handle files/links forwarded from a second launch (e.g. clicking a magnet link)."""
        try:
            while True:
                args = self.inbox.get_nowait()
                if "--quit" in args:          # `BitTorrentClient.exe --quit` (the installer, before an update)
                    self.log("Asked to quit by another launch (installer / uninstaller)")
                    self.quit()
                    return
                for a in args:
                    if a.lower().startswith("magnet:"):
                        self.add_magnet(a, self.cfg["download_dir"])
                    elif a.lower().endswith(".torrent") and os.path.isfile(a):
                        self.add_file(a, self.cfg["download_dir"])
                self.show_window()
        except queue.Empty:
            pass
        names, hashes = [], []
        while True:
            try:
                h, name = self.finished.get_nowait()
            except queue.Empty:
                break
            names.append(name)
            hashes.append(h)
            self.log("Finished %s" % name)
            self._after_finish(h)
        if names:
            self.notify_finished(names, hashes)
        self._handle_seed_limits()
        self._handle_updates()
        self._handle_rss()
        if time.time() - getattr(self, "_rss_checked", 0) > 30:
            self._rss_checked = time.time()
            self._rss_tick()
        if time.time() - getattr(self, "_speed_checked", 0) > 20:
            self._speed_checked = time.time()
            self._apply_speed()
        if getattr(self, "_save_pending", False):
            self._save_pending = False
            self.save()
        self.root.after(300, self.poll)

    def _after_finish(self, h):
        """A download completed: move it to the 'finished' folder if one is set (and it is not in a category folder)."""
        s = self.mgr.sessions.get(h)
        dest = self.cfg.get("done_dir", "").strip()
        if not dest or not isinstance(s, Session) or not s.complete or s.category in self.cfg.get("categories", {}) \
                and self.cfg["categories"][s.category]:
            return
        if os.path.abspath(s.save_dir) == os.path.abspath(dest):
            return

        def work():
            try:
                os.makedirs(dest, exist_ok=True)
                self.mgr.relocate(h, dest)
            except Exception:
                pass
            self._save_pending = True      # saved by poll() on the Tk thread
        threading.Thread(target=work, daemon=True).start()

    def _handle_seed_limits(self):
        done = []
        while True:
            try:
                h, action = self.seedq.get_nowait()
            except queue.Empty:
                break
            s = self.mgr.sessions.get(h)
            if s is None:
                continue
            done.append(s.t.name if isinstance(s, Session) else h)
            if action == "remove":
                self.mgr.remove(h)
                for path in (self.torrent_path(h), self.resume_path(h)):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            else:
                self.mgr.pause(h)
        if done:
            self.save()
            self.refresh_now()
            if self.cfg.get("notify", True) and tray.available():
                text = "%s\nreached its seeding limit." % done[0] if len(done) == 1 else \
                    "%d torrents reached their seeding limit." % len(done)
                if self.tray.notify("Seeding finished", text):
                    self.root.after(15000, self._hide_temp_tray)

    def notify_finished(self, names, hashes=None):
        """Tell the user a download finished: an on-screen toast (always works), a sound, a flashing taskbar button, and the
        Windows tray notification."""
        if not self.cfg.get("notify", True):
            return
        if len(names) == 1:
            text = "%s\nhas finished downloading." % names[0]
        else:
            text = "%d downloads have finished:\n%s" % (len(names), "\n".join(names[:3]) + ("\n\u2026" if len(names) > 3 else ""))
        try:
            from dialogs import Toast
            self._toast = Toast(self, "Download complete", text, hashes[0] if hashes and len(hashes) == 1 else None)
        except Exception:
            import traceback
            self.log("Notification failed: %s" % traceback.format_exc().splitlines()[-1])
        if self.cfg.get("notify_sound", True):
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass
        self._flash_taskbar()
        if tray.available() and self.tray.notify("Download complete", text):
            self.root.after(15000, self._hide_temp_tray)

    def _flash_taskbar(self):
        """Make the taskbar button flash until the window is used (Windows only; does nothing if it is in focus)."""
        try:
            import ctypes
            from ctypes import wintypes
            if self.root.focus_displayof() is not None or self.root.state() == "withdrawn":
                return

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                            ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x0F, 5, 0)     # FLASHW_ALL | FLASHW_TIMERNOFG
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def check_updates(self, manual=False, parent=None):
        """Look for a new version (in the background, about once a day unless `manual`). An installed copy also
        downloads it; _handle_updates then asks before installing. See updater.py."""
        import updater
        if not manual and (not self.cfg.get("update_check", True)
                           or time.time() - self.cfg.get("update_last", 0) < 86400):
            return
        if getattr(self, "_update_busy", False):
            return
        self._update_busy = True
        folder = os.path.join(self.dir, "updates")

        def work():
            try:
                rel = updater.latest()
                if not updater.is_newer(rel, version_mod.VERSION):
                    self.updq.put((manual, parent, "none", rel))
                elif updater.can_self_update():
                    self.updq.put((manual, parent, "ready", (rel, updater.download(rel, folder))))
                else:                                                   # running from source / a portable folder
                    self.updq.put((manual, parent, "newer", rel))
            except Exception as e:
                self.updq.put((manual, parent, "err", str(e) or e.__class__.__name__))
        threading.Thread(target=work, name="updater", daemon=True).start()

    def _handle_updates(self):
        import updater
        while True:
            try:
                manual, parent, kind, val = self.updq.get_nowait()
            except queue.Empty:
                return
            self._update_busy = False
            parent = parent if (parent is not None and parent.winfo_exists()) else self.root
            if kind == "err":
                self.log("Update check failed: %s" % val)
                if manual:
                    messagebox.showerror(APP, "Could not check for updates:\n\n%s" % val, parent=parent)
                continue
            self.cfg["update_last"] = time.time()
            if kind == "none":
                if manual:
                    messagebox.showinfo(APP, "You are up to date (version %s)." % version_mod.VERSION, parent=parent)
            elif kind == "newer":
                if manual and messagebox.askyesno(APP, "Version %s is available (you have %s).\n\nOpen the download page?"
                                                  % (val["version"], version_mod.VERSION), parent=parent) \
                        and val["page"].startswith("https://"):
                    import webbrowser
                    webbrowser.open(val["page"])
            elif kind == "ready":
                rel, path = val
                self._update_ready = path
                self.log("Version %s downloaded and ready to install" % rel["version"])
                if messagebox.askyesno(APP, "Version %s is ready to install (you have %s).\n\nInstall it now? "
                                       "Bit Torrent Client closes and reopens by itself; downloads continue afterwards. "
                                       "Windows may ask for permission.\n\nChoose No to install it when you next exit."
                                       % (rel["version"], version_mod.VERSION), parent=parent):
                    self._install_update(relaunch=True)

    def _install_update(self, relaunch):
        """Run the downloaded installer; it closes this copy with --quit and (if `relaunch`) starts the new one."""
        import updater
        path, self._update_ready = getattr(self, "_update_ready", None), None
        if not path or not os.path.isfile(path):
            return
        try:
            updater.run_installer(path, relaunch=relaunch)
            self.log("Installing the update")
        except OSError as e:
            self.log("Could not start the update installer: %s" % e)

    def _hide_temp_tray(self):
        """The icon was only there to show a notification: remove it again, unless the window is hidden to the tray."""
        if self.tray.temporary and self.root.state() != "withdrawn":
            self.tray.hide()

    def register_magnet(self):
        try:
            import winreg
        except ImportError:
            messagebox.showinfo(APP, "This option only works on Windows.", parent=self.root)
            return
        if getattr(sys, "frozen", False):
            cmd = '"%s" "%%1"' % sys.executable
        else:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            cmd = '"%s" "%s" "%%1"' % (pyw, os.path.abspath(__file__))
        try:
            base = r"Software\Classes\magnet"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
                winreg.SetValue(k, "", winreg.REG_SZ, "URL:Magnet Link")
                winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
                winreg.SetValue(k, "", winreg.REG_SZ, cmd)
        except OSError as e:
            messagebox.showerror(APP, "Could not register: %s" % e, parent=self.root)
            return
        messagebox.showinfo(APP, "Done. If Windows asks which app should open magnet links, "
                                 "choose Bit Torrent Client (or set it in Settings > Apps > Default apps).", parent=self.root)

    # -- port forwarding help ---------------------------------------------
    def diagnostics_dialog(self):
        DiagnosticsDialog(self).show()

    def check_firewall(self):
        """Look up the Windows Firewall state in the background (PowerShell is slow to start)."""
        if not firewall.available():
            return

        def work():
            self.fw_state = firewall.check()
        threading.Thread(target=work, name="fwcheck", daemon=True).start()

    def check_network(self):
        """Look for software filtering local connections (AdGuard and the like), in the background."""
        if getattr(self, "_net_busy", False):
            return
        self._net_busy, self.net_state, box = True, None, {}
        threading.Thread(target=lambda: box.setdefault("r", netcheck.check()), name="netcheck", daemon=True).start()

        def done():
            if "r" not in box:
                self.root.after(500, done)
                return
            self.net_state, self._net_busy = box["r"], False
            if not self.net_state["ok"]:
                self.log(netcheck.summary(self.net_state))
                self.log(netcheck.hint(self.net_state))
        self.root.after(500, done)

    def fw_text(self):
        st = self.fw_state if self.fw_state is not None else firewall.check()
        self.fw_state = st
        return {"allowed": "Windows Firewall allows incoming connections to this program.",
                "blocked": "Windows Firewall is BLOCKING incoming connections to this program, so forwarded ports "
                           "will not work. Click the button to fix this (one Windows permission prompt).",
                "missing": "No Windows Firewall rule found for this program. Windows may block incoming "
                           "connections. Click the button to add an allow rule."}.get(
            st, "Could not read the Windows Firewall state. If port forwarding fails, click the button to add an allow rule.")

    def fix_firewall(self):
        if not firewall.add_rule():
            messagebox.showinfo(APP, "The firewall rule was not added (the permission prompt was declined).", parent=self.root)
            return
        self.fw_state = None
        messagebox.showinfo(APP, "The firewall rule is being added. Re-open Settings in a few seconds to confirm.",
                            parent=self.root)
        self.root.after(6000, self.check_firewall)

    def hints(self):
        """Plain-language advice from what was found."""
        out = []
        pm = self.mgr.portmap
        net = netcheck.hint(self.net_state)
        if net:
            out.append(net)
        if self.fw_state == "blocked":
            out.append("Windows Firewall is blocking this program: Settings > Connection > Allow in Windows Firewall.")
        elif self.fw_state == "missing":
            out.append("No Windows Firewall allow rule exists for this program: Settings > Connection > Allow in Windows Firewall.")
        if pm is not None:
            s4 = pm.status4.lower()
            if "searching" not in s4 and "forwarded" not in s4:
                out.append("No router answered. Enable UPnP (or NAT-PMP/PCP) in the router's settings, or forward port "
                           "%s TCP+UDP to this PC by hand." % (self.mgr.port or self.cfg["port"]))
            if "not public" in s4:
                out.append("The router's external address is private (double NAT, e.g. modem plus router, or CGNAT). "
                           "Forward the port on the upstream device too, or ask the ISP for a public address.")
        return out

    def diagnostics_text(self):
        pm = self.mgr.portmap
        parts = ["Windows Firewall: %s" % (self.fw_state or "unknown"), netcheck.summary(self.net_state)]
        parts.append(pm.diagnostics() if pm else "Automatic port forwarding is turned off.")
        hints = self.hints()
        if hints:
            parts.insert(0, "What to try:\n" + "\n".join("  - " + h for h in hints) + "\n")
        return "\n".join(parts)

    # -- settings ---------------------------------------------------------
    def settings_dialog(self):
        d = getattr(self, "_dropdown", None)
        if d is not None:                        # the gear again: close the drop-down
            try:
                d.destroy()
            except tk.TclError:
                pass
            self._dropdown = None
            return
        res = SettingsDialog(self).show()
        if res:
            self.apply_settings(**res)

    def apply_settings(self, down_limit, up_limit, encryption, utp, portmap, theme, download_dir, port=None, tray=None, notify=None, notify_sound=None, native_frame=None,
                       max_active=None, autostart=None, seed_ratio=None, seed_minutes=None, seed_action=None,
                       done_dir=None, categories=None, update_repo=None, update_check=None, player=None, libmpv=None, player_exe=None, player_where=None, media=None,
                       alt_down=None, alt_up=None, sched_on=None, sched_start=None, sched_end=None, sched_days=None):
        self.cfg.update(down_limit=max(0, int(down_limit)), up_limit=max(0, int(up_limit)), encryption=encryption,
                        utp=utp, portmap=bool(portmap), theme=theme, download_dir=download_dir)
        if alt_down is not None:
            self.cfg.update(alt_down=max(0, int(alt_down)), alt_up=max(0, int(alt_up or 0)), sched_on=bool(sched_on),
                            sched_start=sched_start or "08:00", sched_end=sched_end or "23:00",
                            sched_days=list(sched_days if sched_days is not None else range(7)))
        if seed_ratio is not None:
            self.cfg.update(seed_ratio=float(seed_ratio), seed_minutes=int(seed_minutes or 0),
                            seed_action=seed_action or "pause")
            self.mgr.set_seed_rules(self.cfg["seed_ratio"], self.cfg["seed_minutes"], self.cfg["seed_action"])
        if done_dir is not None:
            self.cfg["done_dir"] = done_dir
        if categories is not None:
            self.cfg["categories"] = dict(categories)
            for sess in self.mgr.sessions.values():          # drop the category of torrents whose category was deleted
                if getattr(sess, "category", "") and sess.category not in categories:
                    sess.category = ""
        if update_repo is not None:
            self.cfg["update_repo"] = update_repo
        if update_check is not None:
            self.cfg["update_check"] = bool(update_check)
        if player is not None:
            self.cfg.update(player=player, libmpv=libmpv or "", player_exe=player_exe or "", player_where=player_where or "window")
            if media:
                self.cfg.update(media)
                if not media.get("lan_share") and getattr(self, "_streamer", None):
                    self._streamer.stop_lan()
        if tray is not None:
            self.cfg["tray"] = bool(tray)
        if autostart is not None and autostart_mod.available() and autostart != autostart_mod.is_enabled():
            autostart_mod.set_enabled(autostart)
        if notify is not None:
            self.cfg["notify"] = bool(notify)
        if notify_sound is not None:
            self.cfg["notify_sound"] = bool(notify_sound)
        if native_frame is not None:
            self.cfg["native_frame"] = bool(native_frame)
        if max_active is not None:
            self.cfg["max_active"] = max(0, int(max_active))
            self.mgr.set_max_active(self.cfg["max_active"])
        if port and 1024 <= int(port) <= 65535:
            self.cfg["port"] = int(port)   # used from the next start
        self._apply_speed()
        self.mgr.encryption = encryption
        self.mgr.utp_mode = utp
        self.mgr.set_portmap(bool(portmap))
        self.P = ui.palette(theme)
        self.apply_theme()
        self.save()

    # -- actions ----------------------------------------------------------
    def _selected(self):
        return [h for h in self.tree.selection() if h in self._snap_by_hash]

    def pause(self):
        for h in self._selected():
            self.mgr.pause(h)
            self.log("Paused %s" % self._snap_by_hash.get(h, {}).get("name", h[:8]))
        self.save()

    def resume(self):
        for h in self._selected():
            self.mgr.resume(h)
            self.log("Resumed %s" % self._snap_by_hash.get(h, {}).get("name", h[:8]))
        self.save()

    def files_dialog(self):
        sel = self._selected()
        if len(sel) != 1:
            messagebox.showinfo(APP, "Select one torrent to choose its files.", parent=self.root)
            return
        s = self.mgr.sessions.get(sel[0])
        if not isinstance(s, Session):
            messagebox.showinfo(APP, "The file list is not known yet. It appears as soon as the magnet link has found "
                                     "its metadata.", parent=self.root)
            return
        res = FilesDialog(self, s.t, s.skip, progress=s.file_progress(), title="Files").show()
        if res is None or res == s.skip:
            return
        try:
            self.mgr.set_skip(sel[0], res)
        except ValueError as e:
            messagebox.showerror(APP, str(e), parent=self.root)
            return
        self.save()
        self.refresh_now()

    def recheck(self):
        sel = self._selected()
        if sel and messagebox.askyesno(APP, "Verify all downloaded data of %d torrent(s) again?\n\nThis reads every "
                                            "file and can take a while." % len(sel), parent=self.root):
            for h in sel:
                self.mgr.recheck(h)

    def move_in_queue(self, where):
        sel = self._selected()
        if sel:
            self.mgr.move(set(sel), where)
            self.save()

    def refresh_now(self):
        """Redraw straight away (e.g. after a dialog changed something) without adding a second refresh loop."""
        self._refresh_once(graph=False)

    def toggle_pause(self):
        states = [self._snap_by_hash[h]["raw_state"] for h in self._selected()]
        if any(s in RUNNING or s == "Queued" for s in states):
            self.pause()
        else:
            self.resume()

    def remove(self):
        sel = self._selected()
        if not sel:
            return
        ans = messagebox.askyesnocancel(
            APP, "Remove %d torrent(s)?\n\nYes: also delete the downloaded files\nNo: keep the files" % len(sel),
            parent=self.root)
        if ans is None:
            return
        for h in sel:
            self.log("Removed %s%s" % (self._snap_by_hash.get(h, {}).get("name", h[:8]), " and its files" if ans else ""))
            self.mgr.remove(h, delete_files=ans)
            for path in (self.torrent_path(h), self.resume_path(h)):
                try:
                    os.remove(path)
                except OSError:
                    pass
            self.__dict__.setdefault("_resume_last", {}).pop(h, None)
        self.save()

    def open_folder(self):
        sel = self._selected()
        if not sel or sel[0] not in self.mgr.sessions:
            return
        d = self.mgr.sessions[sel[0]].save_dir
        if hasattr(os, "startfile"):
            os.startfile(d)
        else:
            subprocess.Popen(["xdg-open", d])

    def _on_space(self, _e):
        self.toggle_pause()
        return "break"   # stop Tk's own Space handling (it would toggle the row's selection)

    def _popup(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self._show_menu(e.x_root, e.y_root)

    # -- sorting / columns ------------------------------------------------
    def sort_by(self, col):
        self.sort_rev = (not self.sort_rev) if self.sort_col == col else False
        self.sort_col = col
        for c, h, _a in COLS:
            arrow = ("  \u25BC" if self.sort_rev else "  \u25B2") if c == col else ""
            self.tree.heading(c, text=h + arrow)
        self._apply_sort(list(self._snap_by_hash.values()))

    def _apply_sort(self, snap):
        if not self.sort_col:
            return
        order = [s["hash"] for s in sorted(snap, key=SORT_KEYS[self.sort_col], reverse=self.sort_rev)]
        for i, h in enumerate(order):
            if self.tree.exists(h) and h not in self._hidden and self.tree.index(h) != i:
                self.tree.move(h, "", i)

    # relative widths: a wide Name column, narrow Down / Up / Peers / ETA
    COL_WEIGHT = {"name": 4.0, "size": 1.1, "progress": 1.5, "status": 1.5, "down": 1.0, "up": 1.0, "peers": 0.8, "eta": 0.9}
    COL_MIN = {"name": 170, "size": 70, "progress": 90, "status": 110, "down": 70, "up": 70, "peers": 55, "eta": 65}

    def _column_widths(self, total):
        """Split `total` pixels by weight, never going below a column's minimum."""
        mins = {c: self.px(self.COL_MIN[c]) for c in self.cols}
        fixed, free = {}, list(self.cols)
        while True:
            room = total - sum(fixed.values())
            wsum = sum(self.COL_WEIGHT[c] for c in free)
            widths = {c: room * self.COL_WEIGHT[c] / wsum for c in free}
            low = [c for c in free if widths[c] < mins[c]]
            if not low:
                break
            for c in low:
                fixed[c] = mins[c]
                free.remove(c)
            if not free:
                return fixed
        out = dict(fixed)
        out.update({c: int(w) for c, w in widths.items()})
        return out

    def _equalize_columns(self):
        """Size the columns by their weights so they fill the table (Name gets the most room)."""
        for c, w in self._column_widths(self.tree.winfo_width()).items():
            self.tree.column(c, width=max(w, self.px(self.COL_MIN[c])), stretch=False)

    def _on_resize(self):
        self._equalize_columns()
        self._fit_details()

    def _fit_details(self):
        """Taller window -> more rows in the Files / Peers / Trackers tables (4 on a small window, up to 12)."""
        try:
            rows = max(4, min(12, 4 + (self.root.winfo_height() - self.px(620)) // self.px(30)))
            for tv in self.tab_trees.values():
                if int(tv.cget("height")) != rows:
                    tv.configure(height=rows)
        except (tk.TclError, ValueError, AttributeError):
            pass

    # -- progress bars (drawn by the list itself, so they sit on the artwork) -------------------------------
    def _yscroll(self, a, b):
        self._sb_set(a, b)

    def _paint_bar(self, c, iid, x, y, w, h, selected):
        """Pill-shaped bar in the state colour; an active download gets a soft halo."""
        P, px = self.P, self.px
        bar_h = px(8)
        x0, x1, cy = x + px(10), x + max(w - px(10), px(30)), y + h // 2
        if x1 - x0 < bar_h * 2:
            return
        kind = self.bar_kind.get(iid, "Idle")
        col = P[ui.BAR_KINDS.get(kind, "bar_idle")]
        r = bar_h // 2
        c.create_line(x0 + r, cy, x1 - r, cy, width=bar_h, capstyle="round", fill=P["trough"])
        frac = max(0.0, min(1.0, self.progress.get(iid, 0) / 100.0))
        if frac > 0:
            end = x0 + r + (x1 - x0 - 2 * r) * frac
            if kind == "Down":
                c.create_line(x0 + r, cy, end, cy, width=bar_h + px(6), capstyle="round",
                              fill=arttable.blend(col, P["bg"], 0.78))
            c.create_line(x0 + r, cy, max(end, x0 + r + 0.5), cy, width=bar_h, capstyle="round", fill=col)

    def place_bars(self):
        """Kept for callers from before the bars were drawn by the list: just repaint it."""
        self.tree.redraw()

    # -- live display -----------------------------------------------------
    def _status_text(self, s):
        if s["error"]:
            return "Error: " + s["error"]
        if s["raw_state"] == "Downloading":
            return "Downloading %.1f%%" % (s["progress"] * 100)
        if s.get("magnet") and s["raw_state"] == "Fetching metadata" and s["progress"]:
            return "Fetching metadata %d%%" % (s["progress"] * 100)
        return s["state"]

    def refresh(self):
        self._refresh_once()
        self.root.after(1000, self.refresh)

    def _refresh_once(self, graph=True):
        snap = self.mgr.snapshot()
        self._snap_by_hash = {s["hash"]: s for s in snap}
        for s in snap:
            h = s["hash"]
            self.progress[h] = s["progress"] * 100
            kind = "Err" if s["error"] else BAR_KIND.get(s["raw_state"], "Idle")
            self.bar_kind[h] = kind
            tags = ("error",) if s["error"] else ("paused",) if s["raw_state"] in ("Paused", "Queued") else ()
            arrow = {1: "\u25B2 ", -1: "\u25BC "}.get(s.get("priority", 0), "")
            vals = (arrow + s["name"], fmt_size(s["size"]) if s["size"] else "-", "", self._status_text(s),
                    fmt_rate(s["down"]), fmt_rate(s["up"]), s["peers"], fmt_eta(s["eta"]))
            if self.tree.exists(h):
                self.tree.item(h, values=vals, tags=tags)
            else:
                self.tree.insert("", "end", iid=h, values=vals, tags=tags)
        self._apply_filter(snap)
        for iid in list(self.tree.get_children()) + list(self._hidden):
            if iid not in self._snap_by_hash:
                self.tree.delete(iid)
                self._hidden.discard(iid)
        for iid in list(self.progress):
            if iid not in self._snap_by_hash:
                for d in (self.progress, self.bar_kind):
                    d.pop(iid, None)
        self._apply_sort(snap)
        self.tree.redraw()          # progress values changed even where the row text did not
        self._toggle_empty(not snap)
        self._update_buttons()
        self._update_details()
        self._update_statusbar(snap)
        self._log_changes(snap)
        self._update_sidebar(snap)
        if graph:
            for s in snap:
                self.graph.push(s["hash"], s["down"], s["up"])
            self.graph.push(None, sum(s["down"] for s in snap), sum(s["up"] for s in snap))
        self.graph.forget(set(self._snap_by_hash))
        sel = tuple(self._selected())
        if sel != self._tab_hash:
            self._tab_hash = sel
            self._reset_tabs()
        self._update_tabs()

    def _apply_filter(self, snap):
        """Hide the rows the sidebar filter and search box rule out (and bring back those that match again)."""
        q = self.search.get()
        for s in snap:
            h = s["hash"]
            if h not in self._hidden and not self.tree.exists(h):
                continue    # added a moment ago: the next refresh() creates its row
            show = panels.matches_filter(self.filter_key, s) and panels.matches_search(q, s)
            if show and h in self._hidden:
                self.tree.move(h, "", "end")
                self._hidden.discard(h)
            elif not show and h not in self._hidden:
                self.tree.detach(h)
                self._hidden.add(h)

    def _update_sidebar(self, snap):
        self._sync_categories()
        counts = panels.count_filters(snap)
        for key, (_row, _name, count) in self.side_rows.items():
            n = counts.get(key)
            if n is None:
                n = sum(1 for s in snap if panels.matches_filter(key, s))
            count.configure(text=str(n) if n else "")

    def _toggle_empty(self, show):
        self._empty_shown = show
        self._layout_art()

    def is_fullscreen(self):
        try:
            if self.chrome is not None:
                return bool(self.chrome.full)
            return bool(int(self.root.attributes("-fullscreen")))
        except (tk.TclError, TypeError, ValueError):
            return False

    def toggle_fullscreen(self, on=None):
        """Full screen (F11, or the title bar's corner button): the window covers the whole monitor, taskbar
        included. F11 again, Esc or the button bring it back to how it was."""
        on = (not self.is_fullscreen()) if on is None else bool(on)
        if on == self.is_fullscreen():
            return
        if self.chrome is not None:
            self.chrome.fill_screen(on)
        else:
            self.root.attributes("-fullscreen", on)

    def _art_moved(self):
        """The window changed size: the picture is centred on it, so every part redraws its slice. An open
        Settings drop-down closes (it hangs from the gear), other dialogs' see-through backdrops repaint."""
        d = getattr(self, "_dropdown", None)
        if d is not None:
            try:
                d.destroy()
            except tk.TclError:
                pass
            self._dropdown = None
        for w in self.root.winfo_children():
            if hasattr(w, "_paint_backdrop"):
                w._paint_backdrop()
        for fn in (self._draw_toolbar, self._draw_status, self._draw_sidebar, self.tree.redraw,
                   getattr(self, "_redraw_details", None), getattr(self.chrome, "draw", None)):
            if fn is not None:
                try:
                    fn()
                except (tk.TclError, AttributeError):
                    pass
        if self._empty_shown:
            self._draw_empty()

    def _draw_list_art(self, c):
        """The list draws its slice of the window's artwork behind its rows, so adding torrents never shrinks it."""
        self._art_at(c, self.root)

    def _layout_art(self):
        """With no rows shown, the empty-state canvas (same artwork, plus the hint) covers the list's rows -
        not its heading row, which stays visible (Name, Size, Progress ...)."""
        try:
            if self.tree.get_children():
                self.empty.place_forget(); return
            head = int(getattr(self.tree, "head_height", 0) or 0)
            self.empty.place(x=self.tree.winfo_x(), y=self.tree.winfo_y() + head, width=self.tree.winfo_width(),
                             height=max(1, self.tree.winfo_height() - head))
            try:
                tk.Misc.tkraise(self.empty)           # (Canvas.lift would raise a canvas item instead)
            except AttributeError:
                self.empty.lift()
            self._draw_empty()
        except tk.TclError:
            pass

    def _on_select(self):
        self._update_buttons()
        self._update_details()

    def _update_buttons(self):
        states = [self._snap_by_hash[h]["raw_state"] for h in self._selected()]
        self.btn["resume"].set_enabled(any(s == "Paused" for s in states))
        self.btn["pause"].set_enabled(any(s in RUNNING or s == "Queued" for s in states))
        one = self._selected()
        self.btn["files"].set_enabled(len(one) == 1 and isinstance(self.mgr.sessions.get(one[0]), Session))
        self.btn["remove"].set_enabled(bool(states))
        self.btn["folder"].set_enabled(bool(states))

    def _update_details(self):
        sel = self._selected()
        v = self.d_vars
        if len(sel) != 1:
            self.d_name.set("Select a torrent to see its details" if not sel else "%d torrents selected" % len(sel))
            for var in v.values():
                var.set("-")
            return
        s = self._snap_by_hash[sel[0]]
        self.d_name.set(s["name"])
        v["save"].set(s["save_dir"])
        v["size"].set(fmt_size(s["size"]) if s["size"] else "-")
        v["down"].set(fmt_size(s["downloaded"]))
        v["up"].set(fmt_size(s["uploaded"]))
        v["ratio"].set("%.2f" % (s["uploaded"] / s["downloaded"]) if s["downloaded"] else "-")
        v["peers"].set(str(s["peers"]))
        v["hash"].set(s["hash"][:16] + "\u2026")

    def _pf_summary(self):
        """(short text, palette colour key, full text) describing router port forwarding."""
        pm = self.mgr.portmap
        if pm is None:
            return "Port forwarding off", "muted", "Automatic port forwarding is turned off (Settings > Connection)."
        s4, full = pm.status4.lower(), pm.status
        if "forwarded" in s4 and "partly" not in s4:
            txt = "Ports forwarded (%s)" % (pm.protocol or "router")
            return txt, ("warn" if "not public" in s4 else "good"), full
        if "searching" in s4:
            return "Looking for router\u2026", "warn", full
        if "partly" in s4:
            return "Ports partly forwarded", "warn", full
        return "Port forwarding unavailable", "bad", full

    def _update_statusbar(self, snap):
        down, up = sum(s["down"] for s in snap), sum(s["up"] for s in snap)
        self.s_speed.configure(text="\u2193 %s      \u2191 %s" % (rate0(down), rate0(up)))
        if self.tray.visible:
            self.tray.set_tip("%s\n\u2193 %s   \u2191 %s" % (APP, rate0(down), rate0(up)))
        counts = {}
        for s in snap:
            counts[s["raw_state"]] = counts.get(s["raw_state"], 0) + 1
        parts = ["%d torrent%s" % (len(snap), "" if len(snap) == 1 else "s")]
        for state, word in (("Downloading", "downloading"), ("Finished", "finished"), ("Seeding", "seeding"),
                            ("Queued", "queued"), ("Paused", "paused")):
            if counts.get(state):
                parts.append("%d %s" % (counts[state], word))
        self.s_count.configure(text="  \u00B7  ".join(parts))
        pm_text, key, self._pf_full = self._pf_summary()
        self.s_pf.configure(text=pm_text)
        self.s_dot.configure(fg=self.P[key])
        if not self.mgr.port:
            port = "no incoming connections"
        elif self.mgr.public_port != self.mgr.port:
            port = "port %d (local %d)" % (self.mgr.public_port, self.mgr.port)
        else:
            port = "port %d" % self.mgr.port
        dht = ("DHT %d nodes" % sum(len(d.nodes) for d in self.mgr.dhts)) if self.mgr.dhts else "DHT off"
        lim = ""
        ld, lu = self.mgr.down.rate // 1024, self.mgr.up.rate // 1024
        if ld or lu:
            lim = "  \u00B7  limits \u2193 %s \u2191 %s" % (("%d KiB/s" % ld) if ld else "-", ("%d KiB/s" % lu) if lu else "-")
        self.s_net.configure(text="%s  \u00B7  %s%s" % (dht, port, lim))

    def close(self):
        """The window's close button: hide to the system tray (downloads keep running) or quit, per Settings."""
        ui.Tooltip.hide_all()
        if self.cfg.get("tray", True) and tray.available():
            balloon = None
            if not self._tray_told:
                balloon = (APP, "Still running in the background. Click the icon to open it, right-click to exit.")
            if self.tray.show(balloon):
                self._tray_told = True
                self.save()
                self.root.withdraw()
                return
        self.quit()

    @staticmethod
    def read_shortcut(path):
        """The address inside an internet shortcut (.url) file, or ''."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f.read(20000).splitlines():
                    if line.strip().lower().startswith("url="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def on_drop(self, paths):
        """Files dropped on the window: add the .torrent ones; a dropped link (.url shortcut) is treated like a pasted one."""
        try:
            torrents = [p for p in paths if p.lower().endswith(".torrent") and os.path.isfile(p)]
            links = [self.read_shortcut(p) for p in paths if p.lower().endswith(".url") and os.path.isfile(p)]
            links = [u for u in links if u]
            if torrents:
                self.show_window()
                self.add_torrents(torrents)
            elif links:
                self.show_window()
                self.handle_link(links[0])
            elif paths:
                messagebox.showinfo(APP, "Drop .torrent files here. For a magnet link or a website address, copy it and "
                                         "press Ctrl+V.", parent=self.root)
        except Exception as e:                       # never let a drop take the app down
            self.log("Could not handle the dropped item: %s" % e)

    def handle_link(self, text):
        """A magnet link adds a torrent; a web address looks for RSS feeds on that site."""
        text = text.strip()
        if text.lower().startswith("magnet:"):
            self.add_magnet_dialog(text)
        elif text.lower().startswith(("http://", "https://")) and "\n" not in text:
            self.find_feeds(text)

    def find_feeds(self, initial=""):
        """Find feeds -> choose one -> fill in the add-feed form -> subscribe (also used by the RSS window)."""
        picked = FindFeedsDialog(self, initial).show()
        if not picked:
            return False
        res = FeedEditDialog(self, rss.new_feed(picked["url"], picked["title"]), new=True).show()
        if not res:
            return False
        self.cfg.setdefault("rss", []).append(res)
        self.save()
        self.check_feed(len(self.cfg["rss"]) - 1, manual=True)
        return True

    def start_hidden(self):
        """Launched at sign-in: go straight to the tray (or just show the window if there is no tray)."""
        if self.cfg.get("tray", True) and tray.available() and self.tray.show():
            self.root.withdraw()

    def show_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self.tray.hide()

    def _confirm_exit(self, what):
        busy = [s for s in self.mgr.snapshot() if s["raw_state"] in ("Downloading", "Checking", "Fetching metadata")]
        if not busy:
            return True
        return messagebox.askyesno(APP, "%d torrent(s) are still downloading.\n\n%s anyway? They resume where they left "
                                        "off next time." % (len(busy), what), parent=self.root)

    def exit_app(self):
        """Really quit (the window's close button only hides the app to the tray)."""
        if self._confirm_exit("Exit"):
            self.quit()

    def restart(self):
        """Quit completely, then start a fresh copy (see main())."""
        if self._confirm_exit("Restart"):
            self.restart_requested = True
            self.quit()

    def quit(self):
        if getattr(self, "_panel", None):
            self._panel.close()
        if getattr(self, "_streamer", None):
            self._streamer.stop()
        self.save()
        self.tray.destroy()
        self.root.withdraw()
        self.mgr.shutdown()
        self.save_resume()          # after the sessions stopped: the files are closed and their timestamps final
        if getattr(self, "_update_ready", None) and not getattr(self, "restart_requested", False):
            self._install_update(relaunch=False)                  # "install when I next exit"
        self.root.destroy()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--player":       # the built-in player runs as its own small process
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        import player
        player.main(sys.argv[2:])
        return
    if any(a.lower() == "--quit" for a in sys.argv[1:]):
        # Ask a running copy to save and exit (used by the installer and uninstaller), then stop; never opens a window.
        # Closing the main window only hides it to the tray, so Windows' own "close the app" request is not enough.
        if single_instance.acquire(["--quit"], lambda a: None):
            single_instance.release()                       # nothing was running
        return
    args = [a for a in sys.argv[1:]
            if a.lower().startswith("magnet:") or (a.lower().endswith(".torrent") and os.path.isfile(a))]
    hidden = any(a.lower() in ("--minimized", "--tray") for a in sys.argv[1:])   # started with Windows
    inbox = queue.Queue()
    if not single_instance.acquire(args, inbox.put):
        return  # handed over to the already-running window
    if args:
        inbox.put(args)
    try:  # crisp text on high-DPI Windows 11 displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = App(root, inbox)
    if hidden:
        app.start_hidden()
    root.mainloop()
    if getattr(app, "restart_requested", False):      # everything is shut down: launch a fresh copy
        single_instance.release()
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(sys.argv[0])]
        try:
            subprocess.Popen(cmd, close_fds=True, creationflags=0x00000008 | 0x00000200 if os.name == "nt" else 0)
        except OSError:
            pass


if __name__ == "__main__":
    main()
