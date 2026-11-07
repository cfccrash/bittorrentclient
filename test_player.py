"""Player smoke test: libmpv + Tk in one window (needs libmpv, ffmpeg and a display; skipped otherwise)."""
import os, shutil, subprocess, sys, tempfile, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mpvlib
if not mpvlib.find_library() or not shutil.which("ffmpeg"):
    print("player test: SKIPPED (needs libmpv and ffmpeg)"); sys.exit(0)
try:
    import tkinter as tk
    tk.Tk().destroy()
except Exception:
    print("player test: SKIPPED (no Tk display)"); sys.exit(0)
import player, media_state

tmp = tempfile.mkdtemp()
def make(name, secs, chapters=False):
    p = os.path.join(tmp, name)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=%d:size=320x240:rate=25" % secs,
           "-f", "lavfi", "-i", "sine=duration=%d" % secs]
    if chapters:
        meta = os.path.join(tmp, "meta.txt")
        open(meta, "w").write(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=30000\ntitle=Intro\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=30000\nEND=%d\ntitle=Main\n" % (secs * 1000))
        cmd += ["-i", meta, "-map_metadata", "2"]
    subprocess.run(cmd + ["-c:v", "libx264", "-c:a", "aac", "-shortest", p], check=True)
    return p
a, b = make("a.mp4", 60, True), make("b.mp4", 4)
assert player.fmt_time(3725) == "1:02:05" and player.fmt_time(65) == "1:05"
state = os.path.join(tmp, "state"); os.makedirs(state)
media_state.update(state, "h:0", 33.0, 60.0, "A")                       # earlier session stopped at 33 s
assert media_state.resume_pos(state, "h:0") == 33.0
queue = [dict(url=a, title="A", key="h:0"), dict(url=b, title="B", key="h:1")]
sess = {"state_dir": state, "opts": {"hwdec": False}}
p = player.PlayerWindow(None, queue=queue, session=sess, options={"vo": os.environ.get("PLAYER_VO", "null"), "ao": "null"})
pn = p.panel
assert pn.mpv, "libmpv failed to start"
out = {}
def step(n=[0]):
    n[0] += 1
    if n[0] == 10:
        out["resumed"] = pn.mpv.get_double("time-pos"); out["chapters"] = len(pn.chapters); out["dur"] = pn.dur
        pn.rel(1); pn.set_speed(2.0); pn._fill_tracks(); pn._fill_chapters(); out["menu"] = pn.tracks_menu.index("end")
        pn.ranges, pn.size = [(0, 500)], 1000; pn.hover_x = 40; pn._draw_seek()
        pn.sub_delay(0.1); pn.sub_scale(0.1)
        pn.mpv.command("script-message", "tk-help") if False else None
        pn.step(1)                                                       # next file
    if n[0] == 14:
        out["title"] = pn.cur["title"]; out["idx"] = pn.index
        pn.toggle(); pn.win.update() if hasattr(pn, "win") else None; out["paused"] = pn.mpv.get_flag("pause")
        pn.set_aspect(1); out["aspect"] = pn.mpv.get("video-aspect-override")
        pn.set_aspect(7); out["keep"] = pn.mpv.get_flag("keepaspect"); pn.set_aspect(0)
        p.fullscreen(True); p.last_move = 0; p._hide_bar_tick(); out["cur_hidden"] = str(p.win.cget("cursor")) == "none"
        p._motion(None); out["cur_back"] = str(p.win.cget("cursor")) == ""; p.fullscreen(False)
        pn.stop(); out["stopped"] = pn.stopped; pn.toggle(); out["restarted"] = not pn.stopped
        pn._save_pos(); p.close(); return
    p.win.after(400, step)
p.win.after(400, step)
p.run()
assert 55 < out["dur"] < 65 and out["chapters"] == 2 and out["resumed"] >= 30 and out["menu"] >= 6, out
assert out["idx"] == 1 and out["title"] == "B" and out["paused"], out
assert out["aspect"] in ("1.777778", "1.777778", "16:9") or out["aspect"].startswith("1.77"), out
assert out["cur_hidden"] and out["cur_back"], out
assert out["keep"] is False and out["stopped"] and out["restarted"], out
e = media_state.load(state)["h:0"]
assert e["pos"] >= 20 and not e.get("watched"), e
print("PLAYER TEST: ALL PASSED", out)

# ---- preview thumbnails and key bindings inside the video window
import thumbs, time as _t
clip2 = os.path.join(tmp, "k.mp4")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=25", "-c:v", "libx264", "-g", "25", clip2], check=True)
g = thumbs.Grabber(); got = []
g.request(clip2, 1.0, got.append); end = _t.time() + 8
while not got and _t.time() < end: _t.sleep(0.05)
g.request(clip2, 6.0, got.append); end = _t.time() + 8
while len(got) < 2 and _t.time() < end: _t.sleep(0.05)
assert len(got) == 2 and all(got) and open(got[0], "rb").read() != open(got[1], "rb").read(), got
g.close()
jp = os.path.join(tmp, "p.jpg")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=185x278:duration=1", "-frames:v", "1", jp], check=True)
png = thumbs.png_for(jp); assert png and open(png, "rb").read(4) == b"\x89PNG"
m = mpvlib.Mpv(options={"vo": "null", "ao": "null"})
m.command("keybind", "SPACE", "cycle pause"); m.command("keybind", "UP", "add volume 5"); m.command("loadfile", clip2); _t.sleep(0.6)
m.command("keypress", "SPACE"); _t.sleep(0.1); assert m.get_flag("pause")
v = m.get_double("volume"); m.command("keypress", "UP"); _t.sleep(0.1); assert m.get_double("volume") == v + 5
m.close()
print("PLAYER EXTRAS: preview thumbnails, JPEG poster conversion, in-video key bindings: OK")
