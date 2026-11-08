"""Modal dialogs: Add torrent / Add magnet link, and Settings."""
import os
import shutil
import tkinter as tk
from tkinter import filedialog, ttk

import autostart
import queue
import rss
import schedule
import threading
import time
import firewall
import random
from torrent_core import parse_magnet
import ui
import chrome
from ui import center_over, fmt_size

ENC_CHOICES = {"Encrypt the handshake only (fastest)": "light", "Prefer full encryption (slower)": "prefer",
               "Require full encryption": "require", "Disabled": "off"}
UTP_CHOICES = {"TCP first, uTP as fallback": "fallback", "Prefer uTP": "prefer", "TCP only (uTP off)": "off"}
SEED_ACTIONS = {"Pause the torrent": "pause", "Remove it from the list (keep files)": "remove"}
PLAYER_CHOICES = {"Built-in player": "builtin", "Other player (VLC, mpv, system default)": "external"}
WHERE_CHOICES = {"Separate window": "window", "Inside the main window (Player tab)": "tab"}
WHERE_INV = {v: k for k, v in WHERE_CHOICES.items()}
PLAYER_CHOICES_INV = {v: k for k, v in PLAYER_CHOICES.items()}
THEME_CHOICES = {"Aurora (dark)": "dark", "Light": "light", "Match Windows": "system"}


def _inverse(d):
    return {v: k for k, v in d.items()}


class _Dialog(tk.Toplevel):
    """Base: themed, modal, centred over the main window, Esc closes."""

    def __init__(self, app, title, strip=True):
        super().__init__(app.root)
        self.app = app
        self.px = app.px                 # sizes below are for 96 dpi; px() scales them on high-DPI screens
        self.result = None
        self.chrome = None
        self.configure(bg=app.P["bg"])
        self.transient(app.root)
        if not strip:                    # a drop-down panel: no title strip, just a thin border
            try:
                self.overrideredirect(True)
                self.configure(highlightthickness=0)          # no border lines: it sits on the artwork
            except tk.TclError:
                pass
        elif chrome.enabled(app.cfg):
            self.chrome = chrome.attach(self, app.P, app.px, title, resizable=False, buttons=("close",),
                                        font=app.F.bold)
        self.title(title)
        self.resizable(False, False)
        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        if not self.chrome and strip:
            self.after(60, lambda: ui.set_glass(self, app.P) if self.winfo_exists() else None)

    def title(self, text=None):
        base = getattr(tk.Toplevel, "title", None)
        r = (base(self, text) if text is not None else base(self)) if base else None
        if text is not None and getattr(self, "chrome", None):
            self.chrome.set_title(text)
        return r

    def resizable(self, w=None, h=None):
        base = getattr(tk.Toplevel, "resizable", None)
        r = base(self, w, h) if base else None
        if w is not None and getattr(self, "chrome", None):
            self.chrome.set_resizable(bool(w or h))
        return r

    def _place(self):
        center_over(self, self.app.root)

    def _see_through(self):
        """The dialog's background colour is made see-through, and the main window's aurora is drawn right behind
        it in a borderless backdrop window - so the artwork shows around the options, but not the main window's
        own text. Text, tick boxes and input fields stay solid."""
        if os.name != "nt" or getattr(self, "_bd", None) is not None:
            return
        try:
            bd = self._bd = tk.Toplevel(self.app.root)
            bd.overrideredirect(True)
            bd.attributes("-topmost", True)
            c = bd.canvas = tk.Canvas(bd, bd=0, highlightthickness=0, bg=self.app.P["bg"])
            c.pack(fill="both", expand=True)
            c.bind("<MouseWheel>", lambda e: getattr(self, "_wheel", lambda e: None)(e))   # wheel over the gaps
            self.attributes("-transparentcolor", self.app.P["bg"])
            self.bind("<Configure>", lambda e: self._follow() if e.widget is self else None, add="+")
            self.bind("<Destroy>", lambda e: bd.destroy() if e.widget is self and bd.winfo_exists() else None, add="+")
        except tk.TclError:
            self._bd = None

    def _follow(self, stack=False):
        """Keep the backdrop exactly under the dialog, showing the artwork's slice for that spot. Only acts when the
        dialog really moved or changed size (raising windows from <Configure> would fire it again, for ever)."""
        bd = getattr(self, "_bd", None)
        try:
            if bd is None or not bd.winfo_exists():
                return
            geo = "%dx%d+%d+%d" % (self.winfo_width(), self.winfo_height(), self.winfo_rootx(), self.winfo_rooty())
            if geo != getattr(self, "_bd_geo", None):
                self._bd_geo = geo
                bd.geometry(geo)
                bd.canvas.delete("all")
                bd.canvas.configure(width=self.winfo_width(), height=self.winfo_height())
                self.after_idle(self._paint_backdrop)
            if stack:
                bd.lift()
                self.lift()
        except (tk.TclError, AttributeError):
            pass

    def _paint_backdrop(self):
        bd = getattr(self, "_bd", None)
        try:
            if bd is not None and bd.winfo_exists():
                bd.canvas.delete("all")
                self.app._art_at(bd.canvas, self.app.root)
        except (tk.TclError, AttributeError):
            pass

    def show(self):
        self._place()
        self._see_through()
        prev = None
        try:
            prev = self.app.root.grab_current()
        except tk.TclError:
            pass
        # A frameless window is not stacked by Windows like a normal owned dialog: it can open *behind* another window
        # (the Search window, say) while holding the input grab, which looks like a frozen app. Force it to the front.
        try:
            self.update_idletasks()
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)       # stays above the main window for as long as the dialog is open
            self.update()
            self._follow(stack=True)                # see-through backdrop under it, then the dialog on top
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()
        self.wait_window(self)
        try:
            if prev is not None and prev.winfo_exists():
                prev.grab_set()
                prev.focus_force()
            else:                    # give the keyboard back to the main window (F11, Ctrl+V ... else go nowhere)
                self.app.root.focus_force()
        except tk.TclError:
            pass
        return self.result


class MessageDialog(_Dialog):
    """Themed replacement for tkinter.messagebox. kind: info / warning / error / question; buttons: list of
    (label, value); the first is the default (Enter), `cancel` is what Esc / the close button returns."""

    GLYPH = {"info": ("\uE946", "accent"), "warning": ("\uE7BA", "warn"), "error": ("\uEA39", "bad"),
             "question": ("\uE897", "accent")}

    def __init__(self, app, title, message, kind="info", buttons=(("OK", True),), cancel=None, parent=None):
        super().__init__(app, title)
        self.cancel_value = cancel
        self.result = cancel
        self.transient(parent or app.root)
        P = app.P
        body = ttk.Frame(self, padding=(24, 22, 24, 8))
        body.pack(fill="both", expand=True)
        glyph, colour = self.GLYPH.get(kind, self.GLYPH["info"])
        if app.F.has_icons:
            tk.Label(body, text=glyph, font=app.F.icon, bg=P["bg"], fg=P[colour]).pack(side="left", anchor="n",
                                                                                      padx=(0, 14))
        ttk.Label(body, text=message, wraplength=app.px(420), justify="left").pack(side="left", anchor="n")
        row = ttk.Frame(self, padding=(24, 14, 24, 18))
        row.pack(fill="x")
        for i, (label, value) in reversed(list(enumerate(buttons))):
            b = ttk.Button(row, text=label, style="Accent.TButton" if i == 0 else "TButton",
                           command=lambda v=value: self._pick(v))
            b.pack(side="right", padx=(8, 0))
            if i == 0:
                first = b
        self.bind("<Return>", lambda e: self._pick(buttons[0][1]))
        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(cancel))
        self.bind("<Escape>", lambda e: self._pick(cancel))
        first.focus_set()

    def _pick(self, v):
        self.result = v
        self.destroy()


def install_messageboxes(app):
    """Route tkinter.messagebox through MessageDialog so no native grey boxes appear."""
    from tkinter import messagebox as mb
    import types
    if not isinstance(mb.showinfo, types.FunctionType):      # test doubles stay in charge
        return

    def make(kind, buttons, cancel):
        def f(title=None, message=None, **kw):
            parent = kw.get("parent")
            try:
                return MessageDialog(app, title or "", message or "", kind, buttons, cancel, parent).show()
            except tk.TclError:
                return cancel
        return f
    mb.showinfo = make("info", (("OK", True),), True)
    mb.showwarning = make("warning", (("OK", True),), True)
    mb.showerror = make("error", (("OK", True),), True)
    mb.askyesno = make("question", (("Yes", True), ("No", False)), False)
    mb.askyesnocancel = make("question", (("Yes", True), ("No", False), ("Cancel", None)), None)


CHECK_ON, CHECK_OFF, CHECK_PART = "\u2611", "\u2610", "\u25A3"


class FilesDialog(_Dialog):
    """Tick the files of a torrent to download. `skip` = indices currently unticked; result = the new skip set."""

    def __init__(self, app, torrent, skip=(), progress=None, title="Choose files"):
        super().__init__(app, title)
        self.resizable(True, True)
        self.t = torrent
        self.progress = progress
        self.nodes = {}                  # iid -> dict(name, parent, kids, files(indices), size)
        self.on = {i for i in range(len(torrent.files)) if i not in set(skip)}
        self._build_model()
        body = ttk.Frame(self, padding=(24, 20, 24, 20))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=torrent.name, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True, pady=(14, 0))
        cols = ("size", "done") if progress else ("size",)
        self.tree = ttk.Treeview(frame, columns=cols, show="tree headings", style="Dialog.Treeview", height=14,
                                 selectmode="browse")
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.column("#0", width=430, anchor="w", stretch=True)
        self.tree.heading("size", text="Size", anchor="w")
        self.tree.column("size", width=self.px(90), anchor="w", stretch=False)
        if progress:
            self.tree.heading("done", text="Done", anchor="w")
            self.tree.column("done", width=self.px(70), anchor="w", stretch=False)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._insert("", self.root_ids)
        self.tree.bind("<ButtonRelease-1>", self._click)
        self.tree.bind("<space>", lambda e: self._toggle_selected())
        ttk.Label(body, text="Click a row or press Space to tick or untick it. Unticked files are not downloaded.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self.summary = ttk.Label(body, text="", style="Muted.TLabel")
        self.summary.pack(anchor="w", pady=(4, 0))
        self.err = ttk.Label(body, text="", style="Bad.TLabel")
        self.err.pack(anchor="w")
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Select all", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(btns, text="Select none", command=lambda: self.set_all(False)).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 8))
        self._refresh_all()

    # -- model ---------------------------------------------------------------
    def _build_model(self):
        t = self.t
        self.root_ids = []
        for i, (parts, ln, _off) in enumerate(t.files):
            path = parts[1:] if t.multi else parts
            parent = ""
            for depth, name in enumerate(path):
                last = depth == len(path) - 1
                iid = ("f%d" % i) if last else ("d:" + parent + "/" + name)
                if iid not in self.nodes:
                    self.nodes[iid] = dict(name=name, parent=parent, kids=[], files=[], size=0, idx=i if last else None)
                    (self.nodes[parent]["kids"] if parent else self.root_ids).append(iid)
                node = self.nodes[iid]
                node["files"].append(i)
                node["size"] += ln
                parent = iid

    def _insert(self, parent, ids):
        for iid in ids:
            n = self.nodes[iid]
            vals = (fmt_size(n["size"]),)
            if self.progress:
                vals += (self._done_text(n),)
            self.tree.insert(parent, "end", iid=iid, text=self._label(iid), values=vals, open=True, **self._mark(iid))
            self._insert(iid, n["kids"])

    def _done_text(self, n):
        if not self.progress or not n["size"]:
            return ""
        got = sum(self.progress[i] * self.t.files[i][1] for i in n["files"])
        return "%d%%" % int(100 * got / n["size"])

    def state(self, iid):
        """'on' / 'off' / 'part' for a file or folder."""
        f = self.nodes[iid]["files"]
        n = sum(1 for i in f if i in self.on)
        return "off" if n == 0 else "on" if n == len(f) else "part"

    def _label(self, iid):
        if self._mark(iid):
            return "  " + self.nodes[iid]["name"]
        glyph = {"on": CHECK_ON, "off": CHECK_OFF, "part": CHECK_PART}[self.state(iid)]
        return "%s  %s" % (glyph, self.nodes[iid]["name"])

    def _mark(self, iid):
        """The round tick box (green when ticked) as the row's image; {} falls back to the text glyphs."""
        img = ui.check_images().get(self.state(iid) + "_tight")
        return {"image": img} if img is not None else {}

    def _refresh_all(self):
        for iid in self.nodes:
            self.tree.item(iid, text=self._label(iid), **self._mark(iid))
        chosen = sorted(self.on)
        size = sum(self.t.files[i][1] for i in chosen)
        self.summary.configure(text="%d of %d files selected  \u00B7  %s of %s" % (
            len(chosen), len(self.t.files), fmt_size(size), fmt_size(self.t.length)))

    def toggle(self, iid):
        """Tick a row, or untick it when it is fully ticked (a folder acts on everything inside it)."""
        if iid not in self.nodes:
            return
        files = self.nodes[iid]["files"]
        if self.state(iid) == "on":
            self.on.difference_update(files)
        else:
            self.on.update(files)
        self._refresh_all()

    def set_all(self, on):
        self.on = set(range(len(self.t.files))) if on else set()
        self._refresh_all()

    def skip_set(self):
        return {i for i in range(len(self.t.files)) if i not in self.on}

    def _click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.toggle(iid)

    def _toggle_selected(self):
        for iid in self.tree.selection():
            self.toggle(iid)

    def _ok(self):
        if not self.on:
            self.err.configure(text="Tick at least one file.")
            return
        self.result = self.skip_set()
        self.destroy()


class AddDialog(_Dialog):
    """kind='torrent': `items` is a list of (path, raw bytes, Torrent). kind='magnet': a link to paste."""

    def __init__(self, app, kind, items=None, initial=""):
        super().__init__(app, "Add torrent" if kind == "torrent" else "Add magnet link")
        self.kind, self.items = kind, items or []
        body = ttk.Frame(self, padding=(24, 20, 24, 20))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="Add torrent" if kind == "torrent" else "Add magnet link",
                  style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        r = 1
        if kind == "torrent":
            tv = ttk.Treeview(body, columns=("name", "size", "files"), show="headings", style="Dialog.Treeview",
                              height=min(max(len(self.items), 1), 6), selectmode="none")
            for col, text, w, anchor in (("name", "Name", 380, "w"), ("size", "Size", 90, "e"), ("files", "Files", 60, "e")):
                tv.heading(col, text=text, anchor=anchor)
                tv.column(col, width=self.px(w), anchor=anchor, stretch=(col == "name"))
            for _path, _raw, t in self.items:
                tv.insert("", "end", values=(t.name, fmt_size(t.length), len(t.files)))
            tv.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(16, 0))
            self.need = sum(t.length for _p, _r, t in self.items)
            self.skip = set()
            self.files_info = ttk.Label(body, text="", style="Muted.TLabel")
            if len(self.items) == 1 and len(self.items[0][2].files) > 1:
                r += 1
                row = ttk.Frame(body)
                row.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 0))
                ttk.Button(row, text="Choose files\u2026", command=self._choose_files).pack(side="left")
                self.files_info = ttk.Label(row, text="All files will be downloaded", style="Muted.TLabel")
                self.files_info.pack(side="left", padx=(12, 0))
        else:
            ttk.Label(body, text="Paste a magnet link", style="Muted.TLabel").grid(row=r, column=0, columnspan=3,
                                                                                    sticky="w", pady=(16, 4))
            self.link = tk.StringVar(value=initial)
            entry = ttk.Entry(body, textvariable=self.link, width=70)
            entry.grid(row=r + 1, column=0, columnspan=3, sticky="ew")
            entry.focus_set()
            r += 1
            self.need = 0
        r += 1
        cats = app.cfg.get("categories", {})
        self.category = tk.StringVar(value="(none)")
        if cats:
            crow = ttk.Frame(body)
            crow.grid(row=r, column=0, columnspan=3, sticky="w", pady=(16, 0))
            ttk.Label(crow, text="Category", style="Muted.TLabel").pack(side="left", padx=(0, 10))
            cb = ttk.Combobox(crow, values=["(none)"] + sorted(cats, key=str.lower), textvariable=self.category,
                              state="readonly", width=24)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: self._category_picked())
            r += 1
        ttk.Label(body, text="Save to", style="Muted.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(16, 4))
        self.folder = tk.StringVar(value=app.cfg["download_dir"])
        ttk.Entry(body, textvariable=self.folder).grid(row=r + 1, column=0, columnspan=2, sticky="ew")
        ttk.Button(body, text="Browse…", command=self._browse).grid(row=r + 1, column=2, padx=(8, 0))
        self.space = ttk.Label(body, text="", style="Muted.TLabel")
        self.space.grid(row=r + 2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.start = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Start downloading immediately", variable=self.start).grid(
            row=r + 3, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self.err = ttk.Label(body, text="", style="Bad.TLabel", wraplength=self.px(520), justify="left")
        self.err.grid(row=r + 4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        btns = ttk.Frame(body)
        btns.grid(row=r + 5, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Add", style="Accent.TButton", command=self._ok).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.folder.trace_add("write", lambda *_: self._update_space())
        self._update_space()

    def _choose_files(self):
        t = self.items[0][2]
        res = FilesDialog(self.app, t, self.skip).show()
        if res is None:
            return
        self.skip = res
        picked = len(t.files) - len(res)
        self.need = sum(f[1] for i, f in enumerate(t.files) if i not in res)
        self.files_info.configure(text="All files will be downloaded" if not res else
                                  "%d of %d files selected (%s)" % (picked, len(t.files), fmt_size(self.need)))
        self._update_space()

    def _category_picked(self):
        folder = self.app.cfg.get("categories", {}).get(self.category.get())
        self.folder.set(folder or self.app.cfg["download_dir"])

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="Save downloads to", initialdir=self.folder.get() or None)
        if d:
            self.folder.set(d)

    def _update_space(self):
        p = self.folder.get().strip() or "."
        while p and not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        try:
            free = shutil.disk_usage(p or ".").free
        except OSError:
            self.space.configure(text="", style="Muted.TLabel")
            return
        txt = "Free space: %s" % fmt_size(free)
        if self.need:
            txt += "   |   Needs: %s" % fmt_size(self.need)
        self.space.configure(text=txt, style="Bad.TLabel" if self.need > free else "Muted.TLabel")

    def _fail(self, msg):
        self.err.configure(text=msg)

    def _ok(self):
        folder = self.folder.get().strip()
        if not folder:
            return self._fail("Choose a folder to save to.")
        if self.kind == "magnet":
            try:
                parse_magnet(self.link.get())
            except ValueError as e:
                return self._fail("That is not a valid magnet link: %s" % e)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            return self._fail("Cannot use that folder: %s" % e)
        self.result = {"folder": folder, "start": bool(self.start.get()),
                       "category": "" if self.category.get() == "(none)" else self.category.get(),
                       "skip": sorted(getattr(self, "skip", ())),
                       "link": self.link.get().strip() if self.kind == "magnet" else None}
        self.destroy()


class SettingsDialog(_Dialog):
    """Settings drop down from the gear button: at first only the list of sections (with Save / Cancel). Clicking a
    section opens its page beside the list, the panel only as big as that page needs; clicking it again folds it."""

    def __init__(self, app):
        super().__init__(app, "Settings", strip=False)
        cfg, mgr = app.cfg, app.mgr
        P = app.P
        outer = ttk.Frame(self, padding=(10, 10, 10, 10))
        outer.pack(fill="both", expand=True)
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        side = tk.Frame(body, bg=P["bg"], bd=0, highlightthickness=0)
        side.pack(side="left", fill="y")
        nav = tk.Frame(side, bg=P["bg"], bd=0, highlightthickness=0)
        nav.pack(side="top", fill="x")
        self._sep = tk.Frame(body, bg=P["border"], width=1, bd=0, highlightthickness=0)
        # the open page sits in a canvas, so a page taller than the room below the gear scrolls (mouse wheel)
        self._content = tk.Frame(body, bg=P["bg"], bd=0, highlightthickness=0)
        self._cv = tk.Canvas(self._content, bg=P["bg"], bd=0, highlightthickness=0, width=1, height=1)
        self._sb = ttk.Scrollbar(self._content, orient="vertical", command=self._cv.yview)
        self._cv.configure(yscrollcommand=self._sb.set)
        self._cv.pack(side="left", fill="both", expand=True)
        content = self._inner = ttk.Frame(self._cv)
        self._cv.create_window(0, 0, window=content, anchor="nw")
        content.columnconfigure(0, weight=1)
        self.bind("<MouseWheel>", self._wheel, add="+")

        self.pages, self.nav_rows, self._chev = {}, {}, {}
        self.current_page = None

        def tab(title):
            """A settings page: listed in the column; shown beside it (cascading) when its name is clicked."""
            f = ttk.Frame(content, padding=(18, 4, 4, 10))
            f.columnconfigure(1, weight=1)
            row = tk.Frame(nav, bd=0, highlightthickness=0, cursor="hand2")
            name = tk.Label(row, text=title, anchor="w", font=app.F.ui, bd=0, padx=14, pady=8, cursor="hand2")
            chev = tk.Label(row, text="\u203A", font=app.F.ui, bd=0, padx=10, cursor="hand2")
            name.pack(side="left", fill="x", expand=True)
            chev.pack(side="right")
            row.pack(fill="x", pady=1)
            for w in (row, name, chev):
                w.bind("<Button-1>", lambda e, t=title: self.show_page(t))
                w.bind("<Enter>", lambda e, t=title: self._paint_nav(hover=t))
                w.bind("<Leave>", lambda e: self._paint_nav())
            self.pages[title] = f
            self.nav_rows[title] = name
            self._chev[title] = (row, chev)
            return f

        # -- General
        g = tab("General")
        box = ttk.LabelFrame(g, text="Appearance", padding=14)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        self.theme = tk.StringVar(value=_inverse(THEME_CHOICES)[cfg["theme"]])
        ttk.Label(box, text="Theme").grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Combobox(box, values=list(THEME_CHOICES), textvariable=self.theme, state="readonly",
                     width=30).grid(row=0, column=1, sticky="w")
        box2 = ttk.LabelFrame(g, text="Downloads", padding=14)
        box2.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        box2.columnconfigure(0, weight=1)
        ttk.Label(box2, text="Default save folder", style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.folder = tk.StringVar(value=cfg["download_dir"])
        ttk.Entry(box2, textvariable=self.folder, width=48).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(box2, text="Browse…", command=self._browse).grid(row=1, column=1, padx=(8, 0), pady=(4, 0))

        self.tray = tk.BooleanVar(value=cfg.get("tray", True))
        ttk.Checkbutton(box2, text="Closing the window keeps running in the system tray", variable=self.tray).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.notify = tk.BooleanVar(value=cfg.get("notify", True))
        ttk.Checkbutton(box2, text="Show a notification when a download finishes", variable=self.notify).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.notify_sound = tk.BooleanVar(value=cfg.get("notify_sound", True))
        ttk.Checkbutton(box2, text="Play a sound too", variable=self.notify_sound).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        box3 = ttk.LabelFrame(g, text="Windows integration", padding=14)
        box3.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        if autostart.available():
            self.autostart = tk.BooleanVar(value=autostart.is_enabled())
            ttk.Checkbutton(box3, text="Start Bit Torrent Client when I sign in to Windows (minimised to the tray)",
                            variable=self.autostart).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.native_frame = tk.BooleanVar(value=bool(cfg.get("native_frame", False)))
        ttk.Checkbutton(box3, text="Use the standard Windows title bars instead of the Aurora ones (restart needed)",
                        variable=self.native_frame).grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Button(box3, text="Use Bit Torrent Client for magnet links", command=app.register_magnet).grid(row=0, column=0, sticky="w")
        ttk.Label(box3, text="Clicking a magnet link in your browser will then open it in Bit Torrent Client.",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))

        # -- Media (playing while downloading)
        md = tab("Media")
        import mpvlib
        box5 = ttk.LabelFrame(md, text="Player", padding=14)
        box5.grid(row=0, column=0, sticky="ew")
        box5.columnconfigure(1, weight=1)
        self.player_mode = tk.StringVar(value=PLAYER_CHOICES_INV.get(cfg.get("player", "builtin"), "Built-in player"))
        ttk.Label(box5, text="Play with").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Combobox(box5, values=list(PLAYER_CHOICES), textvariable=self.player_mode, state="readonly",
                     width=30).grid(row=0, column=1, sticky="w")
        self.player_where = tk.StringVar(value=WHERE_INV.get(cfg.get("player_where", "window"), "Separate window"))
        ttk.Label(box5, text="Built-in player shows in").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(8, 0))
        ttk.Combobox(box5, values=list(WHERE_CHOICES), textvariable=self.player_where, state="readonly",
                     width=30).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(box5, text="libmpv file").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=(8, 0))
        self.libmpv = tk.StringVar(value=cfg.get("libmpv", ""))
        ttk.Entry(box5, textvariable=self.libmpv, width=34).grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(box5, text="Browse\u2026", command=self._browse_lib).grid(row=2, column=2, padx=(8, 0), pady=(8, 0))
        ttk.Label(box5, text="Player program").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=(8, 0))
        self.player_exe = tk.StringVar(value=cfg.get("player_exe", ""))
        ttk.Entry(box5, textvariable=self.player_exe, width=34).grid(row=3, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(box5, text="Browse\u2026", command=self._browse_exe).grid(row=3, column=2, padx=(8, 0), pady=(8, 0))
        found = mpvlib.find_library(cfg.get("libmpv", ""))
        self.lib_note = ttk.Label(box5, style="Muted.TLabel", wraplength=self.px(520))
        self.lib_note.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._lib_text(found)
        ttk.Button(box5, text="Get libmpv\u2026", command=self._get_libmpv).grid(row=4, column=2, padx=(8, 0), pady=(8, 0))
        self.hwdec = tk.BooleanVar(value=cfg.get("hwdec", True))
        ttk.Checkbutton(box5, text="Hardware video decoding (recommended for 4K / HEVC)", variable=self.hwdec).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.skip_extras = tk.BooleanVar(value=cfg.get("skip_extras", True))
        ttk.Checkbutton(box5, text="When playing, don't download samples and extras (.nfo, .jpg, .txt \u2026)",
                        variable=self.skip_extras).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.thumbs = tk.BooleanVar(value=cfg.get("thumbs", True))
        ttk.Checkbutton(box5, text="Preview pictures when hovering the seek bar", variable=self.thumbs).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.lan_hours = tk.StringVar(value=str(cfg.get("lan_hours", 8)))
        ttk.Label(box5, text="Network links expire after (hours)").grid(row=9, column=0, sticky="w", padx=(0, 12), pady=(8, 0))
        ttk.Entry(box5, textvariable=self.lan_hours, width=6).grid(row=9, column=1, sticky="w", pady=(8, 0))
        self.lan_share = tk.BooleanVar(value=cfg.get("lan_share", False))
        ttk.Checkbutton(box5, text="Let other devices on my network play streams (\"Copy link for TV / phone\")",
                        variable=self.lan_share).grid(row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))
        box6 = ttk.LabelFrame(md, text="Languages and subtitles", padding=14)
        box6.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        box6.columnconfigure(1, weight=1)
        self.alang = tk.StringVar(value=cfg.get("alang", ""))
        self.slang = tk.StringVar(value=cfg.get("slang", ""))
        self.os_lang = tk.StringVar(value=cfg.get("os_lang", "en"))
        self.os_key = tk.StringVar(value=cfg.get("os_key", ""))
        self.os_user = tk.StringVar(value=cfg.get("os_user", ""))
        self.os_pass = tk.StringVar(value=cfg.get("os_pass", ""))
        for r, (label, var, show) in enumerate((("Preferred audio language (e.g. eng,jpn)", self.alang, ""),
                                                ("Preferred subtitle language (e.g. eng)", self.slang, ""),
                                                ("OpenSubtitles language", self.os_lang, ""),
                                                ("OpenSubtitles API key", self.os_key, ""),
                                                ("OpenSubtitles username", self.os_user, ""),
                                                ("OpenSubtitles password", self.os_pass, "*"))):
            ttk.Label(box6, text=label).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=(0 if r == 0 else 6, 0))
            ttk.Entry(box6, textvariable=var, width=30, show=show).grid(row=r, column=1, sticky="w", pady=(0 if r == 0 else 6, 0))
        ttk.Label(box6, text="Subtitle download needs a free API key and account from opensubtitles.com. The password is "
                  "stored in your settings file on this computer.", style="Muted.TLabel", wraplength=self.px(520)).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        box7 = ttk.LabelFrame(md, text="Library", padding=14)
        box7.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        box7.columnconfigure(1, weight=1)
        self.tmdb_key = tk.StringVar(value=cfg.get("tmdb_key", ""))
        ttk.Label(box7, text="TMDB API key").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(box7, textvariable=self.tmdb_key, width=34).grid(row=0, column=1, sticky="w")
        ttk.Label(box7, text="Optional. A free key from themoviedb.org adds descriptions and posters to the Library "
                  "(posters use Windows' imaging, or libmpv). Without it the Library still lists what you can watch.",
                  style="Muted.TLabel", wraplength=self.px(520)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # -- Downloads (seeding rules, finished folder, categories)
        dl = tab("Downloads")
        box = ttk.LabelFrame(dl, text="Seeding", padding=14)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        self.seed_ratio = tk.StringVar(value=("%g" % cfg.get("seed_ratio", 0)))
        self.seed_minutes = tk.StringVar(value=str(cfg.get("seed_minutes", 0)))
        self.seed_action = tk.StringVar(value=_inverse(SEED_ACTIONS)[cfg.get("seed_action", "pause")])
        ttk.Label(box, text="Stop seeding at ratio").grid(row=0, column=0, sticky="w", padx=(0, 16), pady=4)
        ttk.Spinbox(box, from_=0, to=1000, increment=0.5, textvariable=self.seed_ratio, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(box, text="or after (minutes)").grid(row=1, column=0, sticky="w", padx=(0, 16), pady=4)
        ttk.Spinbox(box, from_=0, to=1_000_000, textvariable=self.seed_minutes, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(box, text="Then").grid(row=2, column=0, sticky="w", padx=(0, 16), pady=4)
        ttk.Combobox(box, values=list(SEED_ACTIONS), textvariable=self.seed_action, state="readonly",
                     width=28).grid(row=2, column=1, sticky="w")
        ttk.Label(box, text="0 = no limit. Whichever limit is reached first applies.", style="Muted.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        box = ttk.LabelFrame(dl, text="When a download finishes", padding=14)
        box.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text="Move finished downloads to (leave empty to keep them where they are)",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.done_dir = tk.StringVar(value=cfg.get("done_dir", ""))
        ttk.Entry(box, textvariable=self.done_dir, width=48).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(box, text="Browse\u2026", command=lambda: self._pick_dir(self.done_dir)).grid(row=1, column=1, padx=(8, 0), pady=(4, 0))
        box = ttk.LabelFrame(dl, text="Categories (each can have its own save folder)", padding=14)
        box.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        box.columnconfigure(0, weight=1)
        self.categories = dict(cfg.get("categories", {}))
        self.cat_tree = ttk.Treeview(box, columns=("name", "folder"), show="headings", height=4, style="Compact.Treeview",
                                     selectmode="browse")
        self.cat_tree.heading("name", text="Category")
        self.cat_tree.heading("folder", text="Save folder")
        self.cat_tree.column("name", width=self.px(140))
        self.cat_tree.column("folder", width=self.px(300))
        self.cat_tree.grid(row=0, column=0, rowspan=3, sticky="ew")
        ttk.Button(box, text="Add\u2026", command=self._cat_add).grid(row=0, column=1, padx=(8, 0), sticky="ew")
        ttk.Button(box, text="Folder\u2026", command=self._cat_folder).grid(row=1, column=1, padx=(8, 0), pady=3, sticky="ew")
        ttk.Button(box, text="Remove", command=self._cat_remove).grid(row=2, column=1, padx=(8, 0), sticky="ew")
        self._cat_fill()

        # -- Speed
        s = tab("Speed")
        box = ttk.LabelFrame(s, text="Global speed limits", padding=14)
        box.grid(row=0, column=0, sticky="ew")
        self.down = tk.StringVar(value=str(cfg["down_limit"]))
        self.up = tk.StringVar(value=str(cfg["up_limit"]))
        for i, (label, var) in enumerate((("Download limit", self.down), ("Upload limit", self.up))):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", padx=(0, 16), pady=5)
            ttk.Spinbox(box, from_=0, to=10_000_000, textvariable=var, width=10).grid(row=i, column=1, sticky="w")
            ttk.Label(box, text="KiB/s", style="Muted.TLabel").grid(row=i, column=2, sticky="w", padx=(8, 0))
        ttk.Label(box, text="0 means unlimited. Limits apply to all torrents together.",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        abox = ttk.LabelFrame(s, text="Alternative speed (switch it on from the status bar)", padding=14)
        abox.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        self.alt_down = tk.StringVar(value=str(cfg.get("alt_down", 0)))
        self.alt_up = tk.StringVar(value=str(cfg.get("alt_up", 0)))
        for i, (label, var) in enumerate((("Download", self.alt_down), ("Upload", self.alt_up))):
            ttk.Label(abox, text=label).grid(row=0, column=i * 3, sticky="w", padx=(0 if i == 0 else 20, 8))
            ttk.Spinbox(abox, from_=0, to=10_000_000, textvariable=var, width=8).grid(row=0, column=i * 3 + 1, sticky="w")
            ttk.Label(abox, text="KiB/s", style="Muted.TLabel").grid(row=0, column=i * 3 + 2, sticky="w", padx=(6, 0))
        self.sched_on = tk.BooleanVar(value=cfg.get("sched_on", False))
        ttk.Checkbutton(abox, text="Use these speeds automatically during the hours below",
                        variable=self.sched_on).grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        tf = ttk.Frame(abox)
        tf.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self.sched_start = tk.StringVar(value=cfg.get("sched_start", "08:00"))
        self.sched_end = tk.StringVar(value=cfg.get("sched_end", "23:00"))
        ttk.Label(tf, text="From").pack(side="left")
        ttk.Entry(tf, textvariable=self.sched_start, width=6).pack(side="left", padx=(6, 12))
        ttk.Label(tf, text="to").pack(side="left")
        ttk.Entry(tf, textvariable=self.sched_end, width=6).pack(side="left", padx=(6, 12))
        ttk.Label(tf, text="(24-hour, e.g. 22:00 to 07:00 runs overnight)", style="Muted.TLabel").pack(side="left")
        df = ttk.Frame(abox)
        df.grid(row=3, column=0, columnspan=6, sticky="w", pady=(6, 0))
        on_days = set(cfg.get("sched_days", range(7)))
        self.sched_days = []
        for i, name in enumerate(schedule.DAYS):
            v = tk.BooleanVar(value=i in on_days)
            self.sched_days.append(v)
            ttk.Checkbutton(df, text=name, variable=v).pack(side="left", padx=(0, 8))
        qbox = ttk.LabelFrame(s, text="Download queue", padding=14)
        qbox.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self.max_active = tk.StringVar(value=str(cfg.get("max_active", 0)))
        ttk.Label(qbox, text="Active downloads").grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Spinbox(qbox, from_=0, to=99, textvariable=self.max_active, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(qbox, text="0 means no limit. The rest wait in the queue (right-click a torrent to change its place). "
                             "Seeding torrents never wait.", style="Muted.TLabel", wraplength=self.px(440),
                  justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # -- Connection
        c = tab("Connection")
        box = ttk.LabelFrame(c, text="Peers", padding=14)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        self.enc = tk.StringVar(value=_inverse(ENC_CHOICES)[cfg["encryption"]])
        self.utp = tk.StringVar(value=_inverse(UTP_CHOICES)[cfg["utp"]])
        for i, (label, var, choices) in enumerate((("Encryption", self.enc, ENC_CHOICES),
                                                   ("Transport", self.utp, UTP_CHOICES))):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", padx=(0, 16), pady=5)
            ttk.Combobox(box, values=list(choices), textvariable=var, state="readonly",
                         width=34).grid(row=i, column=1, sticky="w")
        box = ttk.LabelFrame(c, text="Router", padding=14)
        box.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self.portmap = tk.BooleanVar(value=cfg["portmap"])
        ttk.Checkbutton(box, text="Automatic port forwarding (PCP / NAT-PMP / UPnP)",
                        variable=self.portmap).grid(row=0, column=0, sticky="w")
        pf = ttk.Frame(box)
        pf.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(pf, text="Listening port").pack(side="left")
        self.port = tk.StringVar(value=str(cfg.get("port") or mgr.port or ""))
        ttk.Entry(pf, textvariable=self.port, width=8).pack(side="left", padx=8)
        ttk.Button(pf, text="Random", command=lambda: self.port.set(str(random.randint(20000, 40000)))).pack(side="left")
        ttk.Label(pf, text="takes effect after restart", style="Muted.TLabel").pack(side="left", padx=8)
        status = mgr.portmap.status if mgr.portmap else "off"
        ttk.Label(box, text="Listening on port %s.   Status: %s" % (mgr.port or "-", status), style="Muted.TLabel",
                  wraplength=self.px(470), justify="left").grid(row=2, column=0, sticky="w", pady=(8, 0))
        bt = ttk.Frame(box)
        bt.grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Button(bt, text="Diagnostics\u2026", command=self._diagnostics).pack(side="left")
        ttk.Button(bt, text="Banned peers\u2026", command=self._banned).pack(side="left", padx=(8, 0))
        ttk.Label(c, text="Peer locations: IP geolocation by DB-IP.com (CC BY 4.0); flags from flag-icons (MIT). "
                          "A peer's country is where its address block is registered, so VPN users show up elsewhere.",
                  style="Muted.TLabel", wraplength=self.px(470), justify="left").grid(row=3, column=0, sticky="w", pady=(14, 0))
        if firewall.available():
            fw = ttk.LabelFrame(c, text="Windows Firewall", padding=14)
            fw.grid(row=2, column=0, sticky="ew", pady=(14, 0))
            fw.columnconfigure(0, weight=1)
            ttk.Label(fw, text=app.fw_text(), style="Muted.TLabel", wraplength=self.px(330), justify="left").grid(row=0, column=0, sticky="w")
            ttk.Button(fw, text="Allow in Windows Firewall", command=app.fix_firewall).grid(row=0, column=1, padx=(12, 0))

        # -- About: version, credits, data folder (the same content as the About window)
        ab = tab("About")
        about_content(ab, app, self.px).grid(row=0, column=0, sticky="nsew")
        # Updates live with the version they update
        box4 = ttk.LabelFrame(ab, text="Updates", padding=14)
        box4.grid(row=1, column=0, sticky="ew", pady=(18, 0))
        box4.columnconfigure(1, weight=1)
        self.update_check = tk.BooleanVar(value=cfg.get("update_check", True))
        ttk.Checkbutton(box4, text="Keep Bit Torrent Client up to date automatically",
                        variable=self.update_check).grid(row=0, column=0, sticky="w")
        ttk.Button(box4, text="Check now", command=self._check_update).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(box4, text="About once a day it looks for a new version and downloads it in the background, then asks "
                             "before installing (or installs it when you next exit).",
                  style="Muted.TLabel", wraplength=self.px(470), justify="left").grid(row=1, column=0, columnspan=3,
                                                                                       sticky="w", pady=(8, 0))

        # Save / Cancel under the section list
        btns = ttk.Frame(side)
        btns.pack(side="top", fill="x", pady=(14, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self._ok).pack(fill="x")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(fill="x", pady=(6, 0))
        self.bind("<Return>", lambda e: self._ok())
        self._paint_nav()

    def show_page(self, title):
        """Open `title`'s page beside the list (or fold it away when it is already open)."""
        if title == self.current_page:
            title = None
        for name, f in self.pages.items():
            if name == title:
                f.grid(row=0, column=0, sticky="nsew")
            else:
                f.grid_remove()
        if title is None:
            self._content.pack_forget()
        elif self.current_page is None:
            self._content.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.current_page = title
        self._paint_nav()
        self._fit()

    def _paint_nav(self, hover=None):
        """No boxes: the open section and the pointed-at one are bold white."""
        P = self.app.P
        for name, label in self.nav_rows.items():
            lit = name == self.current_page or name == hover
            row, chev = self._chev[name]
            for w in (row, label, chev):
                w.configure(bg=P["bg"])
            label.configure(fg=P["fg"], font=self.app.F.bold if lit else self.app.F.ui)
            chev.configure(fg=P["fg"] if lit else P["muted"])

    def _anchor(self):
        """Screen position just under the gear button (top-left of the panel)."""
        app = self.app
        try:
            b, bar = app.btn["settings"], app.toolbar
            return bar.winfo_rootx() + b.x0, bar.winfo_rooty() + b.y1 + 2
        except (AttributeError, KeyError, tk.TclError, TypeError):
            return None

    def _wheel(self, e):
        try:
            if self.current_page and self._sb.winfo_ismapped():
                self._cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
        except tk.TclError:
            pass

    def _fit(self):
        """Exactly as big as what is shown: the list alone, or the list plus the open page - but never past the
        bottom of the screen (then the page scrolls)."""
        try:
            self.update_idletasks()
            if self.current_page:
                pw, ph = self._inner.winfo_reqwidth(), self._inner.winfo_reqheight()
                at = self._anchor()
                top = at[1] if at else self.winfo_rooty()
                try:
                    wx, wy, ww, wh = ui.work_area(self)
                    room = wy + wh - top
                except Exception:
                    room = self.winfo_screenheight() - top
                room -= self.px(30)                                    # padding + border
                ch = max(self.px(200), min(ph, room))
                scroll = ph > ch
                self._cv.configure(width=pw, height=ch, scrollregion=(0, 0, pw, ph), yscrollincrement=self.px(30))
                self._cv.yview_moveto(0)
                if scroll:
                    self._sb.pack(side="right", fill="y")
                else:
                    self._sb.pack_forget()
                self.update_idletasks()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            at = self._anchor()
            if at is None:
                self.geometry("%dx%d" % (w, h))
                return
            x, y = at
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            x = max(0, min(x, sw - w))
            y = max(0, min(y, sh - h))
            self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except (tk.TclError, TypeError, ValueError):
            pass

    def _place(self):
        if self._anchor() is None:
            super()._place()
        self._fit()

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="Default save folder", initialdir=self.folder.get() or None)
        if d:
            self.folder.set(d)

    def _pick_dir(self, var):
        d = filedialog.askdirectory(parent=self, title="Choose a folder", initialdir=var.get() or None)
        if d:
            var.set(d)

    def _cat_fill(self):
        self.cat_tree.delete(*self.cat_tree.get_children())
        for name, folder in sorted(self.categories.items(), key=lambda kv: kv[0].lower()):
            self.cat_tree.insert("", "end", iid=name, values=(name, folder or "(default folder)"))

    def _cat_add(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("New category", "Category name:", parent=self)
        name = (name or "").strip()
        if not name:
            return
        d = filedialog.askdirectory(parent=self, title="Save folder for '%s' (cancel for the default folder)" % name)
        self.categories[name] = d or ""
        self._cat_fill()

    def _cat_folder(self):
        sel = self.cat_tree.selection()
        if sel:
            d = filedialog.askdirectory(parent=self, title="Save folder for '%s'" % sel[0])
            if d:
                self.categories[sel[0]] = d
                self._cat_fill()

    def _cat_remove(self):
        for name in self.cat_tree.selection():
            self.categories.pop(name, None)
        self._cat_fill()

    def _check_update(self):
        self.app.check_updates(manual=True, parent=self)

    def _banned(self):
        BannedDialog(self.app).show()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _lib_text(self, found):
        self.lib_note.configure(text=("Built-in player ready: " + found) if found else
                                "libmpv was not found. Use \"Get libmpv\u2026\" to download it (about 30 MB), or choose the file.")

    def _get_libmpv(self):
        self.app.get_libmpv(parent=self, done=lambda path: (self.libmpv.set(path) if path else None, self._lib_text(path)))

    def _browse_lib(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(parent=self, title="libmpv library",
                                       filetypes=[("libmpv", "*.dll *.so* *.dylib"), ("All files", "*.*")])
        if p:
            self.libmpv.set(p)

    def _browse_exe(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(parent=self, title="Player program (for \"Other player\")",
                                       filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if p:
            self.player_exe.set(p)

    def _diagnostics(self):
        DiagnosticsDialog(self.app).show()
        try:
            self.grab_set()      # this dialog is modal again once the diagnostics window closes
        except tk.TclError:
            pass

    def _ok(self):
        try:
            down, up = int(self.down.get()), int(self.up.get())
            maxa = int(self.max_active.get())
            if down < 0 or up < 0 or maxa < 0:
                raise ValueError
        except ValueError:
            from tkinter import messagebox
            messagebox.showerror("Settings", "Speed limits and the number of active downloads must be whole numbers (0 = unlimited).", parent=self)
            return
        try:
            seed_ratio = float(self.seed_ratio.get() or 0)
            seed_minutes = int(self.seed_minutes.get() or 0)
            if seed_ratio < 0 or seed_minutes < 0:
                raise ValueError
        except ValueError:
            from tkinter import messagebox
            messagebox.showerror("Settings", "The seeding limits must be numbers (0 = no limit).", parent=self)
            return
        try:
            port = int(self.port.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            from tkinter import messagebox
            messagebox.showerror("Settings", "The listening port must be a number from 1024 to 65535.", parent=self)
            return
        from tkinter import messagebox
        try:
            alt_down, alt_up = int(self.alt_down.get() or 0), int(self.alt_up.get() or 0)
            if alt_down < 0 or alt_up < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Settings", "The alternative speeds must be whole numbers (0 = unlimited).", parent=self)
            return
        if not (schedule.valid_hhmm(self.sched_start.get()) and schedule.valid_hhmm(self.sched_end.get())):
            messagebox.showerror("Settings", "Enter the schedule times as hours:minutes, for example 08:00 or 23:30.", parent=self)
            return
        self.result = dict(port=port,
                           alt_down=alt_down, alt_up=alt_up, sched_on=bool(self.sched_on.get()),
                           sched_start=self.sched_start.get().strip(), sched_end=self.sched_end.get().strip(),
                           sched_days=[i for i, v in enumerate(self.sched_days) if v.get()], tray=bool(self.tray.get()), notify=bool(self.notify.get()), notify_sound=bool(self.notify_sound.get()), native_frame=bool(self.native_frame.get()), max_active=maxa,
                           autostart=bool(self.autostart.get()) if hasattr(self, "autostart") else None, down_limit=down, up_limit=up, encryption=ENC_CHOICES[self.enc.get()],
                           utp=UTP_CHOICES[self.utp.get()],
                           portmap=bool(self.portmap.get()), theme=THEME_CHOICES[self.theme.get()],
                           download_dir=self.folder.get().strip() or self.app.cfg["download_dir"],
                           seed_ratio=seed_ratio, seed_minutes=seed_minutes,
                           seed_action=SEED_ACTIONS[self.seed_action.get()], done_dir=self.done_dir.get().strip(),
                           categories=dict(self.categories),
                           update_check=bool(self.update_check.get()),
                           player=PLAYER_CHOICES[self.player_mode.get()], libmpv=self.libmpv.get().strip(),
                           player_exe=self.player_exe.get().strip(), player_where=WHERE_CHOICES[self.player_where.get()],
                           media=dict(hwdec=bool(self.hwdec.get()), skip_extras=bool(self.skip_extras.get()),
                                      lan_share=bool(self.lan_share.get()), thumbs=bool(self.thumbs.get()),
                                      lan_hours=max(1, int(self.lan_hours.get() or 8)) if str(self.lan_hours.get() or "8").isdigit() else 8, alang=self.alang.get().strip(),
                                      slang=self.slang.get().strip(), os_lang=self.os_lang.get().strip() or "en",
                                      os_key=self.os_key.get().strip(), os_user=self.os_user.get().strip(),
                                      os_pass=self.os_pass.get(), tmdb_key=self.tmdb_key.get().strip()))
        self.destroy()


class RSSDialog(_Dialog):
    """The list of feeds: add, edit, remove, check now. Status columns refresh while the window is open."""

    def __init__(self, app):
        super().__init__(app, "RSS feeds")
        self.resizable(True, True)
        body = ttk.Frame(self, padding=(20, 18, 20, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="RSS feeds", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Torrents that appear in these feeds and match your words are added automatically. "
                             "A new feed only picks up items published after you add it (unless you ask for the "
                             "existing ones).", style="Muted.TLabel", wraplength=self.px(640), justify="left").pack(anchor="w", pady=(4, 10))
        fr = ttk.Frame(body)
        fr.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(fr, columns=("name", "on", "match", "status"), show="headings", height=8,
                               style="Compact.Treeview", selectmode="browse")
        for c, h, w in (("name", "Feed", 230), ("on", "On", 50), ("match", "Looks for", 180), ("status", "Last check", 380)):
            self.tv.heading(c, text=h, anchor="w")
            self.tv.column(c, width=self.px(w), minwidth=self.px(40), anchor="w", stretch=c == "status")
        self.tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", lambda e: self._edit())
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Check now", command=self._check).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Remove", command=self._remove).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Edit\u2026", command=self._edit).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Add feed\u2026", command=self._add).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Find feeds\u2026", style="Accent.TButton", command=self._find).pack(side="right", padx=(0, 8))
        self._fill()
        self.after(1000, self._tick)

    def _feeds(self):
        return self.app.cfg.setdefault("rss", [])

    def _fill(self):
        sel = self.tv.selection()
        self.tv.delete(*self.tv.get_children())
        for i, f in enumerate(self._feeds()):
            look = " ".join(x for x in (f.get("include", ""), ("not " + f["exclude"]) if f.get("exclude") else "") if x) or "everything"
            self.tv.insert("", "end", iid=str(i), values=(f.get("name") or f["url"], "yes" if f.get("enabled", True) else "no",
                                                          look, f.get("status") or "Not checked yet"))
        for i in sel:
            if self.tv.exists(i):
                self.tv.selection_set(i)

    def _tick(self):
        if self.winfo_exists():
            self._fill()
            self.after(1500, self._tick)

    def _current(self):
        sel = self.tv.selection()
        return int(sel[0]) if sel else None

    def _find(self):
        if self.app.find_feeds():
            self._fill()

    def _add(self, feed=None):
        res = FeedEditDialog(self.app, feed or rss.new_feed(), new=True).show()
        if res:
            self._feeds().append(res)
            self.app.save()
            self.app.check_feed(len(self._feeds()) - 1, manual=True)
            self._fill()

    def _edit(self):
        i = self._current()
        if i is None:
            return
        res = FeedEditDialog(self.app, dict(self._feeds()[i])).show()
        if res:
            old = self._feeds()[i]
            if res["url"] != old["url"]:
                res["primed"] = False        # a different address is a different feed
            self._feeds()[i] = res
            self.app.save()
            self._fill()

    def _remove(self):
        i = self._current()
        if i is not None:
            f = self._feeds().pop(i)
            self.app.cfg.get("rss_seen", {}).pop(f["url"], None)
            self.app.save()
            self._fill()

    def _check(self):
        i = self._current()
        if i is not None:
            self.app.check_feed(i, manual=True)
            self._fill()


class FindFeedsDialog(_Dialog):
    """Find feeds to subscribe to: type a website address (its feeds are looked up) or a keyword (a public feed
    directory is searched). Each candidate is fetched, so the list shows how many downloadable torrents it has."""

    def __init__(self, app, initial=""):
        super().__init__(app, "Find RSS feeds")
        self.q, self.found = queue.Queue(), []
        body = ttk.Frame(self, padding=(20, 18, 20, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Find feeds", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Type a website address (for example example.org) and its feeds are looked up, or a "
                             "keyword to search a public feed directory. Feeds with downloadable torrents come first.",
                  style="Muted.TLabel", wraplength=self.px(620), justify="left").pack(anchor="w", pady=(4, 10))
        row = ttk.Frame(body)
        row.pack(fill="x")
        self.text = tk.StringVar(value=initial)
        entry = ttk.Entry(row, textvariable=self.text, width=52)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Search", style="Accent.TButton", command=self._search).pack(side="left", padx=(8, 0))
        entry.bind("<Return>", lambda e: self._search())
        entry.focus_set()
        fr = ttk.Frame(body)
        fr.pack(fill="both", expand=True, pady=(12, 0))
        self.tv = ttk.Treeview(fr, columns=("title", "torrents", "items", "url"), show="headings", height=8,
                               style="Compact.Treeview", selectmode="browse")
        for c, h, w in (("title", "Feed", 200), ("torrents", "Torrents", 70), ("items", "Items", 55), ("url", "Address", 300)):
            self.tv.heading(c, text=h, anchor="w")
            self.tv.column(c, width=self.px(w), minwidth=self.px(40), anchor="w", stretch=c == "url")
        self.tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", lambda e: self._subscribe())
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._show_sample())
        self.msg = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.msg, style="Muted.TLabel", wraplength=self.px(620), justify="left").pack(anchor="w", pady=(8, 0))
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Subscribe\u2026", style="Accent.TButton", command=self._subscribe).pack(side="right", padx=(0, 8))
        if initial:
            self.after(150, self._search)

    def _search(self):
        text = self.text.get().strip()
        if not text:
            return
        self.tv.delete(*self.tv.get_children())
        self.found = []
        self.msg.set("Looking\u2026")

        def work():
            try:
                self.q.put(("ok", rss.find_feeds(text)))
            except Exception as e:
                self.q.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()
        self.after(200, self._poll)

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            kind, payload = self.q.get_nowait()
        except queue.Empty:
            self.after(200, self._poll)
            return
        if kind == "err":
            self.msg.set(payload)
            return
        self.found = payload
        for i, f in enumerate(payload):
            self.tv.insert("", "end", iid=str(i), values=(f["title"], f["torrents"], f["items"], f["url"]))
        self.tv.selection_set("0")
        self.msg.set("%d feed(s) found. Select one and click Subscribe." % len(payload))

    def _show_sample(self):
        sel = self.tv.selection()
        if sel and self.found:
            f = self.found[int(sel[0])]
            self.msg.set(("e.g. " + "  |  ".join(f["sample"])) if f["sample"] else "No downloadable torrents in this feed right now.")

    def _subscribe(self):
        sel = self.tv.selection()
        if sel and self.found:
            self.result = self.found[int(sel[0])]
            self.destroy()


class FeedEditDialog(_Dialog):
    """One feed: address, filter words, where the torrents go. 'Preview' shows what the filter would take right now."""

    def __init__(self, app, feed, new=False):
        super().__init__(app, "New RSS feed" if new else "Edit RSS feed")
        self.feed = feed
        self.q = queue.Queue()
        body = ttk.Frame(self, padding=(20, 18, 20, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        v = lambda key, default="": tk.StringVar(value=str(feed.get(key, default)))   # noqa: E731
        self.name, self.url, self.include, self.exclude = v("name"), v("url"), v("include"), v("exclude")
        self.interval, self.category, self.folder = v("interval", rss.DEFAULT_INTERVAL), v("category"), v("folder")
        self.enabled = tk.BooleanVar(value=feed.get("enabled", True))
        self.start = tk.BooleanVar(value=feed.get("start", True))
        self.grab = tk.BooleanVar(value=feed.get("grab_existing", False))
        rows = (("Name (optional)", self.name), ("Feed address", self.url), ("Title contains", self.include),
                ("Title must not contain", self.exclude))
        for r, (label, var) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", padx=(0, 14), pady=4)
            ttk.Entry(body, textvariable=var, width=54).grid(row=r, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(body, text="Words must all appear (any case); use \"quotes\" for phrases, or start with re: for a regular "
                             "expression. Leave empty to take every item.", style="Muted.TLabel", wraplength=self.px(520),
                  justify="left").grid(row=4, column=1, columnspan=2, sticky="w")
        ttk.Label(body, text="Check every").grid(row=5, column=0, sticky="w", pady=(10, 4))
        f = ttk.Frame(body)
        f.grid(row=5, column=1, columnspan=2, sticky="w", pady=(10, 4))
        ttk.Spinbox(f, from_=1, to=10080, textvariable=self.interval, width=7).pack(side="left")
        ttk.Label(f, text="minutes").pack(side="left", padx=(6, 0))
        ttk.Label(body, text="Category").grid(row=6, column=0, sticky="w", pady=4)
        cats = [""] + sorted(app.cfg.get("categories", {}), key=str.lower)
        ttk.Combobox(body, values=cats, textvariable=self.category, state="readonly", width=24).grid(row=6, column=1, sticky="w", pady=4)
        ttk.Label(body, text="Save to").grid(row=7, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.folder, width=40).grid(row=7, column=1, sticky="ew", pady=4)
        ttk.Button(body, text="Browse\u2026", command=lambda: self._pick(self.folder)).grid(row=7, column=2, padx=(8, 0))
        ttk.Label(body, text="Empty = the category's folder, or your default folder.", style="Muted.TLabel").grid(
            row=8, column=1, columnspan=2, sticky="w")
        ttk.Checkbutton(body, text="Start downloading straight away", variable=self.start).grid(
            row=9, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(body, text="Also take the items already in the feed (otherwise only new ones)",
                        variable=self.grab).grid(row=10, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(body, text="This feed is switched on", variable=self.enabled).grid(
            row=11, column=1, columnspan=2, sticky="w", pady=(4, 0))
        self.out = ttk.Treeview(body, columns=("take", "title"), show="headings", height=5, style="Compact.Treeview")
        self.out.heading("take", text="", anchor="w")
        self.out.heading("title", text="Preview: items in the feed right now", anchor="w")
        self.out.column("take", width=self.px(60), stretch=False)
        self.out.column("title", width=self.px(460))
        self.out.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.msg = ttk.Label(body, text="", style="Muted.TLabel", wraplength=self.px(540), justify="left")
        self.msg.grid(row=13, column=0, columnspan=3, sticky="w", pady=(4, 0))
        btns = ttk.Frame(body)
        btns.grid(row=14, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Preview", command=self._preview).pack(side="right", padx=(0, 8))

    def _pick(self, var):
        d = filedialog.askdirectory(parent=self, title="Save folder", initialdir=var.get() or None)
        if d:
            var.set(d)

    def _current(self):
        f = dict(self.feed)
        f.update(name=self.name.get().strip(), url=self.url.get().strip(), include=self.include.get().strip(),
                 exclude=self.exclude.get().strip(), category=self.category.get(), folder=self.folder.get().strip(),
                 start=bool(self.start.get()), grab_existing=bool(self.grab.get()), enabled=bool(self.enabled.get()))
        return f

    def _preview(self):
        feed = self._current()
        for spec in (feed["include"], feed["exclude"]):
            err = rss.check_regex(spec)
            if err:
                self.msg.configure(text="Bad regular expression: %s" % err)
                return
        self.msg.configure(text="Fetching\u2026")
        self.out.delete(*self.out.get_children())

        def work():
            try:
                _title, items = rss.parse_feed(rss.fetch(feed["url"]))
                self.q.put((feed, items))
            except Exception as e:
                self.q.put((feed, str(e)))
        threading.Thread(target=work, daemon=True).start()
        self.after(200, self._poll)

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            feed, res = self.q.get_nowait()
        except queue.Empty:
            self.after(200, self._poll)
            return
        if isinstance(res, str):
            self.msg.configure(text="Could not read the feed: %s" % res)
            return
        take = 0
        for i, it in enumerate(res[:200]):
            ok = rss.matches(it, feed)
            take += ok
            self.out.insert("", "end", iid=str(i), values=("take" if ok else "skip", it["title"]))
        self.msg.configure(text="%d of %d items match%s." % (take, len(res), " (showing the first 200)" if len(res) > 200 else ""))

    def _ok(self):
        feed = self._current()
        from tkinter import messagebox
        if not feed["url"].lower().startswith(("http://", "https://")):
            messagebox.showerror("RSS feed", "The feed address must start with http:// or https://", parent=self)
            return
        for spec in (feed["include"], feed["exclude"]):
            err = rss.check_regex(spec)
            if err:
                messagebox.showerror("RSS feed", "Bad regular expression: %s" % err, parent=self)
                return
        try:
            feed["interval"] = max(1, int(self.interval.get()))
        except ValueError:
            messagebox.showerror("RSS feed", "Check interval must be a whole number of minutes.", parent=self)
            return
        self.result = feed
        self.destroy()


class BannedDialog(_Dialog):
    """The peer addresses the user banned; select some and unban them."""

    def __init__(self, app):
        super().__init__(app, "Banned peers")
        body = ttk.Frame(self, padding=(20, 18, 20, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Banned peers", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="These addresses are refused and never contacted. Right-click a peer on the Peers tab to ban it.",
                  style="Muted.TLabel", wraplength=self.px(380), justify="left").pack(anchor="w", pady=(4, 10))
        fr = ttk.Frame(body)
        fr.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(fr, columns=("ip",), show="headings", height=8, style="Compact.Treeview", selectmode="extended")
        self.tv.heading("ip", text="Address", anchor="w")
        self.tv.column("ip", width=self.px(360), anchor="w")
        self.tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Unban all", command=lambda: self._unban(all_=True)).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Unban selected", command=self._unban).pack(side="right", padx=(0, 8))
        self._fill()

    def _fill(self):
        self.tv.delete(*self.tv.get_children())
        for ip in sorted(self.app.mgr.banned):
            self.tv.insert("", "end", iid=ip, values=(ip,))

    def _unban(self, all_=False):
        for ip in (list(self.app.mgr.banned) if all_ else self.tv.selection()):
            self.app.unban(ip)
        self._fill()


class AboutDialog(_Dialog):
    def __init__(self, app):
        super().__init__(app, "About")
        body = about_content(self, app, self.px, padding=(28, 24, 28, 20), close=self.destroy)
        body.pack(fill="both", expand=True)


def about_content(parent, app, px, padding=(0, 0, 0, 0), close=None):
    """Version, description, credits and the data folder, built into a new frame in `parent` (not yet placed).
    Used by the About window and by the About page in Settings; `close` adds a Close button (the window)."""
    import version
    body = ttk.Frame(parent, padding=padding)
    ttk.Label(body, text=app_name(), style="Title.TLabel").pack(anchor="w")
    ttk.Label(body, text="Version %s" % version.VERSION, style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
    ttk.Label(body, text="A BitTorrent client written in pure Python: DHT, peer exchange, magnet links, "
                         "encryption, uTP, automatic port forwarding, categories, seeding rules and more.",
              wraplength=px(420), justify="left").pack(anchor="w")
    ttk.Label(body, text="Credits", style="Muted.TLabel").pack(anchor="w", pady=(14, 2))
    ttk.Label(body, text="Peer locations: IP geolocation by DB-IP.com (CC BY 4.0)\n"
                         "National flags: flag-icons (MIT)\n"
                         "Icons: Segoe Fluent Icons / Segoe MDL2 Assets (Windows)",
              wraplength=px(420), justify="left").pack(anchor="w")
    ttk.Label(body, text="Your data stays on this computer; the app only talks to trackers, DHT peers and "
                         "(if you use them) the update check and online search.",
              style="Muted.TLabel", wraplength=px(420), justify="left").pack(anchor="w", pady=(14, 0))
    bt = ttk.Frame(body)
    bt.pack(fill="x", pady=(16, 0))
    side = "right" if close else "left"                 # window: buttons on the right; Settings page: on the left
    if close:
        ttk.Button(bt, text="Close", style="Accent.TButton", command=close).pack(side="right")
    for text, fn in (("Open data folder", lambda: _open_data(app)), ("Open error log", lambda: _open_log(body)))[::-1 if close else 1]:
        ttk.Button(bt, text=text, command=fn).pack(side=side, padx=(0, 8))
    ttk.Label(body, text="Settings, torrents and search plugins live in:\n" + app.dir, style="Muted.TLabel",
              wraplength=px(420), justify="left").pack(anchor="w", pady=(10, 0))
    return body


def _open_data(app):
    try:
        os.startfile(app.dir)
    except (AttributeError, OSError):
        pass


def _open_log(parent):
    import crashlog
    p = crashlog.path
    try:
        if p and not os.path.exists(p):
            open(p, "a").close()
        os.startfile(p)                   # Windows
    except (AttributeError, OSError, TypeError):
        from tkinter import messagebox
        messagebox.showinfo("Error log", "The error log is here:\n\n%s" % p, parent=parent)


def app_name():
    return "Bit Torrent Client"


class DiagnosticsDialog(_Dialog):
    """Live view of what the port mapper found and tried, with plain-language hints."""

    def __init__(self, app):
        super().__init__(app, "Port forwarding diagnostics")
        self.resizable(True, True)
        P = app.P
        body = ttk.Frame(self, padding=(20, 18, 20, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Port forwarding diagnostics", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="What the app found on your network and what it tried. Click Re-scan after changing router "
                             "settings; Copy puts everything on the clipboard so you can send it to whoever is helping.",
                  style="Muted.TLabel", wraplength=self.px(700), justify="left").pack(anchor="w", pady=(4, 12))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, width=100, height=24, wrap="word", bd=0, relief="flat", bg=P["surface"], fg=P["fg"],
                            insertbackground=P["fg"], font=app.F.mono, padx=12, pady=10, highlightthickness=1,
                            highlightbackground=P["border"], highlightcolor=P["border"])
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        self.copy_btn = ttk.Button(btns, text="Copy to clipboard", command=self._copy)
        self.copy_btn.pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Re-scan router", style="Accent.TButton", command=self._rescan).pack(side="right", padx=(0, 8))
        self.report = ""
        self._reload()
        self.after(1500, self._tick)

    def _reload(self):
        report = self.app.diagnostics_text()
        if report == self.report:
            return
        self.report = report
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", report)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _tick(self):
        try:
            self._reload()
            self.after(1500, self._tick)
        except tk.TclError:
            pass   # the window was closed

    def _rescan(self):
        pm = self.app.mgr.portmap
        if pm:
            pm.rescan()
        self.app.check_network()       # the periodic _reload shows the result when it is in
        self._reload()

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.report)
        self.copy_btn.configure(text="Copied")
        self.after(1800, lambda: self.copy_btn.configure(text="Copy to clipboard"))


class SearchWindow(tk.Toplevel):
    """Non-modal online search: pick a source, type a query, double-click a result to add it."""

    def __init__(self, app, sources, errors, query=""):
        super().__init__(app.root)
        import queue
        self.app, self.sources, self.q = app, sources, queue.Queue()
        self.results, self._token = [], 0
        self._sig = self._plugin_sig()
        P = app.P
        self.title("Search online")
        self.configure(bg=P["bg"])
        self.geometry("%dx%d" % (app.px(860), app.px(520)))
        self.minsize(app.px(620), app.px(380))
        self.bind("<Escape>", lambda e: self.destroy())
        if chrome.enabled(app.cfg):
            chrome.attach(self, P, app.px, "Search online", resizable=True, buttons=("close",), font=app.F.bold)
        else:
            self.after(60, lambda: ui.set_glass(self, app.P) if self.winfo_exists() else None)

        top = ttk.Frame(self, padding=(14, 14, 14, 8))
        top.pack(fill="x")
        self.query = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.query)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.run())
        remembered = app.cfg.get("search_source")
        names = self._names(sources)
        self.source = tk.StringVar(value=remembered if remembered in names else sources[0].name)
        self.combo = ttk.Combobox(top, values=names, textvariable=self.source, state="readonly",
                                  width=18, postcommand=self.reload_sources)
        self.combo.pack(side="left", padx=8)
        self.unlimited = tk.BooleanVar(value=bool(app.cfg.get("search_no_limit", False)))
        ttk.Checkbutton(top, text="No result limit", variable=self.unlimited).pack(side="left", padx=(0, 8))
        ttk.Button(top, text="Search", style="Accent.TButton", command=self.run).pack(side="left")

        self.status = tk.StringVar(value="Type what you want to find and press Enter.")
        if errors:
            self.status.set("Could not load plugin(s): " + "; ".join(errors))
        ttk.Label(self, textvariable=self.status, style="Muted.TLabel", padding=(14, 0, 14, 6)).pack(fill="x")

        mid = ttk.Frame(self, padding=(14, 0, 14, 0))
        mid.pack(fill="both", expand=True)
        cols = (("name", "Name", 430, "w"), ("size", "Size", 90, "e"), ("seeds", "Seeds", 70, "e"),
                ("source", "\u2003\u2003Source", 140, "w"))
        self.tree = ttk.Treeview(mid, columns=[c[0] for c in cols], show="headings", style="Dialog.Treeview",
                                 selectmode="browse")
        for key, text, w, anchor in cols:
            self.tree.heading(key, text=text, anchor=anchor, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=app.px(w), anchor=anchor, stretch=key == "name")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.download())
        self.tree.bind("<Return>", lambda e: self.download())

        bot = ttk.Frame(self, padding=14)
        bot.pack(fill="x")
        ttk.Button(bot, text="Plugins…", command=self.open_plugins).pack(side="left")
        self.bind("<FocusIn>", lambda e: self.reload_sources() if e.widget is self else None, add="+")
        ttk.Button(bot, text="Close", command=self.destroy).pack(side="right")
        self.dl = ttk.Button(bot, text="Download selected", style="Accent.TButton", command=self.download)
        self.dl.pack(side="right", padx=8)
        self._sort = ("seeds", True)
        self.entry.focus_set()
        self.after(100, self._poll)
        if query:
            self.query.set(query)
            self.after(150, self.run)

    def _plugin_sig(self):
        import search as _s
        try:
            d = _s.plugin_dir(self.app.dir)
            return (tuple(sorted((n, os.path.getmtime(os.path.join(d, n))) for n in os.listdir(d)
                                 if n.endswith(".py") and not n.startswith("_"))), bool(self.app.cfg.get("rss")))
        except OSError:
            return None

    def reload_sources(self):
        """Pick up plugins added, edited or removed since the window opened (no restart needed)."""
        sig = self._plugin_sig()
        if sig is None or sig == self._sig:
            return                                        # nothing changed in the plugin folder
        self._sig = sig
        try:
            sources, errors = self.app.load_search_sources()
        except Exception:
            return
        self.sources = sources
        names = self._names(sources)
        self.combo.configure(values=names)
        if self.source.get() not in names:
            self.source.set(names[0])
        if errors:
            self.status.set("Could not load plugin(s): " + "; ".join(errors))

    # -- searching ------------------------------------------------------------
    @staticmethod
    def _names(sources):
        """Source list for the drop-down: "All sources" first when there is more than one."""
        import search
        names = [x.name for x in sources]
        return ([search.ALL] if len(names) > 1 else []) + names

    def run(self):
        import search
        import threading
        self.reload_sources()
        text = self.query.get().strip()
        every = self.source.get() == search.ALL
        src = None if every else next((s for s in self.sources if s.name == self.source.get()), self.sources[0])
        if not text and (every or src.name != "My RSS feeds"):      # an empty search lists everything in your feeds
            return
        unlimited = bool(self.unlimited.get())
        self._token += 1
        token = self._token
        name = search.ALL if every else src.name
        if self.app.cfg.get("search_source") != name or bool(self.app.cfg.get("search_no_limit")) != unlimited:
            self.app.cfg["search_source"], self.app.cfg["search_no_limit"] = name, unlimited
            self.app.save()
        self.tree.delete(*self.tree.get_children())
        self.results = []
        if every:
            srcs = list(self.sources)
            self._all = {"total": len(srcs), "answered": 0, "failed": {}}
            self.status.set("Searching %d sources…" % len(srcs))

            def work_all():
                search.search_all(srcs, text, unlimited, report=lambda n, res, err: self.q.put((token, "part", (n, res, err))))
            threading.Thread(target=work_all, daemon=True).start()
            return
        self._all = None
        self.status.set("Searching %s…" % src.name)

        def work():
            try:
                self.q.put((token, "ok", src.search(text, unlimited=unlimited)))
            except Exception as e:
                self.q.put((token, "err", "%s: %s" % (src.name, e)))
        threading.Thread(target=work, daemon=True).start()

    def _all_status(self):
        """Status line while / after searching every source."""
        a, n = self._all, len(self.results)
        txt = "%d result%s" % (n, "" if n == 1 else "s")
        if a["answered"] < a["total"]:
            txt += " so far - %d of %d sources answered…" % (a["answered"], a["total"])
        else:
            txt += " from %d source%s." % (a["total"] - len(a["failed"]), "" if a["total"] - len(a["failed"]) == 1 else "s")
            txt += " Double-click one to add it." if n else ""
        if a["failed"]:
            txt += "  Failed: " + "; ".join("%s (%s)" % (k, v[:60]) for k, v in sorted(a["failed"].items()))
        return txt

    def _poll(self):
        if not self.winfo_exists():
            return
        import queue
        try:
            while True:
                token, kind, payload = self.q.get_nowait()
                if token != self._token:
                    continue
                if kind == "part":                       # one source of an "All sources" search answered
                    import search
                    name, res, err = payload
                    self._all["answered"] += 1
                    if err:
                        self._all["failed"][name] = err
                    else:
                        search.merge(self.results, res)
                        self.fill()
                    self.status.set(self._all_status())
                elif kind == "err":
                    self.status.set("Search failed - " + payload)
                elif kind == "dl":
                    self._downloaded(payload)
                else:
                    self.results = payload
                    self.status.set("%d result%s. Double-click one to add it." % (len(payload), "" if len(payload) == 1 else "s")
                                    if payload else "No results.")
                    self.fill()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        key, rev = self._sort
        def k(r):
            return {"name": (r["name"] or "").lower(), "size": r["size"] if r["size"] is not None else -1,
                    "seeds": r["seeders"] if r["seeders"] is not None else -1, "source": r["source"]}[key]
        order = sorted(range(len(self.results)), key=lambda i: k(self.results[i]), reverse=rev)
        for i in order:
            r = self.results[i]
            self.tree.insert("", "end", iid=str(i), values=(
                r["name"], fmt_size(r["size"]) if r["size"] is not None else "-",
                r["seeders"] if r["seeders"] is not None else "-", "\u2003\u2003" + r["source"]))

    def sort(self, key):
        self._sort = (key, not self._sort[1] if self._sort[0] == key else key != "name" and key != "source")
        self.fill()

    # -- adding ---------------------------------------------------------------
    def download(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.results[int(sel[0])]
        if r.get("magnet"):
            self.app.add_magnet_dialog(r["magnet"])
            return
        self.status.set("Fetching the .torrent file…")
        token = self._token
        import threading, search

        def work():
            try:
                self.q.put((token, "dl", search.http_get(r["torrent_url"], timeout=30)))
            except Exception as e:
                self.q.put((token, "err", "could not fetch the .torrent: %s" % e))
        threading.Thread(target=work, daemon=True).start()

    def _downloaded(self, raw):
        import tempfile
        self.status.set("Added to the add-torrent dialog.")
        fd, path = tempfile.mkstemp(suffix=".torrent")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            self.app.add_torrents([path])
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def open_plugins(self):
        import search
        d = search.plugin_dir(self.app.dir)
        try:
            os.startfile(d)           # Windows
        except AttributeError:
            from tkinter import messagebox
            messagebox.showinfo("Search plugins", "Plugin folder:\n\n" + d, parent=self)
        except OSError:
            pass


class TorrentLimitDialog(_Dialog):
    """Speed limits for one torrent (on top of the global limits)."""

    def __init__(self, app, name, down, up, count=1):
        super().__init__(app, "Speed limit")
        body = ttk.Frame(self, padding=(22, 18, 22, 18))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=name if count == 1 else "%d torrents" % count, style="Title.TLabel",
                  wraplength=self.px(380)).grid(row=0, column=0, columnspan=3, sticky="w")
        self.down, self.up = tk.StringVar(value=str(down)), tk.StringVar(value=str(up))
        for i, (label, var) in enumerate((("Download limit", self.down), ("Upload limit", self.up))):
            ttk.Label(body, text=label).grid(row=i + 1, column=0, sticky="w", padx=(0, 16), pady=(14 if i == 0 else 6, 0))
            ttk.Spinbox(body, from_=0, to=10_000_000, textvariable=var, width=10).grid(
                row=i + 1, column=1, sticky="w", pady=(14 if i == 0 else 6, 0))
            ttk.Label(body, text="KiB/s", style="Muted.TLabel").grid(row=i + 1, column=2, sticky="w", padx=(8, 0))
        ttk.Label(body, text="0 = no limit for this torrent. The global limits in Settings still apply.",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.err = ttk.Label(body, text="", style="Bad.TLabel")
        self.err.grid(row=4, column=0, columnspan=3, sticky="w")
        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="OK", style="Accent.TButton", command=self._ok).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())

    def _ok(self):
        try:
            d, u = int(self.down.get()), int(self.up.get())
            if d < 0 or u < 0:
                raise ValueError
        except ValueError:
            self.err.configure(text="Enter whole numbers (0 = no limit).")
            return
        self.result = (d, u)
        self.destroy()


DEFAULT_TRACKERS = ("udp://tracker.opentrackr.org:1337/announce\n"
                    "udp://open.stealth.si:80/announce\n"
                    "udp://tracker.torrent.eu.org:451/announce")
PIECE_CHOICES = {"Automatic": 0, "256 KiB": 256 << 10, "512 KiB": 512 << 10, "1 MiB": 1 << 20, "2 MiB": 2 << 20,
                 "4 MiB": 4 << 20, "8 MiB": 8 << 20, "16 MiB": 16 << 20}


class CreateTorrentDialog(_Dialog):
    """Make a .torrent from a file or folder, optionally adding it to the list to seed it straight away."""

    def __init__(self, app):
        super().__init__(app, "Create torrent")
        import queue
        self.q, self._cancel, self._busy = queue.Queue(), False, False
        self.resizable(True, False)
        body = ttk.Frame(self, padding=(22, 18, 22, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="Create torrent", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text="File or folder to share", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(14, 4))
        self.path = tk.StringVar()
        ttk.Entry(body, textvariable=self.path).grid(row=2, column=0, sticky="ew")
        ttk.Button(body, text="File…", command=self._pick_file).grid(row=2, column=1, padx=(8, 0))
        ttk.Button(body, text="Folder…", command=self._pick_folder).grid(row=2, column=2, padx=(6, 0))
        ttk.Label(body, text="Trackers (one per line; leave empty for a trackerless torrent that relies on the DHT)",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(14, 4))
        self.trackers = tk.Text(body, height=4, width=64, bd=0, relief="flat", font=app.F.mono,
                                bg=app.P["surface"], fg=app.P["fg"], insertbackground=app.P["fg"],
                                highlightthickness=1, highlightbackground=app.P["border"], highlightcolor=app.P["muted"])
        self.trackers.insert("1.0", DEFAULT_TRACKERS)
        self.trackers.grid(row=4, column=0, columnspan=3, sticky="ew")
        opt = ttk.Frame(body)
        opt.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        opt.columnconfigure(3, weight=1)
        ttk.Label(opt, text="Piece size").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.piece = tk.StringVar(value="Automatic")
        ttk.Combobox(opt, values=list(PIECE_CHOICES), textvariable=self.piece, state="readonly", width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(opt, text="Comment").grid(row=0, column=2, sticky="w", padx=(16, 8))
        self.comment = tk.StringVar()
        ttk.Entry(opt, textvariable=self.comment).grid(row=0, column=3, sticky="ew")
        self.private = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="Private torrent (no DHT or peer exchange)", variable=self.private).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.seed = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Add it to the list and start seeding", variable=self.seed).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.bar = ttk.Progressbar(body, maximum=100, mode="determinate", style="Down.Horizontal.TProgressbar")
        self.bar.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.msg = ttk.Label(body, text="", style="Muted.TLabel", wraplength=self.px(520), justify="left")
        self.msg.grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))
        btns = ttk.Frame(body)
        btns.grid(row=10, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Close", command=self._close).pack(side="left", padx=(0, 8))
        self.go = ttk.Button(btns, text="Create", style="Accent.TButton", command=self.create)
        self.go.pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)

    def _pick_file(self):
        p = filedialog.askopenfilename(parent=self, title="Choose the file to share")
        if p:
            self.path.set(p)

    def _pick_folder(self):
        p = filedialog.askdirectory(parent=self, title="Choose the folder to share")
        if p:
            self.path.set(p)

    def _close(self):
        self._cancel = True
        self.destroy()

    def create(self):
        src = self.path.get().strip()
        if not src or not os.path.exists(src):
            self.msg.configure(text="Choose an existing file or folder first.", style="Bad.TLabel")
            return
        if self._busy:
            return
        name = os.path.basename(src.rstrip("\\/")) or "torrent"
        out = filedialog.asksaveasfilename(parent=self, title="Save the .torrent file", defaultextension=".torrent",
                                           initialfile=name + ".torrent", filetypes=[("Torrent files", "*.torrent")])
        if not out:
            return
        self._busy, self._cancel = True, False
        self.msg.configure(text="Hashing…", style="Muted.TLabel")
        trackers = self.trackers.get("1.0", "end").split()
        opts = dict(trackers=trackers, piece_length=PIECE_CHOICES.get(self.piece.get(), 0),
                    private=bool(self.private.get()), comment=self.comment.get().strip())
        import threading

        def work():
            from torrent_core import create_torrent
            try:
                raw = create_torrent(src, progress=lambda f: self.q.put(("p", f)), cancel=lambda: self._cancel, **opts)
                with open(out, "wb") as f:
                    f.write(raw)
                self.q.put(("done", (raw, out, src)))
            except InterruptedError:
                pass
            except Exception as e:
                self.q.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if not self.winfo_exists():
            return
        import queue
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "p":
                    self.bar["value"] = val * 100
                elif kind == "err":
                    self._busy = False
                    self.msg.configure(text="Could not create the torrent: %s" % val, style="Bad.TLabel")
                elif kind == "done":
                    self._busy = False
                    raw, out, src = val
                    self.bar["value"] = 100
                    self.msg.configure(text="Saved %s" % out, style="Muted.TLabel")
                    if self.seed.get():
                        self.app._add_raw(raw, os.path.dirname(os.path.abspath(src)), True)
                        self.app.refresh_now()
        except queue.Empty:
            pass
        self.after(100, self._poll)


class LibraryDialog(_Dialog):
    """Everything in the client you can watch or listen to: continue-watching first, details from TMDB when a key is set."""

    def __init__(self, app):
        super().__init__(app, "Library")
        import library, media_state
        self.library, self.media_state = library, media_state
        self.resizable(True, True)
        self.meta = library.Meta(app.dir, app.cfg.get("tmdb_key", ""))
        body = ttk.Frame(self, padding=(20, 18, 20, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Library", style="Title.TLabel").pack(anchor="w")
        self.note = ttk.Label(body, style="Muted.TLabel", wraplength=self.px(760), justify="left")
        self.note.pack(anchor="w", pady=(4, 10))
        mid = ttk.Frame(body)
        mid.pack(fill="both", expand=True)
        cols = (("title", "Title", 300, "w"), ("year", "Year", 60, "w"), ("kind", "Type", 70, "w"),
                ("files", "Files", 60, "e"), ("watched", "Watched", 80, "e"), ("resume", "Continue at", 100, "e"))
        self.tv = ttk.Treeview(mid, columns=[c[0] for c in cols], show="headings", height=14,
                               style="Dialog.Treeview", selectmode="browse")
        for key, text, w, anchor in cols:
            self.tv.heading(key, text=text, anchor=anchor)
            self.tv.column(key, width=self.px(w), anchor=anchor, stretch=key == "title")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        side = ttk.Frame(mid, padding=(14, 0, 0, 0), width=self.px(240))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.poster = ttk.Label(side)
        self.poster.pack(anchor="w")
        self.det_title = ttk.Label(side, style="Title.TLabel", wraplength=self.px(220), justify="left")
        self.det_title.pack(anchor="w", pady=(6, 0))
        self.det = ttk.Label(side, style="Muted.TLabel", wraplength=self.px(220), justify="left")
        self.det.pack(anchor="w", pady=(4, 0))
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Play", command=self._play).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Mark all watched", command=lambda: self._mark(True)).pack(side="left")
        ttk.Button(btns, text="Mark all not watched", command=lambda: self._mark(False)).pack(side="left", padx=(8, 0))
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._select())
        self.tv.bind("<Double-1>", lambda e: self._play())
        self.rows = {}
        self._img = None
        self._fill()
        threading.Thread(target=self._fetch_all, daemon=True).start() if self.meta.key else None
        self.geometry("%dx%d" % (self.px(900), self.px(560)))

    def _fill(self):
        sel = self.tv.selection()
        sessions = []
        for h, s in self.app.mgr.sessions.items():
            if hasattr(s, "t"):
                sessions.append((h, s.t.name, [((f[0][1:] if s.t.multi else f[0]), f[1]) for f in s.t.files]))
        state = self.media_state.load(self.app.dir)
        rows = self.library.entries(sessions, state)
        self.rows = {r["hash"]: r for r in rows}
        self.tv.delete(*self.tv.get_children())
        cont = [r for r in rows if r["resume"]]
        cont.sort(key=lambda r: -r["resume"].get("at", 0))
        for r in cont + [r for r in rows if not r["resume"]]:
            d = self.meta.cache.get("%s|%s|%s" % (r["info"]["kind"], r["info"]["title"].lower(), r["info"].get("year") or "")) or {}
            title = d.get("title") or r["info"]["title"]
            rs = r["resume"]
            self.tv.insert("", "end", iid=r["hash"], values=(
                ("▶ " if rs else "") + title, d.get("year") or r["info"].get("year") or "",
                "TV" if r["info"]["kind"] == "tv" else ("Music" if r["kind"] == "audio" else "Movie"),
                r["files"], "%d/%d" % (r["watched"], r["files"]),
                "%d:%02d" % (rs["pos"] // 60, rs["pos"] % 60) if rs else ""))
        self.note.configure(text=("%d title(s). " % len(rows)) + ("" if self.meta.key else
                            "Add a TMDB API key in Settings > Media for descriptions and posters.") if rows else
                            "Nothing to watch yet: torrents with video or audio files appear here.")
        if sel and self.tv.exists(sel[0]):
            self.tv.selection_set(sel[0])

    def _fetch_all(self):
        changed = False
        for r in list(self.rows.values()):
            if self.meta.lookup(r["info"]) is not None:
                changed = True
        if changed:
            try:
                self.after(0, self._fill)
            except Exception:
                pass

    def _select(self):
        sel = self.tv.selection()
        r = self.rows.get(sel[0]) if sel else None
        if not r:
            return
        d = self.meta.cache.get("%s|%s|%s" % (r["info"]["kind"], r["info"]["title"].lower(), r["info"].get("year") or "")) or {}
        self.det_title.configure(text=d.get("title") or r["info"]["title"])
        extra = "★ %.1f   " % d["rating"] if d.get("rating") else ""
        self.det.configure(text=extra + (d.get("overview") or r["name"]))
        self.poster.configure(image="")
        self._img = None
        path = self.meta.poster_file(d) if d.get("poster") else None
        if path:
            try:
                from PIL import Image, ImageTk
                self._img = ImageTk.PhotoImage(Image.open(path))
                self.poster.configure(image=self._img)
            except Exception:                                   # no Pillow: Windows imaging / libmpv make a PNG for Tk
                try:
                    import thumbs
                    png = thumbs.png_for(path, self.app.cfg.get("libmpv", ""))
                    if png:
                        self._img = tk.PhotoImage(file=png)
                        self.poster.configure(image=self._img)
                except Exception:
                    pass

    def _play(self):
        sel = self.tv.selection()
        if sel:
            self.app.play(sel[0])

    def _mark(self, on):
        sel = self.tv.selection()
        sess = self.app.mgr.sessions.get(sel[0]) if sel else None
        if hasattr(sess, "t"):
            import stream
            for i, f in enumerate(sess.t.files):
                if stream.media_kind(f[0][-1]):
                    self.media_state.set_watched(self.app.dir, "%s:%d" % (sel[0], i), on)
            self._fill()


class CastDialog(_Dialog):
    """Find TVs / Chromecasts / DLNA renderers on the network and play one video on the chosen one."""

    def __init__(self, app, h, idx):
        super().__init__(app, "Cast to TV")
        self.h, self.idx, self.devices, self.dev = h, idx, [], None
        self.box = queue.Queue()
        body = ttk.Frame(self, padding=(20, 18, 20, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Cast to TV", style="Title.TLabel").pack(anchor="w")
        self.msg = tk.StringVar(value="Looking for devices on your network\u2026")
        ttk.Label(body, textvariable=self.msg, style="Muted.TLabel", wraplength=self.px(420), justify="left").pack(anchor="w", pady=(4, 10))
        self.lb = tk.Listbox(body, height=7, width=48, exportselection=False, bd=0, highlightthickness=1, activestyle="none",
                             bg=app.P["surface"], fg=app.P["muted"], selectbackground=app.P["surface"],
                             selectforeground=app.P["fg"], font=app.F.ui)
        self.lb.pack(fill="both", expand=True)
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")
        self.b_stop = ttk.Button(row, text="Stop", command=lambda: self._do("stop"))
        self.b_stop.pack(side="right", padx=(0, 8))
        self.b_pause = ttk.Button(row, text="Pause", command=lambda: self._do("pause"))
        self.b_pause.pack(side="right", padx=(0, 8))
        self.b_resume = ttk.Button(row, text="Resume", command=lambda: self._do("resume"))
        self.b_resume.pack(side="right", padx=(0, 8))
        ttk.Button(row, text="Play on selected", command=self._play).pack(side="left")
        ttk.Button(row, text="Scan again", command=self._scan).pack(side="left", padx=(8, 0))
        self.bind("<Destroy>", lambda e: None)
        self._scan()
        self.after(150, self._poll)

    def _scan(self):
        import cast
        self.lb.delete(0, "end")
        self.devices = []
        self.msg.set("Looking for devices on your network\u2026")
        threading.Thread(target=lambda: self.box.put(("scan", cast.discover(3.5))), daemon=True).start()

    def _play(self):
        sel = self.lb.curselection()
        if not sel or sel[0] >= len(self.devices):
            return
        got = self.app._lan_link(self.h, self.idx)
        if not got:
            return
        url, name = got
        import stream
        dev = self.devices[sel[0]]
        self.msg.set("Starting on %s\u2026" % dev.name)

        def work():
            try:
                dev.play(url, name, stream.content_type(name))
                self.box.put(("playing", dev))
            except Exception as e:
                self.box.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _do(self, what):
        dev = self.dev
        if not dev:
            return

        def work():
            try:
                getattr(dev, what)()
                self.box.put(("done", what))
            except Exception as e:
                self.box.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, val = self.box.get_nowait()
                if kind == "scan":
                    self.devices = val
                    self.lb.delete(0, "end")
                    for d in val:
                        self.lb.insert("end", "%s   (%s)" % (d.name, d.kind))
                    if val:
                        self.lb.selection_set(0)
                    self.msg.set("%d device(s) found. Pick one and press Play. The TV pulls the video from this computer, so "
                                 "keep the program running." % len(val) if val else
                                 "No devices found. They must be on the same network and switched on.")
                elif kind == "playing":
                    self.dev = val
                    self.msg.set("Playing on %s. Chromecast cannot play every format (for example some HEVC or MKV audio); "
                                 "DLNA TVs usually can." % val.name)
                elif kind == "done":
                    self.msg.set("Sent: %s" % val)
                elif kind == "err":
                    self.msg.set("Could not cast: %s" % val)
        except queue.Empty:
            pass
        self.after(150, self._poll)


class Toast(tk.Toplevel):
    """A small always-visible message at the bottom-right of the screen ("download complete") with quick actions."""

    def __init__(self, app, title, text, h=None, seconds=14):
        super().__init__(app.root)
        old = getattr(app, "_toast", None)
        try:
            if old is not None and old.winfo_exists():
                old.destroy()
        except Exception:
            pass
        self.app, self.h = app, h
        P = app.P
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.configure(bg=P["border"])
        inner = tk.Frame(self, bg=P["surface"], padx=app.px(16), pady=app.px(12))
        inner.pack(padx=app.px(2), pady=app.px(2))
        tk.Label(inner, text=title, bg=P["surface"], fg=P["fg"], font=app.F.bold, anchor="w").pack(fill="x")
        tk.Label(inner, text=text, bg=P["surface"], fg=P["muted"], font=app.F.ui, anchor="w", justify="left",
                 wraplength=app.px(360)).pack(fill="x", pady=(4, 8))
        row = tk.Frame(inner, bg=P["surface"])
        row.pack(fill="x")
        def btn(label, cmd):
            b = tk.Label(row, text=label, bg=P["border"], fg=P["fg"], font=app.F.ui, padx=app.px(12), pady=app.px(4), cursor="hand2")
            b.bind("<Button-1>", lambda e: cmd())
            b.pack(side="left", padx=(0, 8))
        if h and h in app.mgr.sessions:
            btn("Open folder", self._folder)
            sess = app.mgr.sessions[h]
            import stream
            if hasattr(sess, "t") and any(stream.media_kind(f[0][-1]) for f in sess.t.files):
                btn("Play", self._play)
        btn("Show", lambda: (app.show_window(), self.destroy()))
        btn("Dismiss", self.destroy)
        self.update_idletasks()
        w, ht = self.winfo_reqwidth(), self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("+%d+%d" % (sw - w - app.px(24), sh - ht - app.px(96)))      # above the taskbar
        self.after(seconds * 1000, self._close)

    def _folder(self):
        sess = self.app.mgr.sessions.get(self.h)
        if sess:
            try:
                os.startfile(sess.save_dir)
            except (AttributeError, OSError):
                pass
        self.destroy()

    def _play(self):
        self.app.play(self.h)
        self.destroy()

    def _close(self):
        try:
            self.destroy()
        except tk.TclError:
            pass
