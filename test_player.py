"""Player test: libmpv + Tk in one window (needs libmpv and a display; skipped otherwise). Test clips are made with
ffmpeg when it is installed, else with libmpv's own encoder (then the chapter checks are skipped)."""
import os, shutil, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mpvlib
if not mpvlib.find_library():
    print("player test: SKIPPED (needs libmpv)"); sys.exit(0)
try:
    import tkinter as tk
    tk.Tk().destroy()
except Exception:
    print("player test: SKIPPED (no Tk display)"); sys.exit(0)
import player, media_state

FFMPEG = shutil.which("ffmpeg")
tmp = tempfile.mkdtemp()


def make(name, secs, chapters=False):
    p = os.path.join(tmp, name)
    if FFMPEG:
        cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=%d:size=320x240:rate=25" % secs,
               "-f", "lavfi", "-i", "sine=duration=%d" % secs]
        if chapters:
            meta = os.path.join(tmp, "meta.txt")
            open(meta, "w").write(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=30000\ntitle=Intro\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=30000\nEND=%d\ntitle=Main\n" % (secs * 1000))
            cmd += ["-i", meta, "-map_metadata", "2"]
        subprocess.run(cmd + ["-c:v", "libx264", "-c:a", "aac", "-shortest", p], check=True)
        return p
    p = p.rsplit(".", 1)[0] + ".mkv"                    # libmpv's encoder: video + a sine tone, no chapters
    m = mpvlib.Mpv(options={"vo": "null", "ao": "null", "o": p, "ovc": "libx264", "oac": "aac", "ovcopts": "preset=ultrafast,g=25"})
    m.command("loadfile", "av://lavfi:testsrc=duration=%d:size=320x240:rate=25[out0];sine=duration=%d[out1]" % (secs, secs))
    end = time.time() + 120
    while time.time() < end:
        if any(ev in ("end-file", "idle", "shutdown") for ev in m.poll_events()):
            break
        time.sleep(0.05)
    m.close()
    return p


a, b = make("a.mp4", 60, True), make("b.mp4", 24)
assert player.fmt_time(3725) == "1:02:05" and player.fmt_time(65) == "1:05"
assert player.series_key({"title": "The.Office.S02E05.720p.mkv", "key": "ab:3"}) == "s:the office"
assert player.series_key({"title": "Movie (2024).mkv", "key": "cd:0"}) == "t:cd"
state = os.path.join(tmp, "state"); os.makedirs(state)
media_state.update(state, "h:0", 33.0, 60.0, "A")                       # earlier session stopped at 33 s
assert media_state.resume_pos(state, "h:0") == 33.0
queue = [dict(url=a, title="Show S01E01", key="h:0"), dict(url=b, title="Show S01E02", key="h:1")]
sess = {"state_dir": state, "opts": {"hwdec": False}}
p = player.PlayerWindow(None, queue=queue, session=sess, options={"vo": os.environ.get("PLAYER_VO", "null"), "ao": "null"})
pn = p.panel
assert pn.mpv, "libmpv failed to start"
out = {}


def step(n=[0]):
    n[0] += 1
    if n[0] == 10:
        out["resumed"] = pn.mpv.get_double("time-pos"); out["chapters"] = len(pn.chapters); out["dur"] = pn.dur
        pn.rel(1); pn.set_speed(2.0); out["speed"] = pn._speed_label
        pn._fill_tracks(); pn._fill_chapters(); out["menu"] = pn.tracks_menu.index("end")
        pn.ranges, pn.size = [(0, 500)], 1000; pn.hover_x = 40; pn._draw_seek()
        pn._redraw()
        lft, rgt = pn._layout(pn.px(1600)); out["controls"] = {x[0] for x in lft + rgt}      # full-size strip
        lft, rgt = pn._layout(pn.px(480)); out["mini_controls"] = {x[0] for x in lft + rgt}  # the mini player's
        # a click whose press and release have a redraw in between (the strip redraws while playing) still works
        before = pn.mpv.get_flag("pause"); x0, y0, x1, y1 = next(i for i in pn._items if i["key"] == "play")["box"]
        ev = type("E", (), {"x": (x0 + x1) // 2, "y": (y0 + y1) // 2})()
        pn._cv_press(ev); pn._redraw(); pn._cv_release(ev); out["click"] = pn.mpv.get_flag("pause") != before
        pn._cv_press(ev); pn._redraw(); pn._cv_release(ev)
        pn.sub_delay(0.1); pn.sub_scale(0.1)
        pn.audio_delay(0.2); out["adelay"] = round(pn.mpv.get_double("audio-delay"), 1)
        pn.set_night(True); out["night"] = "dynaudnorm" in (pn.mpv.get("af") or ""); pn.set_night(False)
        pn.vol.set(50); out["vol"] = round(pn.mpv.get_double("volume"))
        pn.seek_percent(50); time.sleep(0.3); out["pct"] = pn.mpv.get_double("time-pos")
        audio = [t for t in pn.mpv.tracks() if t["type"] == "audio"]
        if audio:
            pn.pick_track("audio", audio[0])
        pn.pick_track("sub", None)
        out["prefs"] = player.load_prefs(state).get("s:show", {})
        pn.screenshot(); out["shot"] = pn.status.get()
        shot = os.path.join(os.path.expanduser("~"), "Pictures", "Bit Torrent Client", out["shot"].split(": ", 1)[-1])
        out["shot_file"] = os.path.isfile(shot)
        if out["shot_file"]:
            os.remove(shot)                                              # (a test must not leave pictures behind)
        pn.set_speed(1.0)
        pn.seek_abs(pn.dur - 3)                                          # runs out: the next file starts by itself
    if n[0] == 22:
        out["title"] = pn.cur["title"]; out["idx"] = pn.index
        out["sid"] = pn.mpv.get("sid")                                   # remembered for the series: subtitles off
        pn.toggle(); out["paused"] = pn.mpv.get_flag("pause")
        pn.set_aspect(1); out["aspect"] = pn.mpv.get("video-aspect-override")
        pn.set_aspect(7); out["keep"] = pn.mpv.get_flag("keepaspect"); pn.set_aspect(0)
        p.fullscreen(True); p.last_move = pn._last_move = 0; p._hide_bar_tick(); out["cur_hidden"] = str(p.win.cget("cursor")) == "none"
        p._motion(None); out["cur_back"] = str(p.win.cget("cursor")) == ""; p.fullscreen(False)
        p.set_mini(True); p.win.update(); out["mini"] = (p.mini, bool(int(p.win.attributes("-topmost"))))
        p.set_mini(False); p.win.update(); out["unmini"] = (p.mini, bool(int(p.win.attributes("-topmost"))))
        pn.stop(); out["stopped"] = pn.stopped; pn.toggle(); out["restarted"] = not pn.stopped
        pn._save_pos(); p.close(); return
    p.win.after(400, step)


p.win.after(400, step)
p.run()
assert 55 < out["dur"] < 65 and out["resumed"] >= 30 and out["menu"] >= 9, out
assert out["chapters"] == 2 or not FFMPEG, out
assert {"play", "stop", "prev", "next", "back", "fwd", "full", "mini", "tracks", "chap", "speed", "shot", "aspect"} <= out["controls"], out
assert out["mini_controls"] == {"play", "back", "fwd", "full", "mini", "tracks"}, out
assert out["click"], out
assert out["speed"] == "2x" and out["adelay"] == 0.2 and out["night"] and out["vol"] == 50 and 25 <= out["pct"] <= 35, out
assert out["prefs"].get("sub") == {"off": True} and "Picture saved" in out["shot"] and out["shot_file"], out
assert out["idx"] == 1 and out["title"] == "Show S01E02" and out["paused"] and out["sid"] == "no", out
assert out["aspect"] in ("1.777778", "16:9") or out["aspect"].startswith("1.77"), out
assert out["cur_hidden"] and out["cur_back"], out
# (always on top is the window manager's job: CI's virtual display on Linux has none)
assert out["mini"][0] is True and out["unmini"] == (False, False), out
assert out["mini"][1] or sys.platform != "win32", out
assert out["keep"] is False and out["stopped"] and out["restarted"], out
e = media_state.load(state)["h:0"]
assert e.get("watched") or e["pos"] >= 55, e
print("PLAYER TEST: ALL PASSED", out)

# ---- preview thumbnails and key bindings inside the video window
import thumbs
clip2 = make("k.mp4", 8)
g = thumbs.Grabber(); got = []
g.request(clip2, 1.0, got.append); end = time.time() + 8
while not got and time.time() < end: time.sleep(0.05)
g.request(clip2, 6.0, got.append); end = time.time() + 8
while len(got) < 2 and time.time() < end: time.sleep(0.05)
assert len(got) == 2 and all(got) and open(got[0], "rb").read() != open(got[1], "rb").read(), got
g.close()
if FFMPEG:
    jp = os.path.join(tmp, "p.jpg")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=185x278:duration=1", "-frames:v", "1", jp], check=True)
    png = thumbs.png_for(jp); assert png and open(png, "rb").read(4) == b"\x89PNG"
m = mpvlib.Mpv(options={"vo": "null", "ao": "null"})
for key, cmd in (("SPACE", "cycle pause"), ("UP", "add volume 5"), ("l", "seek 10"), ("5", "seek 50 absolute-percent")):
    m.command("keybind", key, cmd)
m.command("loadfile", clip2); time.sleep(0.6)
m.command("keypress", "SPACE"); time.sleep(0.1); assert m.get_flag("pause")
v = m.get_double("volume"); m.command("keypress", "UP"); time.sleep(0.1); assert m.get_double("volume") == v + 5
m.command("keypress", "5"); time.sleep(0.3); assert 3 <= m.get_double("time-pos") <= 5, m.get_double("time-pos")
m.close()
print("PLAYER EXTRAS: preview thumbnails, in-video key bindings (incl. 0-9 jumps): OK")
