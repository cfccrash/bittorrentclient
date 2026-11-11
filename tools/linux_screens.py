"""Screenshots of the app on Linux (run under Xvfb in CI): the main window, the Settings drop-down, the player in the
Player tab and in its own window. Usage: xvfb-run -a -s "-screen 0 1920x1080x24" python tools/linux_screens.py OUTDIR

Uses ImageMagick's `import -window root`. A test clip is made with libmpv's own encoder."""
import os
import queue
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "screens")
os.makedirs(out, exist_ok=True)
os.environ["APPDATA"] = tempfile.mkdtemp()        # a fresh data folder
import tkinter as tk
import mpvlib
import bittorrentclient as bc
import dialogs
import player

log = lambda *a: print(*a, flush=True)


def shot(name):
    p = os.path.join(out, name + ".png")
    subprocess.run(["import", "-window", "root", p], check=False)
    log("screenshot", p, os.path.exists(p))


def make_clip():
    lib = mpvlib.find_library()
    if not lib:
        log("no libmpv - player screenshots skipped")
        return None
    p = os.path.join(tempfile.mkdtemp(), "Show S01E01.mkv")
    m = mpvlib.Mpv(options={"vo": "null", "ao": "null", "o": p, "ovc": "libx264", "oac": "aac",
                            "ovcopts": "preset=ultrafast,g=25"})
    m.command("loadfile", "av://lavfi:testsrc2=duration=30:size=1280x720:rate=25[out0];sine=duration=30[out1]")
    end = time.time() + 120
    while time.time() < end:
        if any(ev in ("end-file", "idle", "shutdown") for ev in m.poll_events()):
            break
        time.sleep(0.05)
    m.close()
    return p if os.path.isfile(p) else None


clip = make_clip()
root = tk.Tk()
root.report_callback_exception = lambda *a: traceback.print_exception(*a)
app = bc.App(root, queue.Queue())
root.geometry("1800x1000+40+40")
steps = []


def step(ms, fn):
    steps.append((ms, fn))


def warp(w, fx=0.5, fy=0.5):
    """Move the (virtual) mouse over widget w, so hover-only things show."""
    w.event_generate("<Motion>", warp=True, x=int(w.winfo_width() * fx), y=int(w.winfo_height() * fy))


step(4000, lambda: shot("1-main"))


def open_settings():
    d = dialogs.SettingsDialog(app)
    app._screens_settings = d
    root.after(1200, lambda: (d.show_page("General"), root.after(1200, lambda: shot("2-settings")),
                              root.after(2000, d.destroy)))
    d.show()


step(500, open_settings)
if clip:
    step(4000, lambda: app._play_in_tab([dict(url=clip, title="Show S01E01", key="")], mpvlib.find_library()))
    step(3000, lambda: warp(app._panel.video, 0.5, 0.8))
    step(1500, lambda: shot("3-player-tab"))
    step(500, lambda: app.popout_player())
    step(3000, lambda: warp([w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and w.winfo_viewable()][-1], 0.5, 0.85))
    step(1500, lambda: shot("4-player-window"))
step(500, lambda: (log("done"), os._exit(0)))

i = [0]


def run():
    try:
        ms, fn = steps[i[0]]
        i[0] += 1
        fn()
    except Exception:
        traceback.print_exc()
    if i[0] < len(steps):
        root.after(steps[i[0]][0], run)


root.after(steps[0][0], run)
root.after(120000, lambda: os._exit(3))
root.mainloop()
