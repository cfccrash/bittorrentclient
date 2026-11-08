"""Custom window frames: strip, buttons, maximise/restore, resize grips, themed message boxes. Needs Tk + a display."""
import os, sys, tempfile, time, types
os.environ["BTC_CHROME"] = "1"
os.environ["APPDATA"] = tempfile.mkdtemp()   # never touch the user's real data folder / error.log
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import tkinter as tk
except ImportError:
    print("chrome test: SKIPPED (no tkinter)"); sys.exit(0)
import bittorrentclient as B, dialogs, chrome, ui


def main():
    import queue
    root = tk.Tk()
    app = B.App(root, queue.Queue())
    root.update()
    t_end = time.time() + 0.3   # on Windows chrome._after_map withdraws + re-shows the window ~50 ms after start;
    while time.time() < t_end:  # let that finish, or it restores the old size over the resize below
        root.update(); time.sleep(0.02)
    c = app.chrome
    if os.name == "nt":
        # the main window keeps a real Windows frame behind the Aurora strip (Snap Layouts, Win+arrows, edge snapping)
        import ctypes
        from ctypes import wintypes
        assert c and c.native and c.native.hwnd and not root.overrideredirect(), "main window should have a native frame"
        u = ctypes.windll.user32
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        r = wintypes.RECT(); u.GetWindowRect(c.native.hwnd, ctypes.byref(r))
        hit = lambda x, y: u.SendMessageW(c.native.hwnd, 0x84, 0, (y & 0xFFFF) << 16 | (x & 0xFFFF))   # WM_NCHITTEST
        mid_y = r.top + c.h // 2
        assert hit((r.left + r.right) // 2, mid_y) == 2, "title strip must be the caption (drag, snap, double-click)"
        assert hit(r.right - c.bw - c.bw // 2, mid_y) == 9, "our maximise button must be HTMAXBUTTON (Snap Layouts)"
        assert hit(r.right - c.bw // 2, mid_y) == 1, "close stays a Tk button"
        assert hit(r.left + 1, (r.top + r.bottom) // 2) == 10 and hit((r.left + r.right) // 2, (r.top + r.bottom) // 2) == 1
        # what a REAL pointer finds: Windows asks the innermost window first, so the strip's child windows must let the
        # question through over the max button (1.9.25 answered the frame directly but the canvas swallowed it)
        u.WindowFromPoint.restype = wintypes.HWND
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        root.attributes("-topmost", True); root.update()
        assert u.WindowFromPoint(wintypes.POINT(r.right - c.bw - c.bw // 2, mid_y)) == c.native.hwnd, "max button must reach the frame"
        assert u.WindowFromPoint(wintypes.POINT((r.left + r.right) // 2, mid_y)) == c.bar.winfo_id()
        root.attributes("-topmost", False)
        # a lone Alt must not park the app in the (hidden) system-menu mode
        assert u.SendMessageW(c.native.hwnd, 0x0112, 0xF100, 0) == 0
        c.maximize(); root.update()
        assert c.maxed and u.IsZoomed(c.native.hwnd)
        c.restore(); root.update()
        assert not c.maxed and not u.IsZoomed(c.native.hwnd)
        mw, mh = root.minsize()
        assert mw <= app.px(700) and mh <= app.px(400), "small enough to snap side by side / into quarters"
    else:
        assert c and root.overrideredirect(), "main window not frameless"
        g0 = root.geometry()
        c.maximize(); root.update(); assert c.maxed
        c.restore(); root.update(); assert not c.maxed and root.geometry() == g0, (root.geometry(), g0)
        # resize grip drag
        w0 = root.winfo_width()
        c._rs_start(type("E", (), dict(x_root=0, y_root=0))(), "e")
        c._rs_move(type("E", (), dict(x_root=80, y_root=0))()); root.update()
        assert root.winfo_width() == w0 + 80, (root.winfo_width(), w0)
    # toolbar: the part over the sidebar is exactly as wide as the sidebar column below it
    root.update()
    # toolbar: Exit / Restart / Settings sit over the column, the torrent actions start where the list starts
    app._draw_toolbar(); colw = app._side.winfo_width()
    assert all(0 <= app.btn[k].x0 < app.btn[k].x1 <= colw for k in ("exit", "restart", "settings")), colw
    assert app.btn["resume"].x0 >= colw and app.btn["folder"].x1 < app.toolbar.winfo_width()
    # one picture behind the whole window: title strip, toolbar, column, list and status bar all draw it
    for c in (app.chrome.bar, app.toolbar, app.side_cv, app.statusbar):
        assert any(c.type(i) == "image" for i in c.find_all()), c
    # dialogs get a strip and a close button
    dlg = dialogs.AboutDialog(app); root.update()
    assert dlg.chrome and dlg.overrideredirect()
    dlg.chrome.close(); root.update()
    assert not dlg.winfo_exists()
    # themed message box
    import tkinter.messagebox as mb
    root.after(200, lambda: [w for w in root.winfo_children() if isinstance(w, dialogs.MessageDialog)][0]._pick(True))
    assert mb.askyesno("T", "Really?") is True
    root.after(200, lambda: [w for w in root.winfo_children() if isinstance(w, dialogs.MessageDialog)][0]._pick(None))
    assert mb.askyesnocancel("T", "Really?") is None
    # theme is Aurora
    assert app.P["bg"] == ui.DARK["bg"] or app.cfg.get("theme") != "dark"
    root.update(); os.system("import -window root /tmp/chrome_main.png 2>/dev/null")
    dlg = dialogs.AboutDialog(app); dlg.show = lambda: None; root.update(); os.system("import -window root /tmp/chrome_about.png 2>/dev/null"); dlg.destroy()
    app.chrome.set_title("x"); app.chrome.restyle(app.P)
    # dialog opened from the (non-modal) Search window must come up mapped, grabbing input, and be closable
    import search
    sw = dialogs.SearchWindow(app, [type("S", (), {"name": "X"})()], []); root.update()
    got = {}
    def probe():
        d = [w for w in root.winfo_children() if isinstance(w, dialogs.AddDialog)][0]
        got["mapped"] = d.winfo_ismapped(); got["grab"] = d.grab_current() is not None
        d.destroy()
    root.after(300, probe)
    app.add_magnet_dialog("magnet:?xt=urn:btih:" + "a" * 40)
    assert got == {"mapped": True, "grab": True}, got
    # a plugin dropped into the folder while the Search window is open shows up without a restart
    import search as _s
    d = _s.plugin_dir(app.dir)
    open(os.path.join(d, "late_plugin.py"), "w").write('NAME = "Late"\ndef search(q, limit=1000):\n    return []\n')
    sw.reload_sources(); assert "Late" in sw.combo.cget("values"), sw.combo.cget("values")
    os.remove(os.path.join(d, "late_plugin.py"))
    sw.destroy()
    app.empty.update(); root.update(); os.system("import -window root /tmp/chrome_empty.png 2>/dev/null")
    # tooltips never outlive the pointer / the window
    # the column's buttons are drawn on its canvas (no boxes): one tooltip, placed under the pointed-at button
    tt = app._side_tip
    app.side_cv.update()
    add = app.btn["add"]; app._side_hover = add
    r = tt.region(); assert r and r[3] - r[1] == add.y1 - add.y0 > 0, r
    assert app._side_item_at(types.SimpleNamespace(y=(add.y0 + add.y1) // 2)) is add
    assert app._side_item_at(types.SimpleNamespace(y=app._side_hits[0][0] + 1)) == app._side_hits[0][2]
    tt.delay = 1; tt.text = "tip"; tt._inside = lambda: True
    tt._show(); root.update(); assert tt.tip is not None
    ui.Tooltip.hide_all(); assert tt.tip is None
    tt._show(); tt._inside = lambda: False; root.update(); time.sleep(0.3); root.update(); assert tt.tip is None, "watchdog hides a stranded tooltip"
    # details pane: the drawn tab row (over the artwork) switches the Notebook pages, the Player tab idles on the art
    root.update(); app._draw_details(); root.update()
    hits = list(app._tab_hits)
    assert [app.tabs.tab(i, "text") for *_, i in hits] == ["Overview", "Files", "Peers", "Trackers", "Log", "Speed", "Player"]
    for x0, x1, i in hits:
        app.tabstrip.event_generate("<Button-1>", x=(x0 + x1) // 2, y=app.tabstrip.winfo_height() // 2); root.update()
        assert app.tabs.index(app.tabs.select()) == i and app._tab == app.tabs.tab(i, "text"), (i, app._tab)
    assert isinstance(app._player_hint, tk.Canvas) and app._player_hint.find_all()

    # canvas tables (arttable): Treeview-like behaviour, drawn over the artwork
    import arttable
    top = tk.Toplevel(root); top.geometry("400x300+0+0")
    t = arttable.ArtTable(top, columns=("a", "b"), rowheight=20, head_height=20, palette=lambda: app.P, font=app.F.ui,
                          head_font=app.F.small, draw_bg=lambda c: c.create_rectangle(0, 0, 5, 5, tags=("art",)))
    t.pack(fill="both", expand=True); root.update()
    fired, scrolled = [], []
    t.bind("<<TreeviewSelect>>", lambda e: fired.append(t.selection()))
    t.configure(yscrollcommand=lambda a, b: scrolled.append((float(a), float(b))))
    for i in range(40):
        t.insert("", "end", iid="r%d" % i, values=("row %d" % i, i))
    root.update()
    assert t.find_withtag("art") and t.get_children()[0] == "r0", "draws the background and keeps the rows"
    t.event_generate("<Button-1>", x=50, y=20 + 2 * 20 + 5); root.update()          # third row
    assert t.selection() == ("r2",) and fired[-1] == ("r2",)
    t.event_generate("<Shift-Button-1>", x=50, y=20 + 4 * 20 + 5); root.update()
    assert t.selection() == ("r2", "r3", "r4")
    t.event_generate("<Control-Button-1>", x=50, y=20 + 3 * 20 + 5); root.update()
    assert t.selection() == ("r2", "r4")
    t.focus_force(); t.event_generate("<Down>"); root.update()
    assert t.selection() == ("r5",)
    t.yview("scroll", 10, "units"); root.update()
    assert t.identify_row(25) == "r10" and scrolled[-1][0] == 10 / 40 and t.bbox("r0") == ""
    t.event_generate("<End>"); root.update()
    assert t.selection() == ("r39",) and t.bbox("r39")
    t.detach("r39"); assert "r39" not in t.get_children() and t.exists("r39") and t.selection() == ()
    t.move("r39", "", 0); t.delete("r1"); root.update()
    assert t.get_children()[:2] == ("r39", "r0") and not t.exists("r1")
    assert t.find_all(), "a redraw must not wipe the rows (Canvas.delete vs row delete)"
    t.configure(height=6); assert t.cget("height") == 6
    # hover outline follows the pointer; painters draw their column instead of its text
    t.event_generate("<Motion>", x=50, y=20 + 25); root.update(); assert t._hover_row == t.identify_row(45)
    t.event_generate("<Leave>"); root.update(); assert t._hover_row is None
    painted = []
    t.painters["b"] = lambda c, iid, x, y, w, h, on: painted.append(iid)
    t.redraw(); root.update(); assert painted and painted[0] == t.get_children()[t._top]
    top.destroy()
    # main list: progress bars are canvas items now (no widgets), one per visible row, in the state colour
    assert not [w for w in app.tree.winfo_children() if w.winfo_class() == "TProgressbar"]
    # each theme gets its own artwork (light one generated by tools/make_light_backdrop.py)
    dark_art = app._art(); app.P = ui.palette("light"); light_art = app._art(); app.P = ui.palette("dark")
    assert dark_art is not None and light_art is not None and dark_art is not light_art
    # the main list keeps the artwork full size: with rows present the table itself draws it behind them
    assert app.tree.get_children() == () or not app.empty.winfo_ismapped()
    # Search window: "All sources" searches every source, merges as each one answers, reports the ones that fail
    import search as _srch
    mk = lambda h, n: {"name": n, "magnet": "magnet:?xt=urn:btih:" + h * 20, "seeders": 1}
    def slow(q, limit):
        time.sleep(0.3); return [mk("22", "both"), mk("33", "only two")]
    def broken(q, limit):
        raise OSError("site down")
    fake = [_srch.Source("One", lambda q, limit: [mk("11", "only one"), mk("22", "both")]), _srch.Source("Two", slow),
            _srch.Source("Broken", broken)]
    sw = dialogs.SearchWindow(app, fake, []); root.update()
    assert sw.combo.cget("values")[0] == _srch.ALL, sw.combo.cget("values")
    sw.source.set(_srch.ALL); sw.query.set("x"); sw.unlimited.set(True); sw.run()
    t_end = time.time() + 10
    while (sw._all is None or sw._all["answered"] < 3) and time.time() < t_end:
        root.update(); time.sleep(0.05)
    assert sorted(r["name"] for r in sw.results) == ["both", "only one", "only two"], sw.results
    assert len(sw.tree.get_children()) == 3 and "from 2 sources" in sw.status.get() and "Broken (site down)" in sw.status.get(), sw.status.get()
    assert app.cfg["search_source"] == _srch.ALL and app.cfg["search_no_limit"] is True
    sw.destroy()

    # tray icon setup must not fault: a truncated 64-bit module handle made RegisterClassW / CreateWindowExW raise an
    # access violation that Windows swallowed but faulthandler logged on every start (45 dumps in a user's error.log)
    import tray, subprocess
    if tray.available():
        code = ("import faulthandler, sys, tempfile, os; sys.path.insert(0, %r); p = os.path.join(tempfile.mkdtemp(), 'fh');"
                "f = open(p, 'w'); faulthandler.enable(f, all_threads=True); import tray; t = tray.Tray(lambda: 0, lambda: 0);"
                "t._setup(); assert t._hwnd; f.flush(); print('DUMP' if open(p).read().strip() else 'CLEAN')"
                % os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert r.stdout.strip() == "CLEAN", (r.stdout, r.stderr[-500:])
    print("CHROME TEST: ALL PASSED")
    root.destroy()

main()
