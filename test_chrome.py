"""Custom window frames: strip, buttons, maximise/restore, resize grips, themed message boxes. Needs Tk + a display."""
import os, sys, tempfile, time
os.environ["BTC_CHROME"] = "1"
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
    print("CHROME TEST: ALL PASSED")
    root.destroy()

main()
