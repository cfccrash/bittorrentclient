"""GUI logic test with a fake tkinter (no display needed). It runs the real App / dialogs / ui code against
in-memory widgets, so it catches logic and wiring errors - it cannot judge how the window looks."""
import os, queue, sys, tempfile, time, types
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class Var:
    def __init__(self, master=None, value=None, **kw):
        self._v = value
    def get(self): return self._v
    def set(self, v): self._v = v
    def trace_add(self, *a): pass


class Widget:
    def __init__(self, *a, **kw):
        self.opts = dict(kw)
    def configure(self, cnf=None, **kw):
        self.opts.update(cnf or {}); self.opts.update(kw)
    config = configure
    def cget(self, k): return self.opts.get(k)
    def __getitem__(self, k): return self.opts.get(k)
    def __setitem__(self, k, v): self.opts[k] = v
    def winfo_width(self): return self.opts.get("_w", 1000)
    def winfo_height(self): return 600
    def winfo_reqwidth(self): return 400
    def winfo_reqheight(self): return 300
    def winfo_rootx(self): return 0
    def winfo_rooty(self): return 0
    def winfo_pointerxy(self): return (5, 5)
    def winfo_fpixels(self, s): return 96.0
    def geometry(self, g=None):
        if g: self.opts['_geo'] = g
        return self.opts.get('_geo', '')
    def winfo_screenwidth(self): return 1366
    def winfo_screenheight(self): return 768
    def winfo_id(self): return 1
    def clipboard_get(self): return self.opts.get("_clip", "")
    def __getattr__(self, name):   # pack / grid / bind / after / title ... are no-ops
        if name.startswith("__"):
            raise AttributeError(name)
        return mock.MagicMock(name=name)


class Treeview(Widget):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rows, self.order, self.sel = {}, [], ()
        self.headings, self.columns, self.tags = {}, {}, {}
    def insert(self, parent, index, iid=None, values=(), tags=(), **kw):
        self.rows[iid] = {"values": tuple(values), "tags": tuple(tags)}; self.order.append(iid); return iid
    def item(self, iid, option=None, **kw):
        if kw:
            self.rows[iid].update({k: (tuple(v) if k in ("values", "tags") else v) for k, v in kw.items()})
        return self.rows[iid]
    def exists(self, iid): return iid in self.rows
    def get_children(self, item=""): return tuple(self.order)
    def delete(self, *ids):
        for i in ids:
            self.rows.pop(i, None)
            if i in self.order: self.order.remove(i)
    def detach(self, *ids):
        for i in ids:
            if i in self.order: self.order.remove(i)
    def index(self, iid): return self.order.index(iid)
    def move(self, iid, parent, index):
        if iid in self.order: self.order.remove(iid)
        self.order.insert(len(self.order) if index == "end" else index, iid)
    def selection(self): return self.sel
    def selection_set(self, *ids):
        self.sel = tuple(ids[0]) if ids and isinstance(ids[0], (list, tuple)) else tuple(ids)
    def bbox(self, iid, col=None): return (10, 10, 120, 40) if iid in self.rows else ""
    def heading(self, col, **kw): self.headings.setdefault(col, {}).update(kw)
    def column(self, col, **kw): self.columns.setdefault(col, {}).update(kw)
    def tag_configure(self, tag, **kw): self.tags[tag] = kw
    def identify_row(self, y): return self.order[0] if self.order else ""


class Text(Widget):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw); self.content = ""
    def insert(self, idx, txt): self.content += txt
    def delete(self, a, b=None): self.content = ""


class Style:
    calls = []
    def __init__(self, *a, **kw): pass
    def theme_use(self, n): Style.calls.append(("theme_use", n))
    def configure(self, name, **kw): Style.calls.append(("configure", name, kw))
    def map(self, name, **kw): Style.calls.append(("map", name, kw))
    def layout(self, name, spec=None): Style.calls.append(("layout", name))
    def element_create(self, name, *a, **kw): Style.calls.append(("element_create", name))


class Font:
    def __init__(self, root=None, **kw): self.kw = kw
    def configure(self, **kw): pass
    def actual(self, k): return "Segoe UI"
    def metrics(self, k=None): return 20
    def measure(self, text): return 8 * len(str(text))


def install():
    tk = types.ModuleType("tkinter"); ttk = types.ModuleType("tkinter.ttk"); tkfont = types.ModuleType("tkinter.font")
    fd = types.ModuleType("tkinter.filedialog"); mb = types.ModuleType("tkinter.messagebox")
    tk.TclError = type("TclError", (Exception,), {})
    tk.EventType = types.SimpleNamespace(Leave="8")
    for n in ("Tk", "Frame", "Label", "Entry", "Toplevel", "Menu", "PhotoImage", "Canvas"):
        setattr(tk, n, type(n, (Widget,), {}))
    tk.Text = Text
    tk.StringVar = tk.BooleanVar = tk.IntVar = Var
    for n in ("Frame", "Label", "Button", "Entry", "Spinbox", "Combobox", "Checkbutton", "Notebook", "LabelFrame",
              "Scrollbar", "Progressbar"):
        setattr(ttk, n, type(n, (Widget,), {}))
    ttk.Treeview, ttk.Style = Treeview, Style
    tkfont.Font = Font
    tkfont.families = lambda root=None: ("Segoe UI", "Segoe Fluent Icons")
    tkfont.nametofont = lambda name: Font()
    fd.askopenfilenames = mock.MagicMock(return_value=()); fd.askdirectory = mock.MagicMock(return_value="")
    for n in ("showerror", "showinfo", "showwarning"):
        setattr(mb, n, mock.MagicMock())
    mb.askyesnocancel = mock.MagicMock(return_value=False)
    mb.askyesno = mock.MagicMock(return_value=True)
    tk.ttk, tk.font, tk.filedialog, tk.messagebox = ttk, tkfont, fd, mb
    sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.font": tkfont,
                        "tkinter.filedialog": fd, "tkinter.messagebox": mb})
    return tk, mb


def main():
    tk, mb = install()
    # ---- the app was renamed: a user's data folder from the old name is carried over, not lost
    mig = tempfile.mkdtemp(); os.environ["APPDATA"] = mig
    old = os.path.join(mig, "PyTorrent"); os.makedirs(os.path.join(old, "torrents"))
    open(os.path.join(old, "state.json"), "w").write('{"theme": "dark", "down_limit": 42, "torrents": []}')
    open(os.path.join(old, "torrents", "x.torrent"), "wb").write(b"d4:infodee")
    import ui, dialogs, bittorrentclient
    assert bittorrentclient.APP == "Bit Torrent Client" and bittorrentclient.APP_DIR == "BitTorrentClient"
    d = bittorrentclient.data_dir()
    assert os.path.basename(d) == "BitTorrentClient" and not os.path.exists(old)
    assert os.path.exists(os.path.join(d, "state.json")) and os.path.exists(os.path.join(d, "torrents", "x.torrent"))
    assert bittorrentclient.data_dir() == d                      # second launch: nothing to migrate, still fine
    os.environ["APPDATA"] = tempfile.mkdtemp()                   # fresh profile for the rest of the test
    from torrent_core import make_torrent, Torrent

    # ---- palettes are complete and consistent
    assert set(ui.LIGHT) == set(ui.DARK), set(ui.LIGHT) ^ set(ui.DARK)
    assert ui.palette("dark") is ui.DARK and ui.palette("light") is ui.LIGHT
    assert ui.fmt_size(1536) == "1.5 KB" and ui.fmt_rate(0) == "-" and ui.fmt_eta(125) == "2m 05s"
    # the fake screen is 1366x768; on Windows work_area() would ask the real desktop instead
    ui.work_area = lambda root: (0, 0, int(root.winfo_screenwidth()), int(root.winfo_screenheight()))

    root = tk.Tk()
    inbox = queue.Queue()
    app = bittorrentclient.App(root, inbox)
    assert not app.F.__dict__.get("missing") and app.F.has_icons
    names = {c[1] for c in Style.calls if c[0] == "configure"}
    for need in ("Treeview", "Treeview.Heading", "Accent.TButton", "TNotebook.Tab", "Down.Horizontal.TProgressbar",
                 "Seed.Horizontal.TProgressbar", "Err.Horizontal.TProgressbar", "Dialog.Treeview"):
        assert need in names, "style not configured: " + need
    assert app.tree.tags["error"]["foreground"] == ui.DARK["bad"]
    assert set(app.btn) == {"add", "magnet", "search", "create", "rss", "library", "restart", "exit", "resume", "pause", "remove", "files", "folder", "settings"}

    # ---- empty state, then a torrent arrives (magnet link forwarded by a second launch)
    app.refresh()
    assert app._empty_shown is True and app.d_name.get().startswith("Select a torrent")
    assert not app.btn["pause"].enabled and not app.btn["remove"].enabled
    h1 = "ab" * 20
    inbox.put(["magnet:?xt=urn:btih:%s&dn=Zeta&tr=udp://127.0.0.1:9/a" % h1])
    app.poll(); time.sleep(0.3); app.refresh()
    assert app._empty_shown is False and h1 in app.tree.rows
    with mock.patch.object(app, "quit") as q:          # `--quit` forwarded by the installer: a clean exit, no prompt
        inbox.put(["--quit"]); app.poll(); q.assert_called_once()
    vals = app.tree.rows[h1]["values"]
    assert vals[0] == "Zeta" and "Fetching metadata" in vals[3], vals
    assert app.bar_kind[h1] == "Check", app.bar_kind

    # ---- more torrents: real .torrent files, sorting, bar kinds, status colours
    tmp = tempfile.mkdtemp()
    for name, size in (("Alpha", 300_000), ("Mike", 50_000)):
        d = os.path.join(tmp, name); os.makedirs(d)
        open(os.path.join(d, "f.bin"), "wb").write(os.urandom(size))
        open(os.path.join(tmp, name + ".torrent"), "wb").write(make_torrent(d, 32768))
        app.add_file(os.path.join(tmp, name + ".torrent"), tmp)   # data absent -> Downloading (nothing to get from)
    time.sleep(0.6); app.refresh()
    assert len(app.tree.rows) == 3
    app.sort_by("name")
    assert [app.tree.rows[i]["values"][0] for i in app.tree.order] == ["Alpha", "Mike", "Zeta"]
    app.sort_by("name")   # toggles to descending
    assert [app.tree.rows[i]["values"][0] for i in app.tree.order] == ["Zeta", "Mike", "Alpha"]
    assert "\u25BC" in app.tree.headings["name"]["text"] and app.tree.headings["size"]["text"] == "Size"
    app.sort_by("size")
    assert app.sort_col == "size" and not app.sort_rev
    app.refresh()   # sorting survives refreshes (rows are re-ordered, not re-created)

    # ---- selection drives buttons and the details panel
    paused = [i for i, s in app._snap_by_hash.items() if s["raw_state"] == "Paused"]
    running = [i for i, s in app._snap_by_hash.items() if s["raw_state"] in bittorrentclient.RUNNING]
    assert running, app._snap_by_hash
    app.tree.selection_set(running[0]); app._on_select()
    assert app.btn["pause"].enabled and app.btn["remove"].enabled and app.btn["folder"].enabled
    assert app.d_name.get() == app._snap_by_hash[running[0]]["name"] and app.d_vars["hash"].get().endswith("\u2026")
    app.pause(); time.sleep(0.5); app.refresh()
    assert app._snap_by_hash[running[0]]["raw_state"] == "Paused"
    assert "paused" in app.tree.rows[running[0]]["tags"] and app.bar_kind[running[0]] == "Idle"
    app._on_select()
    assert app.btn["resume"].enabled and not app.btn["pause"].enabled
    app.toggle_pause(); time.sleep(0.4); app.refresh()          # Space resumes it
    assert app._snap_by_hash[running[0]]["raw_state"] in bittorrentclient.RUNNING
    app.tree.selection_set(tuple(app.tree.order)); app._on_select()
    assert app.d_name.get() == "3 torrents selected"
    app.tree.selection_set(()); app._on_select()
    assert not app.btn["remove"].enabled

    # ---- status bar
    t_end = time.time() + 10
    while not app.mgr.port and time.time() < t_end:   # the listener comes up asynchronously
        time.sleep(0.1)
    app.refresh()
    assert "\u2193" in app.s_speed.opts["text"] and "3 torrents" in app.s_count.opts["text"]
    assert "port" in app.s_net.opts["text"] and "DHT" in app.s_net.opts["text"]
    assert app.s_pf.opts["text"] and app.s_dot.opts["fg"] in list(ui.LIGHT.values()) + list(ui.DARK.values())
    class PM:   # fake port mapper states -> indicator colour
        protocol = "PCP"
        status = status4 = "forwarded TCP 6881, UDP 6881 via PCP (public IP 1.2.3.4)"
    real_pm, app.mgr.portmap = app.mgr.portmap, PM()
    assert app._pf_summary()[:2] == ("Ports forwarded (PCP)", "good")
    PM.status4 = "forwarded TCP 6881 via NAT-PMP (router WAN address 10.0.0.2 is not public: double NAT / CGNAT)"
    assert app._pf_summary()[1] == "warn"
    PM.status4 = "Searching for router..."; assert app._pf_summary()[1] == "warn"
    PM.status4 = "no PCP / NAT-PMP / UPnP router found"; assert app._pf_summary()[1] == "bad"
    app.mgr.portmap = None; assert app._pf_summary()[1] == "muted"
    app.mgr.portmap = real_pm

    # ---- settings: limits, theme switch, persistence
    app.apply_settings(300, 150, "require", "prefer", False, "dark", tmp)
    assert app.mgr.down.rate == 300 * 1024 and app.mgr.encryption == "require" and app.mgr.utp_mode == "prefer"
    assert app.P is ui.DARK and app.tree.tags["paused"]["foreground"] == ui.DARK["muted"]
    assert app.btn["add"].P is ui.DARK and app.btn["magnet"].P is ui.DARK and app.btn["resume"].P is ui.DARK
    assert app.menu.opts["bg"] == ui.DARK["surface"] and app.root.opts["bg"] == ui.DARK["bg"]
    app.apply_settings(0, 0, "prefer", "fallback", False, "light", tmp)
    assert app.P is ui.LIGHT and app.btn["add"].P is ui.LIGHT and app.btn["resume"].P is ui.LIGHT
    import json
    saved = json.load(open(app.state_file))
    assert saved["theme"] == "light" and saved["encryption"] == "prefer" and saved["portmap"] is False

    # ---- toolbar button behaviour: hover, disabled, click
    clicks = []
    b = ui.ToolButton(root, app.F, ui.ICON_ADD, "X", lambda: clicks.append(1), accent=False); b.restyle(ui.LIGHT)
    b._enter(None); assert b.opts.get("bg") == ui.LIGHT["hover"]
    b._press(None); b._release(None); assert clicks == [1]
    b.set_enabled(False); b._press(None); b._release(None); assert clicks == [1] and b.label.opts["fg"] == ui.LIGHT["disabled"]

    # ---- Add dialog: magnet
    d = dialogs.AddDialog(app, "magnet", initial="not a magnet")
    d._ok(); assert d.result is None and "not a valid magnet" in d.err.opts["text"]
    d.link.set("magnet:?xt=urn:btih:" + "cd" * 20 + "&dn=Test"); d.folder.set(os.path.join(tmp, "new", "dir")); d.start.set(False)
    d._ok()
    assert d.result == {"folder": os.path.join(tmp, "new", "dir"), "start": False, "category": "", "skip": [],
                        "link": "magnet:?xt=urn:btih:" + "cd" * 20 + "&dn=Test"} and os.path.isdir(d.result["folder"])
    d = dialogs.AddDialog(app, "magnet", initial="magnet:?xt=urn:btih:" + "cd" * 20); d.folder.set(""); d._ok()
    assert d.result is None and "Choose a folder" in d.err.opts["text"]
    # ---- Add dialog: torrent (contents listed, disk space check)
    raw = open(os.path.join(tmp, "Alpha.torrent"), "rb").read()
    d = dialogs.AddDialog(app, "torrent", [("Alpha.torrent", raw, Torrent(raw))]); d.folder.set(tmp); d._update_space()
    assert "Free space" in d.space.opts["text"] and "Needs: 293.0 KB" in d.space.opts["text"], d.space.opts["text"]
    d._ok(); assert d.result["folder"] == tmp and d.result["start"] is True and d.result["link"] is None
    # the full flow: dialog result -> torrent added paused
    other = os.path.join(tmp, "Mike2"); os.makedirs(other); open(os.path.join(other, "g.bin"), "wb").write(os.urandom(40_000))
    p2 = os.path.join(tmp, "Mike2.torrent"); open(p2, "wb").write(make_torrent(other, 16384))
    with mock.patch.object(dialogs.AddDialog, "show", return_value={"folder": tmp, "start": False, "link": None}):
        tk.filedialog.askopenfilenames.return_value = (p2,)
        app.add_dialog()
    h2 = Torrent(open(p2, "rb").read()).hex
    time.sleep(0.2); app.refresh()
    assert app._snap_by_hash[h2]["raw_state"] == "Paused" and "paused" in app.tree.rows[h2]["tags"]
    with mock.patch.object(dialogs.AddDialog, "show", return_value={"folder": tmp, "start": True,
                           "link": "magnet:?xt=urn:btih:" + "ef" * 20 + "&dn=M&tr=udp://127.0.0.1:9/x"}):
        app.add_magnet_dialog("magnet:?xt=urn:btih:" + "ef" * 20)
    assert ("ef" * 20) in app.mgr.sessions
    bad = os.path.join(tmp, "broken.torrent"); open(bad, "wb").write(b"not bencode")
    tk.messagebox.showerror.reset_mock(); app.add_torrents([bad])
    assert tk.messagebox.showerror.called, "unreadable torrent must be reported"

    # ---- Settings dialog: valid and invalid input
    sd = dialogs.SettingsDialog(app)
    sd.down.set("250"); sd.up.set("100"); sd.theme.set("Aurora (dark)"); sd.enc.set("Require full encryption"); sd.utp.set("Prefer uTP")
    sd._ok()
    assert sd.result["down_limit"] == 250 and sd.result["theme"] == "dark" and sd.result["encryption"] == "require" \
        and sd.result["utp"] == "prefer" and sd.result["portmap"] is False
    sd2 = dialogs.SettingsDialog(app); sd2.down.set("fast"); tk.messagebox.showerror.reset_mock(); sd2._ok()
    assert sd2.result is None and tk.messagebox.showerror.called
    assert sd.result["port"] == app.cfg["port"] and 20000 <= app.cfg["port"] <= 40000
    sd5 = dialogs.SettingsDialog(app); sd5.port.set("80"); tk.messagebox.showerror.reset_mock(); sd5._ok()
    assert sd5.result is None and tk.messagebox.showerror.called
    sd6 = dialogs.SettingsDialog(app); sd6.port.set("51234"); sd6._ok(); assert sd6.result["port"] == 51234
    sd3 = dialogs.SettingsDialog(app); sd3.up.set("-5"); sd3._ok(); assert sd3.result is None

    # ---- diagnostics dialog, firewall box and hints
    import firewall
    with mock.patch.object(firewall, "check", return_value="blocked"), mock.patch.object(firewall, "available", return_value=True):
        app.fw_state = None
        assert "BLOCKING" in app.fw_text() and app.fw_state == "blocked"
        assert any("Firewall" in h for h in app.hints())
        sd4 = dialogs.SettingsDialog(app)
    with mock.patch.object(firewall, "add_rule", return_value=False):
        tk.messagebox.showinfo.reset_mock(); app.fix_firewall(); assert tk.messagebox.showinfo.called
    with mock.patch.object(firewall, "add_rule", return_value=True):
        app.fix_firewall(); assert app.fw_state is None
    report = app.diagnostics_text()
    assert "Windows Firewall:" in report
    dd = dialogs.DiagnosticsDialog(app)
    assert dd.report == report or "Windows Firewall:" in dd.report
    assert "Windows Firewall:" in dd.text.content
    dd._rescan(); dd._copy()
    app.mgr.set_portmap(False)
    assert "turned off" in app.diagnostics_text()

    # ---- opens centred, sized to fit the screen, info bar packed first against the bottom
    geo = app.root.geometry()
    assert geo, "window geometry was never set"
    import re as _re
    w, h, x, y = map(int, _re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo).groups())
    assert w <= 1366 and h <= 768 and abs(x - (1366 - w) // 2) <= 1 and abs(y - (768 - h) // 2) <= 1, geo

    # ---- choosing files, queue menu, notifications
    import tray as tray_mod2
    multi = os.path.join(tmp, "Multi"); os.makedirs(os.path.join(multi, "a"))
    for rel, size in (("a/x.bin", 40_000), ("a/y.bin", 30_000), ("z.bin", 20_000)):
        open(os.path.join(multi, rel), "wb").write(os.urandom(size))
    mraw = make_torrent(multi, 16384); mt = Torrent(mraw)
    names = ["/".join(f[0][1:]) for f in mt.files]
    fd = dialogs.FilesDialog(app, mt, skip={names.index("z.bin")})
    assert fd.state("d:/a") == "on" and fd.state("f%d" % names.index("z.bin")) == "off"
    assert "2 of 3 files" in fd.summary.opts["text"], fd.summary.opts["text"]
    fd.toggle("d:/a"); assert fd.on == set() and fd.state("d:/a") == "off"        # a folder unticks everything inside
    fd._ok(); assert fd.result is None and "at least one" in fd.err.opts["text"]
    fd.toggle("f%d" % names.index("a/x.bin")); assert fd.state("d:/a") == "part"
    fd.toggle("d:/a"); assert fd.state("d:/a") == "on"                             # partly ticked -> tick all
    fd.set_all(True); fd.toggle("f%d" % names.index("a/y.bin")); fd._ok()
    assert fd.result == {names.index("a/y.bin")}, fd.result
    d2 = dialogs.AddDialog(app, "torrent", [("Multi.torrent", mraw, mt)]); d2.folder.set(tmp)
    with mock.patch.object(dialogs.FilesDialog, "show", return_value={names.index("z.bin")}):
        d2._choose_files()
    assert "2 of 3 files" in d2.files_info.opts["text"]; d2._ok()
    assert d2.result["skip"] == [names.index("z.bin")]
    app._add_raw(mraw, tmp, start=False, skip=d2.result["skip"])
    assert app.mgr.sessions[mt.hex].skip == {names.index("z.bin")}
    app.save()
    entry = [e for e in app.cfg["torrents"] if e["hash"] == mt.hex][0]
    assert entry["skip"] == [names.index("z.bin")] and entry["paused"] is True
    orders = [app.mgr.sessions[e["hash"]].order for e in app.cfg["torrents"]]
    assert orders == sorted(orders), "saved in queue order"
    time.sleep(0.2); app.refresh()
    app.tree.selection_set(mt.hex)
    with mock.patch.object(dialogs.FilesDialog, "show", return_value={names.index("a/x.bin")}):
        app.files_dialog()
    assert app.mgr.sessions[mt.hex].skip == {names.index("a/x.bin")}
    app.tree.selection_set(h1)                                                     # a magnet without metadata yet
    tk.messagebox.showinfo.reset_mock(); app.files_dialog(); assert tk.messagebox.showinfo.called
    app.tree.selection_set(mt.hex)
    first = app.mgr.sessions[mt.hex].order
    app.move_in_queue("top"); assert app.mgr.sessions[mt.hex].order == 0
    sd7 = dialogs.SettingsDialog(app); sd7.max_active.set("2"); sd7.notify.set(False); sd7._ok()
    assert sd7.result["max_active"] == 2 and sd7.result["notify"] is False
    sd8 = dialogs.SettingsDialog(app); sd8.max_active.set("-1"); tk.messagebox.showerror.reset_mock(); sd8._ok()
    assert sd8.result is None and tk.messagebox.showerror.called
    app.apply_settings(**sd7.result)
    assert app.mgr.max_active == 2 and app.cfg["notify"] is False
    app.apply_settings(**dict(sd7.result, max_active=0, notify=True)); assert app.mgr.max_active == 0
    real_tray = app.tray
    app.tray = types.SimpleNamespace(notify=mock.MagicMock(return_value=True), temporary=True, hide=mock.MagicMock(),
                                     visible=False, set_tip=lambda t: None)
    with mock.patch.object(tray_mod2, "available", return_value=True):
        app.finished.put(("none", "Some.Linux.ISO")); app.poll()
        assert app.tray.notify.call_args[0] == ("Download complete", "Some.Linux.ISO\nhas finished downloading."), app.tray.notify.call_args
        app.cfg["notify"] = False; app.tray.notify.reset_mock()
        app.finished.put(("none", "Quiet")); app.poll(); assert not app.tray.notify.called     # setting off: no notification
        app.cfg["notify"] = True
        app.notify_finished(["A", "B"]); assert "2 downloads" in app.tray.notify.call_args[0][1]
        app.root.state = mock.MagicMock(return_value="normal"); app._hide_temp_tray(); assert app.tray.hide.called
    app.tray = real_tray

    # ---- start with Windows / start hidden in the tray
    import autostart as autostart_t
    assert "--minimized" in autostart_t.command()
    with mock.patch.object(autostart_t.sys, "platform", "linux"):   # never touch the real Run key from a test
        assert autostart_t.available() is False and autostart_t.is_enabled() is False and autostart_t.set_enabled(True) is False
    with mock.patch.object(autostart_t, "available", return_value=True), \
            mock.patch.object(autostart_t, "is_enabled", return_value=False), \
            mock.patch.object(autostart_t, "set_enabled") as set_en:
        app.apply_settings(**dict(sd7.result, max_active=0, notify=True, autostart=True)); set_en.assert_called_once_with(True)
    ht = types.SimpleNamespace(show=mock.MagicMock(return_value=True))
    real_tray2, app.tray = app.tray, ht
    app.root.withdraw = mock.MagicMock()
    with mock.patch.object(tray_mod2, "available", return_value=True):
        app.start_hidden(); assert ht.show.called and app.root.withdraw.called
        app.root.withdraw.reset_mock(); app.cfg["tray"] = False; app.start_hidden(); assert not app.root.withdraw.called
        app.cfg["tray"] = True
    app.tray = real_tray2

    # ---- sidebar filters, search, details tabs, speed graph, drag and drop
    import panels as panels_t
    row = lambda state, name="x", err=None: dict(raw_state=state, name=name, error=err)
    assert panels_t.matches_filter("down", row("Checking")) and panels_t.matches_filter("seed", row("Finished"))
    assert panels_t.matches_filter("queued", row("Queued")) and not panels_t.matches_filter("paused", row("Queued"))
    assert panels_t.matches_filter("error", row("Downloading", err="disk full")) and not panels_t.matches_filter("down", row("Downloading", err="x"))
    assert panels_t.matches_search("  ALP ", row("Downloading", "Alpha")) and not panels_t.matches_search("zzz", row("Seeding", "Alpha"))
    assert panels_t.count_filters([row("Seeding"), row("Paused"), row("Paused")]) == dict(all=3, down=0, seed=1, queued=0, paused=2, error=0)
    app.refresh(); shown_all = len(app.tree.order); assert shown_all >= 3
    app.set_filter("paused")
    paused_rows = [h for h in app.tree.order]
    assert paused_rows and all(app._snap_by_hash[h]["raw_state"] == "Paused" for h in paused_rows), paused_rows
    assert len(paused_rows) < shown_all and app._hidden
    app.set_filter("all"); assert len(app.tree.order) == shown_all and not app._hidden
    app.search.set("alph"); app.refresh_now()
    assert [app._snap_by_hash[h]["name"] for h in app.tree.order] == ["Alpha"], [app._snap_by_hash[h]["name"] for h in app.tree.order]
    app.search.set(""); app.refresh_now(); assert len(app.tree.order) == shown_all
    app.refresh(); assert app.side_rows["all"][2].opts["text"] == str(shown_all)
    # details tabs
    app.tree.selection_set(mt.hex); app.refresh()
    for name, expect in (("Files", 3), ("Trackers", 0)):
        app._tab = name; app._update_tabs()
        assert len(app.tab_trees[name].rows) == expect, (name, app.tab_trees[name].rows)
    app._tab = "Files"; app._update_tabs()
    assert sorted(v["values"][3] for v in app.tab_trees["Files"].rows.values()).count("Skipped") == 1
    app._tab = "Peers"; app._update_tabs(); assert app.tab_trees["Peers"].rows == {}
    app.tree.selection_set(()); app.refresh(); app._tab = "Files"; app._update_tabs(); assert app.tab_trees["Files"].rows == {}
    app._tab = "Speed"; app.refresh(); assert app.graph.hist[None] and app.graph.hist[mt.hex]
    app.speed_canvas.create_line = mock.MagicMock(); app.speed_canvas.delete = mock.MagicMock(); app.speed_canvas.create_text = mock.MagicMock()
    sg = panels_t.SpeedGraph()
    for i in range(10): sg.push("k", 1000 * i, 500 * i)
    sg.draw(app.speed_canvas, "k", "Test", app.P, app.px)
    assert app.speed_canvas.delete.called and app.speed_canvas.create_line.call_count >= 5 and app.speed_canvas.create_text.called
    sg.forget(set()); assert "k" not in sg.hist
    assert panels_t._nice_ceiling(1) == 10_000.0 and panels_t._nice_ceiling(3_000_000) == 5e6
    app._tab = "Overview"
    adds = []
    with mock.patch.object(app, "add_torrents", side_effect=adds.append), mock.patch.object(app, "show_window"):
        app.on_drop([os.path.join(tmp, "Alpha.torrent"), "C:/x/readme.txt"]); assert adds == [[os.path.join(tmp, "Alpha.torrent")]]
        tk.messagebox.showinfo.reset_mock(); app.on_drop(["C:/x/readme.txt"]); assert tk.messagebox.showinfo.called

    # ---- settings: sections listed down the left, the chosen page shown beside them
    sdn = dialogs.SettingsDialog(app)
    # a drop-down: only the section list at first; a section's page cascades beside it, clicking it again folds it
    assert list(sdn.pages) == ["General", "Media", "Downloads", "Speed", "Connection", "About"] and sdn.current_page is None
    sdn.show_page("Connection"); assert sdn.current_page == "Connection"
    assert sdn.nav_rows["Connection"].opts["bg"] == app.P["sel"] and sdn.nav_rows["General"].opts["bg"] == app.P["bg"]
    sdn.show_page("Connection"); assert sdn.current_page is None

    # ---- restart / exit buttons
    with mock.patch.object(app, "quit") as q_mock, mock.patch.object(app.mgr, "snapshot", return_value=[]):
        app.exit_app(); assert q_mock.call_count == 1
        app.restart(); assert q_mock.call_count == 2 and app.restart_requested is True
        app.restart_requested = False
    busy = [dict(raw_state="Downloading")]
    with mock.patch.object(app, "quit") as q_mock, mock.patch.object(app.mgr, "snapshot", return_value=busy):
        tk.messagebox.askyesno.return_value = False; app.exit_app(); assert not q_mock.called   # asked, and declined
        tk.messagebox.askyesno.return_value = True; app.exit_app(); assert q_mock.called
        assert not getattr(app, "restart_requested", False)

    # ---- close button hides to the tray (and quits when the setting is off or no tray exists)
    import tray as tray_mod
    class FakeTray:
        visible = False; shown = []
        def show(self, balloon=None): FakeTray.visible = True; FakeTray.shown.append(balloon); return True
        def hide(self): FakeTray.visible = False
        def destroy(self): FakeTray.visible = False
        def set_tip(self, t): pass
    app.tray = FakeTray()
    with mock.patch.object(tray_mod, "available", return_value=True), mock.patch.object(app.mgr, "shutdown") as sd_mock:
        app.cfg["tray"] = True
        app.root.destroy = mock.MagicMock(); app.root.withdraw = mock.MagicMock(); app.root.deiconify = mock.MagicMock()
        app.close()
        assert FakeTray.visible and app.root.withdraw.called and not sd_mock.called and not app.root.destroy.called
        assert FakeTray.shown[0] and "background" in FakeTray.shown[0][1]       # first time: tells the user
        app.show_window(); assert not FakeTray.visible and app.root.deiconify.called
        app.close(); assert FakeTray.shown[-1] is None                          # later: no repeat balloon
        app.cfg["tray"] = False
        app.close(); assert sd_mock.called and app.root.destroy.called           # setting off: really quits
        app.cfg["tray"] = True
    with mock.patch.object(tray_mod, "available", return_value=False), mock.patch.object(app.mgr, "shutdown") as sd2:
        app.close(); assert sd2.called                                          # no tray on this OS: quits
    assert dialogs.SettingsDialog(app).tray.get() is True

    # ---- removing torrents clears rows and bars
    app.refresh()   # pick up the magnet added through the dialog
    app.tree.selection_set(tuple(app.tree.order)); app.remove(); time.sleep(0.4); app.refresh()
    assert not app.tree.rows and not app.progress and app._empty_shown is True

    # ---- every column (headings and cell text) is left-aligned
    assert {c["anchor"] for c in app.tree.columns.values()} == {"w"} and len(app.tree.columns) == 8
    assert {h["anchor"] for h in app.tree.headings.values()} == {"w"}

    # ---- equal columns at any width, never below the minimum
    app.tree.opts["_w"] = 1000; app._on_resize()
    w = {c: v["width"] for c, v in app.tree.columns.items()}
    assert abs(sum(w.values()) - 1000) <= 8 and w["name"] == max(w.values()) and w["name"] > 2.5 * w["eta"]
    assert w["eta"] < w["status"] and w["peers"] <= w["up"] <= w["status"] and w["down"] == w["up"]
    app.tree.opts["_w"] = 300; app._on_resize()
    assert {c: v["width"] for c, v in app.tree.columns.items()} == {c: app.COL_MIN[c] for c in app.cols}
    # ---- online search window logic (sources, threading queue, sorting, adding)
    import search as search_mod
    from dialogs import SearchWindow
    calls = []
    src = search_mod.Source("Fake", lambda q, n: [
        {"name": "B small", "size": 10, "seeders": 2, "magnet": "magnet:?xt=urn:btih:" + "b" * 40},
        {"name": "A big", "size": 99, "seeders": 50, "torrent_url": "http://x/y.torrent"},
        {"name": "no link"}])
    sw = SearchWindow.__new__(SearchWindow)
    sw.app, sw.sources, sw.q, sw.results, sw._token, sw._sort = app, [src], queue.Queue(), [], 0, ("seeds", True)
    sw.tree, sw.status, sw.query, sw.source = Treeview(), Var(value=""), Var(value="  linux "), Var(value="Fake")
    sw.after = lambda ms, fn=None: None
    sw.winfo_exists = lambda: True
    sw._sig = sw._plugin_sig()
    sw.run()
    for _ in range(50):
        try:
            tok, kind, payload = sw.q.get(timeout=0.2); break
        except queue.Empty:
            pass
    assert kind == "ok" and len(payload) == 2 and payload[0]["source"] == "Fake", (kind, payload)
    sw.q.put((tok, kind, payload)); sw._poll()
    assert [sw.tree.rows[i]["values"][0] for i in sw.tree.get_children()] == ["A big", "B small"]   # by seeds desc
    sw.sort("name"); assert [sw.tree.rows[i]["values"][0] for i in sw.tree.get_children()] == ["A big", "B small"]
    sw.sort("size"); assert sw.tree.rows[sw.tree.get_children()[0]]["values"][0] == "A big"
    sw.q.put((sw._token, "err", "Fake: boom")); sw._poll()
    assert "failed" in sw.status.get() and "boom" in sw.status.get()
    app.add_magnet_dialog = lambda initial="": calls.append(("magnet", initial))
    app.add_torrents = lambda paths: calls.append(("torrent", open(paths[0], "rb").read()))
    sw.tree.sel = (sw.tree.get_children()[1],)
    sw.download(); assert calls and calls[0][0] == "magnet" and calls[0][1].startswith("magnet:?xt=urn:btih:b")
    sw._downloaded(b"d4:infod4:name1:xee"); assert calls[-1] == ("torrent", b"d4:infod4:name1:xee")
    pd = tempfile.mkdtemp(dir=tmp)
    os.makedirs(os.path.join(pd, "search_plugins"))
    open(os.path.join(pd, "search_plugins", "good.py"), "w").write(
        'NAME = "Good"\ndef search(q, limit=50):\n    return [{"name": q, "seeders": "5", "magnet": "magnet:?xt=urn:btih:" + "a" * 40}, {"name": "nolink"}]\n')
    open(os.path.join(pd, "search_plugins", "bad.py"), "w").write('raise RuntimeError("boom")\n')
    srcs, errs = search_mod.load_sources(pd)
    assert [x.name for x in srcs] == ["Internet Archive", "Good"] and len(errs) == 1 and "boom" in errs[0]
    got = srcs[1].search("hi"); assert len(got) == 1 and got[0]["seeders"] == 5 and got[0]["source"] == "Good"
    assert os.path.exists(os.path.join(pd, "search_plugins", "_example.py"))
    real = search_mod.http_json
    search_mod.http_json = lambda u, timeout=15: {"response": {"docs": [{"identifier": "a b", "title": ["T"], "item_size": "1234"}, {"title": "x"}]}}
    ia = search_mod.archive_search('foo "bar": baz')
    search_mod.http_json = real
    assert ia == [{"name": "T", "size": 1234, "seeders": None, "leechers": None,
                   "torrent_url": "https://archive.org/download/a%20b/a%20b_archive.torrent",
                   "page": "https://archive.org/details/a%20b"}], ia
    print("online search window logic: OK")

    app.mgr.shutdown()
    print("GUI logic test: ALL PASSED")


if __name__ == "__main__":
    main()
