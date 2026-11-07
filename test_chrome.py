"""Custom window frames: strip, buttons, maximise/restore, resize grips, themed message boxes. Needs Tk + a display."""
import os, sys, tempfile, time
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
    assert app.chrome and root.overrideredirect(), "main window not frameless"
    c = app.chrome
    g0 = root.geometry()
    c.maximize(); root.update(); assert c.maxed
    c.restore(); root.update(); assert not c.maxed and root.geometry() == g0, (root.geometry(), g0)
    # resize grip drag
    w0 = root.winfo_width()
    c._rs_start(type("E", (), dict(x_root=0, y_root=0))(), "e")
    c._rs_move(type("E", (), dict(x_root=80, y_root=0))()); root.update()
    assert root.winfo_width() == w0 + 80, (root.winfo_width(), w0)
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
    tw = app.btn["add"]
    tt = [t for t in ui.Tooltip.live if t.widget is tw.label or t.widget is tw][0]
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
    top.destroy()
    # the main list keeps the artwork full size: with rows present the table itself draws it behind them
    assert app.tree.get_children() == () or not app.empty.winfo_ismapped()
    print("CHROME TEST: ALL PASSED")
    root.destroy()

main()
