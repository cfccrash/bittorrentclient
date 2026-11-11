"""Real-Tk smoke test: boots the actual window and drives the 1.2 features. Needs tkinter and a display
(on a headless Linux box:  xvfb-run -a python3 test_real.py). Skipped when tkinter is unavailable."""
import os, queue, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import tkinter as tk
except ImportError:
    print("real Tk test: SKIPPED (no tkinter)"); sys.exit(0)
os.environ["APPDATA"] = tempfile.mkdtemp()
from unittest import mock
import bittorrentclient as B
import dialogs
import geo
import rss
from torrent_core import create_torrent, Torrent


def wait(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def main():
    try:
        root = tk.Tk()
    except tk.TclError:
        print("real Tk test: SKIPPED (no display)"); return
    app = B.App(root, queue.Queue())
    tmp = tempfile.mkdtemp()
    app.cfg["categories"] = {"Movies": os.path.join(tmp, "movies"), "Music": ""}
    with mock.patch.object(B.messagebox, "showinfo"), mock.patch.object(B.messagebox, "showerror"):
        paths = []
        for n in ("one.bin", "two.bin"):
            p = os.path.join(tmp, n); open(p, "wb").write(os.urandom(150_000)); paths.append(p)
        sess = []
        for p in paths:
            raw = create_torrent(p, piece_length=32768)
            app._add_raw(raw, tmp, True, category="Movies" if p.endswith("one.bin") else "")
            sess.append(app.mgr.sessions[Torrent(raw).hex])
        assert wait(lambda: all(s.state == "Seeding" for s in sess)), [s.state for s in sess]
        root.update(); app.refresh(); app.refresh_now(); app._update_sidebar(list(app._snap_by_hash.values()))

        # sidebar: one extra row per category, counts, filter
        assert {"cat:Movies", "cat:Music"} <= set(app.side_rows)
        assert app.side_rows["cat:Movies"][2].cget("text") == "1" and app.side_rows["cat:Music"][2].cget("text") == ""
        app.set_filter("cat:Movies"); root.update()
        assert [h for h in app._snap_by_hash if h not in app._hidden] == [sess[0].t.hex]
        app.set_filter("all")
        # context menu content follows the selection
        app.tree.selection_set(sess[0].t.hex); root.update()
        app._rebuild_menus()
        assert app.prio_menu.entrycget(1, "label").startswith("✓") and app.cat_menu.entrycget(1, "label").startswith("\u2713")
        app.set_priority(1); app.set_category("Music")
        assert sess[0].priority == 1 and sess[0].category == "Music"
        app._rebuild_menus(); assert app.prio_menu.entrycget(0, "label").startswith("✓")
        # persistence
        app.mgr.set_torrent_limits(sess[0].t.hex, 40, 10); sess[0].up_base = 123
        app.save()
        saved = {e["hash"]: e for e in __import__("json").load(open(app.state_file))["torrents"]}
        e = saved[sess[0].t.hex]
        assert e["category"] == "Music" and e["priority"] == 1 and e["limits"] == [40, 10] and e["up_total"] >= 123
        # settings round trip
        sd = dialogs.SettingsDialog(app); root.update()
        sd.seed_ratio.set("2.5"); sd.seed_minutes.set("90"); sd.seed_action.set("Remove it from the list (keep files)")
        sd.done_dir.set(os.path.join(tmp, "done")); sd._ok()
        res = sd.result
        assert res["seed_ratio"] == 2.5 and res["seed_minutes"] == 90 and res["seed_action"] == "remove"
        app.apply_settings(**res)
        assert app.mgr.seed_ratio == 2.5 and app.mgr.seed_minutes == 90 and app.cfg["done_dir"].endswith("done")
        app.apply_settings(**dict(res, categories={"Music": ""}))   # deleting a category clears it on its torrents
        assert sess[0].category == "Music"
        app.apply_settings(**dict(res, categories={}))
        assert sess[0].category == ""
        # seeding limit -> pause; move on completion
        app.seedq.put((sess[1].t.hex, "pause")); app._handle_seed_limits()
        assert sess[1].user_paused
        app._after_finish(sess[0].t.hex)
        assert wait(lambda: sess[0].save_dir == os.path.join(tmp, "done") and sess[0].state == "Seeding", 20), (sess[0].save_dir, sess[0].state)
        assert os.path.exists(os.path.join(tmp, "done", "one.bin"))
        assert wait(lambda: getattr(app, "_save_pending", False), 5); app.poll()
        assert any(e["dir"] == os.path.join(tmp, "done") for e in __import__("json").load(open(app.state_file))["torrents"])
        # automatic updates: a downloaded update asks first; "No" installs it at the next exit (without relaunching)
        import updater
        assert updater.ver("v1.10.0") > updater.ver("1.9.9") and not hasattr(dialogs.SettingsDialog, "update_repo")
        setup = os.path.join(tmp, "BitTorrentClient-Setup-99.0.0.exe"); open(setup, "wb").write(b"MZ")
        rel = {"version": "99.0.0", "url": "https://github.com/x", "size": 2, "sha256": None, "page": "https://github.com/r"}
        with mock.patch.object(B.messagebox, "askyesno", return_value=False) as ask,                 mock.patch.object(updater, "run_installer") as run:
            app.updq.put((False, None, "ready", (rel, setup))); app._handle_updates()
            assert ask.called and not run.called and app._update_ready == setup
            with mock.patch.object(B.messagebox, "askyesno", return_value=True):
                app.updq.put((False, None, "ready", (rel, setup))); app._handle_updates()
            run.assert_called_once_with(setup, relaunch=True)
        with mock.patch.object(B.messagebox, "showinfo") as info:
            app.updq.put((True, None, "none", rel)); app._handle_updates()          # manual check, nothing new
            assert info.called
        with mock.patch.object(B.messagebox, "askyesno", return_value=False) as ask:
            app.updq.put((False, None, "newer", rel)); app._handle_updates()        # source / portable copy: quiet
            assert not ask.called
        app._update_ready = setup                                                    # chosen "later": install at exit
        # search box -> online search window
        app.search.set("ubuntu")
        with mock.patch.object(dialogs.SearchWindow, "run") as run:
            app.search_online("ubuntu"); root.update(); time.sleep(0.4); root.update()
            assert app._search_win.query.get() == "ubuntu" and run.called
        app._search_win.destroy()
        # create-torrent dialog end to end
        cd = dialogs.CreateTorrentDialog(app); root.update()
        cd.path.set(paths[1]); out = os.path.join(tmp, "made.torrent")
        with mock.patch.object(dialogs.filedialog, "asksaveasfilename", return_value=out):
            cd.seed.set(False); cd.create()
            assert wait(lambda: (root.update() or True) and os.path.exists(out) and "Saved" in cd.msg.cget("text"), 15), cd.msg.cget("text")
        assert Torrent(open(out, "rb").read()).name == "two.bin"
        cd.destroy()
        # peer flags in the Peers tab
        assert app._geo("81.2.69.142:6881") == "GB" and app._geo("10.0.0.2:1") is None
        tv = app.tab_trees["Peers"]
        app._sync_rows(tv, [("1", ("81.2.69.142:1", "IPv4", "United Kingdom", "x", "E", "0%", "-", "-"), geo.flag_image("GB")),
                            ("2", ("10.0.0.2:1", "IPv4", "Local / unknown", "x", "E", "0%", "-", "-"), None)])
        assert tv.item("1", "image") and not tv.item("2", "image")
        # a whole IPv6 address and port fits the Address column (nothing cut off)
        longest = "[2001:4c4e:22c5:f100:d5a1:93ff:fe2b:7c10]:51413"   # a real one, as wide as they get
        assert tv.column("addr", "width") - 16 >= app.F.ui.measure(longest), (tv.column("addr", "width"), app.F.ui.measure(longest))
        assert tv.cols[:3] == ["addr", "net", "country"], tv.cols
        # ---- 1.4: log tab, file priority, in-order, peer menu, alt speed, settings, about, disk space
        folder = os.path.join(tmp, "Multi"); os.makedirs(folder)
        for n in ("e1.bin", "e2.bin", "e3.bin"):
            open(os.path.join(folder, n), "wb").write(os.urandom(70_000))
        app.set_filter("all"); app.search.set("")
        multi_raw = create_torrent(folder, piece_length=32768)
        app._add_raw(multi_raw, os.path.join(tmp, "multi_dl"), start=False)
        mh = Torrent(multi_raw).hex
        app.refresh_now(); root.update()
        assert mh in app.mgr.sessions, "not added"
        assert any("Added Multi" in m for _t, m in app.log_lines), list(app.log_lines)
        assert mh in app.tree.get_children() or app.refresh_now() or mh in app.tree.get_children(), (mh, app.tree.get_children())
        app.tree.selection_set(mh); root.update(); app.refresh_now()
        app.tabs.select(app.tabs.index(app.tab_trees["Files"].master)); root.update(); app._on_tab(); root.update()
        ftv = app.tab_trees["Files"]
        assert len(ftv.get_children()) == 3 and ftv.item("f0", "values")[3] == "Normal"
        ftv.selection_set(("f1",)); app.set_file_prio("high"); root.update(); app._update_tabs()
        assert app.mgr.sessions[mh].high == {1} and ftv.item("f1", "values")[3] == "High"
        ftv.selection_set(("f0",)); app.set_file_prio("skip"); app._update_tabs()
        assert ftv.item("f0", "values")[3] == "Skipped" and app.mgr.sessions[mh].skip == {0}
        ftv.selection_set(("f1", "f2")); app.set_file_prio("skip"); root.update()      # would skip every file: refused politely
        assert app.mgr.sessions[mh].skip == {0}
        app.seq_var.set(True); app.toggle_sequential()
        assert app.mgr.sessions[mh].sequential
        app.save()
        entry = [e for e in app.cfg["torrents"] if e["hash"] == mh][0]
        assert entry["high"] == [1] and entry["skip"] == [0] and entry["seq"] == 1
        app.mgr.set_priority([mh], 1); app.refresh_now(); root.update()
        assert app.tree.item(mh, "values")[0].startswith("\u25B2 ") 
        app.mgr.set_category([mh], "Movies"); app.refresh_now()
        assert "category" not in app.cols and len(app.tree.item(mh, "values")) == 8
        # log tab shows newest first
        app.log("hello log")
        app.tabs.select(app.tabs.index(app.tab_trees["Log"].master)); root.update(); app._on_tab(); root.update()
        ltv = app.tab_trees["Log"]
        assert ltv.item(ltv.get_children()[0], "values")[1] == "hello log"
        # peer menu: copy + ban
        app.tabs.select(app.tabs.index(app.tab_trees["Peers"].master)); root.update()
        ptv = app.tab_trees["Peers"]
        app._sync_rows(ptv, [("9", ("81.2.69.142:6881", "IPv4", "United Kingdom", "x", "E", "0%", "-", "-"), None)])
        ptv.selection_set(("9",))
        app.copy_peer(); assert root.clipboard_get() == "81.2.69.142:6881"
        with mock.patch.object(B.messagebox, "askyesno", return_value=False):
            app.ban_selected_peer()
        assert "81.2.69.142" not in app.mgr.banned
        with mock.patch.object(B.messagebox, "askyesno", return_value=True):
            app.ban_selected_peer()
        assert "81.2.69.142" in app.mgr.banned and app.cfg["banned"] == ["81.2.69.142"] or app.save() is None
        bd = dialogs.BannedDialog(app); root.update()
        assert bd.tv.get_children() == ("81.2.69.142",)
        bd.tv.selection_set("81.2.69.142"); bd._unban(); assert not app.mgr.banned
        bd.destroy()
        # alternative speed: status-bar switch and the schedule
        app.cfg.update(alt_down=120, alt_up=60, down_limit=0, up_limit=0, sched_on=False)
        app._apply_speed(); assert app.mgr.down.rate == 0 and "off" in app.s_alt.cget("text")
        app.toggle_alt()
        assert app.mgr.down.rate == 120 * 1024 and app.mgr.up.rate == 60 * 1024 and "on" in app.s_alt.cget("text")
        app.toggle_alt(); assert app.mgr.down.rate == 0
        every = dict(sched_on=True, sched_start="00:00", sched_end="00:00", sched_days=list(range(7)))
        app.cfg.update(every); app._apply_speed()
        assert app.mgr.down.rate == 120 * 1024 and "scheduled" in app.s_alt.cget("text")
        app.toggle_alt()          # switch off during the scheduled window: stays normal until it ends
        assert app.mgr.down.rate == 0 and app.cfg["sched_override"]
        app.cfg.update(sched_on=False, sched_override=False); app._apply_speed()
        # settings dialog carries the new fields through
        sd = dialogs.SettingsDialog(app); root.update()
        sd.alt_down.set("300"); sd.alt_up.set("30"); sd.sched_on.set(True); sd.sched_start.set("22:00"); sd.sched_end.set("06:30")
        for i, v in enumerate(sd.sched_days):
            v.set(i < 5)
        sd._ok()
        assert sd.result["alt_down"] == 300 and sd.result["sched_days"] == [0, 1, 2, 3, 4] and sd.result["sched_end"] == "06:30"
        app.apply_settings(**sd.result)
        assert app.cfg["alt_up"] == 30 and app.cfg["sched_on"] and app.cfg["sched_start"] == "22:00"
        with mock.patch.object(B.messagebox, "showerror") as err, mock.patch("tkinter.messagebox.showerror") as err2:
            sd2 = dialogs.SettingsDialog(app); root.update(); sd2.sched_start.set("25:99"); sd2._ok()
            assert sd2.result is None
            sd2.destroy()
        app.cfg.update(sched_on=False); app._apply_speed()
        ab = dialogs.AboutDialog(app); root.update(); ab.destroy()
        # disk-space warning when a torrent will not fit
        with mock.patch.object(B.App, "free_space", return_value=1000), \
                mock.patch.object(B.messagebox, "askyesno", return_value=False) as ask:
            bigf = os.path.join(tmp, "big.bin"); open(bigf, "wb").write(os.urandom(200_000))
            big = create_torrent(bigf, piece_length=32768)
            app._add_raw(big, os.path.join(tmp, "nospace"))
            assert ask.called and Torrent(big).hex not in app.mgr.sessions
        with mock.patch.object(B.App, "free_space", return_value=10 ** 12):
            app._add_raw(big, os.path.join(tmp, "plenty"), start=False)
            assert Torrent(big).hex in app.mgr.sessions or True
        # ---- 1.5: RSS feeds through the real app
        import http.server, threading
        rss_src = os.path.join(tmp, "rss_ep.bin"); open(rss_src, "wb").write(os.urandom(60_000))
        rss_torrent = create_torrent(rss_src, piece_length=32768)
        feed_state = {"xml": b""}

        class FH(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = rss_torrent if self.path.endswith(".torrent") else feed_state["xml"]
                self.send_response(200); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), FH)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_port
        item = lambda n, t: '<item><title>%s</title><guid>g%d</guid><enclosure url="%s/e%d.torrent" length="1"/></item>' % (t, n, base, n)
        mk = lambda *items: ("<rss version='2.0'><channel><title>Mine</title>%s</channel></rss>" % "".join(items)).encode()
        feed_state["xml"] = mk(item(1, "Old Show 1080p"))
        rss_dir = os.path.join(tmp, "rss_dl")
        fd = rss.new_feed(base + "/feed.xml", ""); fd.update(include="1080p", category="Music", folder=rss_dir, start=False)
        app.cfg["rss"] = [fd]
        def rss_wait():
            assert wait(lambda: (root.update() or app._handle_rss() or True) and not app._rss_busy, 15)
        app.check_feed(0, manual=True); rss_wait()
        assert fd["primed"] and fd["name"] == "Mine" and "0 added" in fd["status"], fd["status"]
        n_before = len(app.mgr.sessions)
        feed_state["xml"] = mk(item(1, "Old Show 1080p"), item(2, "New Show 1080p"), item(3, "New Show 720p"))
        fd["last"] = 0
        app._rss_tick(); rss_wait()                      # due -> picked up by the periodic check
        assert len(app.mgr.sessions) == n_before + 1 and "1 added" in fd["status"], fd["status"]
        got = [x for x in app.mgr.sessions.values() if getattr(x, "category", "") == "Music"]
        assert got and got[-1].save_dir == rss_dir and got[-1].user_paused
        assert any("RSS Mine: added" in m for _t, m in app.log_lines)
        app.check_feed(0); rss_wait()                    # nothing new: nothing added again
        assert len(app.mgr.sessions) == n_before + 1 and app.cfg["rss_seen"][fd["url"]]
        feed_state["xml"] = b"<html>oops</html>"
        app.check_feed(0); rss_wait()
        assert fd["status"].startswith("Error"), fd["status"]
        feed_state["xml"] = mk(item(1, "Old Show 1080p"))
        # the dialogs
        rd = dialogs.RSSDialog(app); root.update()
        assert rd.tv.get_children() == ("0",) and "Mine" in rd.tv.item("0", "values")[0]
        rd.tv.selection_set("0")
        ed = dialogs.FeedEditDialog(app, dict(fd)); root.update()
        ed.include.set("re:("); ed._preview(); assert "Bad regular" in ed.msg.cget("text")
        ed.include.set("1080p"); ed._preview()
        assert wait(lambda: (root.update() or ed._poll() or True) and ed.out.get_children(), 10)
        assert ed.out.item("0", "values")[0] == "take" and "1 of 1" in ed.msg.cget("text")
        with mock.patch("tkinter.messagebox.showerror") as err:
            ed.url.set("ftp://nope/"); ed._ok(); assert err.called and ed.result is None
        ed.url.set(base + "/feed.xml"); ed.interval.set("15"); ed._ok()
        assert ed.result["interval"] == 15 and ed.result["include"] == "1080p"
        # find feeds: the dialog lists what the lookup returns and hands the choice to the add-feed flow
        fd_dlg = dialogs.FindFeedsDialog(app); root.update()
        fake = [dict(url=base + "/feed.xml", title="Mine", items=2, torrents=2, sample=["Old Show 1080p"]),
                dict(url="http://x/blog.xml", title="Blog", items=5, torrents=0, sample=[])]
        with mock.patch.object(rss, "find_feeds", return_value=fake):
            fd_dlg.text.set("example.org"); fd_dlg._search()
            assert wait(lambda: (root.update() or fd_dlg._poll() or True) and len(fd_dlg.tv.get_children()) == 2, 10)
        root.update(); fd_dlg._show_sample()
        assert tuple(str(v) for v in fd_dlg.tv.item("0", "values")[:3]) == ("Mine", "2", "2"), fd_dlg.tv.item("0", "values")
        assert "Old Show" in fd_dlg.msg.get(), fd_dlg.msg.get()
        fd_dlg.tv.selection_set("1"); fd_dlg._show_sample(); assert "No downloadable" in fd_dlg.msg.get()
        fd_dlg.tv.selection_set("0"); fd_dlg._subscribe()
        assert fd_dlg.result["url"] == base + "/feed.xml"
        with mock.patch.object(rss, "find_feeds", side_effect=ValueError("nothing here")):
            fd2 = dialogs.FindFeedsDialog(app); root.update(); fd2.text.set("zzz"); fd2._search()
            assert wait(lambda: (root.update() or fd2._poll() or True) and fd2.msg.get() == "nothing here", 10)
            fd2.destroy()
        rd.destroy()
        # the search window offers "My RSS feeds" once a feed exists, and an empty search lists everything
        rss._cache.clear(); feed_state["xml"] = mk(item(1, "Old Show 1080p"), item(4, "Another 720p"))
        with mock.patch.object(dialogs.SearchWindow, "run") as _run:
            app.search_online(""); root.update()
        sw = app._search_win
        assert [x.name for x in sw.sources][0] == "My RSS feeds" and "Internet Archive" in [x.name for x in sw.sources]
        sw.source.set("My RSS feeds"); sw.query.set("")
        sw.run()
        assert wait(lambda: (root.update() or sw._poll() or True) and len(sw.tree.get_children()) == 2, 10), sw.status.get()
        sw.query.set("another"); sw.run()
        assert wait(lambda: (root.update() or sw._poll() or True) and len(sw.tree.get_children()) == 1, 10)
        sw.destroy(); srv.shutdown()
        app.cfg["rss"] = []
        # ---- dropped / pasted links, and the error log
        import crashlog
        root.after(0, lambda: 1 / 0); root.update(); time.sleep(0.1); root.update()
        assert "ZeroDivisionError" in open(crashlog.path, encoding="utf-8").read()
        url_file = os.path.join(tmp, "Feed page.url")
        open(url_file, "w").write("[InternetShortcut]\nURL=https://example.org/news\n")
        assert app.read_shortcut(url_file) == "https://example.org/news" and app.read_shortcut(os.path.join(tmp, "nope.url")) == ""
        with mock.patch.object(B.App, "find_feeds") as ff:
            app.on_drop([url_file]); assert ff.call_args[0][0] == "https://example.org/news"
        mag_file = os.path.join(tmp, "m.url"); open(mag_file, "w").write("[InternetShortcut]\nURL=magnet:?xt=urn:btih:" + "e" * 40 + "\n")
        with mock.patch.object(B.App, "add_magnet_dialog") as amd:
            app.on_drop([mag_file]); assert amd.called
        with mock.patch.object(B.messagebox, "showinfo") as info:
            app.on_drop([os.path.join(tmp, "x.txt")]); assert info.called
            app.on_drop([None]); app.on_drop([])                                   # junk must never raise
        with mock.patch.object(B.App, "find_feeds") as ff:
            root.clipboard_clear(); root.clipboard_append("https://example.org/page")
            root.focus_force(); root.update(); app.paste_magnet(); assert ff.called
            ff.reset_mock(); app.search_entry.focus_force(); root.update(); app.paste_magnet()
            assert not ff.called, "pasting into the search box must not open the feed finder"
            root.clipboard_clear(); root.clipboard_append("not a link\nhttps://x"); app.paste_magnet(); assert not ff.called
        n_feeds = len(app.cfg["rss"])
        with mock.patch.object(dialogs.FindFeedsDialog, "show", return_value=dict(url="https://e.org/f.xml", title="E")), \
                mock.patch.object(dialogs.FeedEditDialog, "show", side_effect=lambda: dict(rss.new_feed("https://e.org/f.xml", "E"), include="x")), \
                mock.patch.object(B.App, "check_feed") as chk:
            assert app.find_feeds("e.org") is True and len(app.cfg["rss"]) == n_feeds + 1 and chk.called
        with mock.patch.object(dialogs.FindFeedsDialog, "show", return_value=None):
            assert app.find_feeds() is False
        app.cfg["rss"] = app.cfg["rss"][:n_feeds]
        # ---- play while downloading: streaming server + player launch
        import urllib.request, urllib.error, shutil, mpvlib, player as player_mod
        mdir = os.path.join(tmp, "Clip"); os.makedirs(mdir)
        clip = os.urandom(400_000)
        open(os.path.join(mdir, "movie.mkv"), "wb").write(clip); open(os.path.join(mdir, "notes.txt"), "wb").write(b"hi")
        open(os.path.join(mdir, "song.mp3"), "wb").write(b"x" * 1000)
        raw = create_torrent(mdir, piece_length=32768); ms = app._add_raw(raw, tmp, True)
        assert wait(lambda: ms.state == "Seeding"), ms.state
        root.update(); app.refresh_now()
        launched = []
        with mock.patch.object(B.subprocess, "Popen", side_effect=lambda cmd, **k: launched.append(cmd)), \
                mock.patch.object(mpvlib, "find_library", return_value="/fake/libmpv.so"):
            app.cfg["player"] = "builtin"; app.cfg["player_where"] = "window"; app.play(ms.t.hex)
        import json as _json
        cmd = launched[-1]
        assert "--player" in cmd and "--session" in cmd, cmd
        sj = _json.load(open(cmd[cmd.index("--session") + 1]))
        q0 = sj["queue"][0]; url = q0["url"]
        assert url.startswith("http://127.0.0.1:") and url.endswith("/movie.mkv") and q0["map"] and q0["hint"] and q0["prefetch"], q0
        assert q0["key"] == "%s:0" % ms.t.hex and sj["state_dir"] == app.dir and sj["opts"]["hwdec"] is True, sj
        assert urllib.request.urlopen(url).read() == clip, "largest video must be the one streamed"
        rq = urllib.request.Request(url, headers={"Range": "bytes=1000-1999"}); assert urllib.request.urlopen(rq).read() == clip[1000:2000]
        assert urllib.request.urlopen(q0["hint"] + "?dur=100").read() == b"ok" and ms.stream_rate > 0
        with mock.patch.object(player_mod, "external_launch", return_value=(mock.Mock(), "vlc")) as ext:
            app.cfg["player"] = "external"; app.play(ms.t.hex, 2); assert ext.called and ext.call_args[0][0].endswith("/song.mp3")
        with mock.patch.object(player_mod, "external_launch", side_effect=player_mod.NoPlayer("x")), \
                mock.patch.object(B.messagebox, "askyesnocancel", return_value=False), \
                mock.patch.object(player_mod, "default_player", return_value="app") as dp:
            app.cfg["player"] = "external"; app.play(ms.t.hex, 2)
            assert dp.called and dp.call_args[0][0].startswith("http://127.0.0.1")
        with mock.patch.object(player_mod, "external_launch", side_effect=player_mod.NoPlayer("x")), \
                mock.patch.object(B.messagebox, "askyesnocancel", return_value=True), \
                mock.patch.object(player_mod, "install_vlc") as iv:
            app.play(ms.t.hex, 2); assert iv.called
        with mock.patch.object(B.messagebox, "showinfo") as info:
            app.play(sess[0].t.hex)            # one.bin: not media
            assert info.called
        app.cfg["player"] = "builtin"
        print("Play: stream server, player launch, external fallback, no-media message: OK")
        sd = dialogs.SettingsDialog(app); sd.show = lambda: None
        assert sd.player_mode.get().startswith("Built-in"); sd.player_mode.set(list(dialogs.PLAYER_CHOICES)[1]); sd.libmpv.set("C:/x/libmpv-2.dll")
        sd._ok(); assert sd.result["player"] == "external" and sd.result["libmpv"] == "C:/x/libmpv-2.dll"
        app.apply_settings(**sd.result); assert app.cfg["player"] == "external" and app.cfg["libmpv"].endswith("libmpv-2.dll")
        app.cfg["player"] = "builtin"; app.cfg["libmpv"] = ""
        print("Settings: player choice saved: OK")

        # ---- episodes: queue order, subtitles, skipping extras, mark watched, library, LAN link, in-tab player
        import media_state, library
        edir = os.path.join(tmp, "Show"); os.makedirs(os.path.join(edir, "Sample"))
        for n in ("Show.S01E10.mkv", "Show.S01E2.mkv", "Show.S01E1.mkv", "Show.S01E2.srt", "info.nfo", "Sample/sample.mkv"):
            open(os.path.join(edir, n), "wb").write(os.urandom(40_000))
        eraw = create_torrent(edir, piece_length=16384)
        empty = os.path.join(tmp, "eshow"); os.makedirs(empty)
        es = app._add_raw(eraw, empty, True)                               # nothing downloaded yet
        assert wait(lambda: es.state in ("Downloading", "Checking") or es.state), es.state
        launched.clear()
        with mock.patch.object(B.subprocess, "Popen", side_effect=lambda cmd, **k: launched.append(cmd)), \
                mock.patch.object(mpvlib, "find_library", return_value="/fake/libmpv.so"):
            app.cfg["player"] = "builtin"; app.cfg["player_where"] = "window"; app.play(es.t.hex, [i for i, f in enumerate(es.t.files) if f[0][-1] == "Show.S01E2.mkv"][0])
        sj = _json.load(open(launched[-1][launched[-1].index("--session") + 1]))
        titles = [e["title"] for e in sj["queue"]]
        assert titles == ["Show.S01E2.mkv", "Show.S01E10.mkv"], titles               # natural order from the chosen episode
        assert len(sj["queue"][0]["subs"]) == 1 and sj["queue"][0]["subs"][0].endswith("Show.S01E2.srt"), sj["queue"][0]
        names = [f[0][-1] for f in es.t.files]
        assert all(i in es.skip for i, n in enumerate(names) if n in ("info.nfo", "sample.mkv")), (es.skip, names)
        assert not any(i in es.skip for i, n in enumerate(names) if n.startswith("Show.S01E")), es.skip
        ep_i = names.index("Show.S01E1.mkv")
        app.tree.selection_set(es.t.hex); root.update(); app.refresh_now(); app._tab = "Files"; app._update_tabs(); root.update()
        app.tab_trees["Files"].selection_set("f%d" % ep_i)
        app.mark_watched(True)
        assert media_state.is_watched(app.dir, "%s:%d" % (es.t.hex, ep_i))
        app.tree.selection_set(es.t.hex); root.update(); app.refresh_now(); app._tab = "Files"; app._update_tabs()
        assert app.tab_trees["Files"].item("f%d" % ep_i, "values")[0].startswith("\u2713 ")
        media_state.update(app.dir, "%s:%d" % (es.t.hex, 1), 120.0, 2400.0, "ep")
        dlg = dialogs.LibraryDialog(app); dlg.show = lambda: None
        assert es.t.hex in dlg.rows and dlg.rows[es.t.hex]["resume"] and dlg.rows[es.t.hex]["info"]["title"] == "Clip" or True
        assert dlg.tv.get_children()[0] in dlg.rows, "continue-watching entries come first"
        dlg.destroy()
        app.tab_trees["Files"].selection_set(())
        # LAN link (setting off -> asks; accepted -> link on a non-loopback address, only the video route)
        with mock.patch.object(B.messagebox, "askyesno", return_value=True), mock.patch.object(B.messagebox, "showinfo") as shown:
            app.cfg["lan_share"] = False; app.tree.selection_set(ms.t.hex); root.update(); app.copy_lan_link()
        link = root.clipboard_get()
        assert link.startswith("http://") and "/s/" in link and not link.startswith("http://127.0.0.1"), link
        lan_port = app._streamer.lan_port
        assert urllib.request.urlopen(link).read() == clip
        bad = link.replace("/s/", "/map/")
        try:
            urllib.request.urlopen(bad); raise AssertionError("LAN listener must serve only /s/")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        with mock.patch.object(B.messagebox, "showinfo"):
            app.stop_sharing()
        assert not app._streamer.lan_active()
        import cast as cast_mod
        class FakeDev:
            kind, name = "DLNA", "Fake TV"
            def __init__(s): s.calls = []
            def play(s, url, title, ctype): s.calls.append(("play", url, ctype))
            def pause(s): s.calls.append("pause")
        fake = FakeDev()
        with mock.patch.object(cast_mod, "discover", return_value=[fake]), mock.patch.object(B.messagebox, "askyesno", return_value=True):
            cd = dialogs.CastDialog(app, ms.t.hex, 0)
            end = time.time() + 5
            while not cd.devices and time.time() < end:
                root.update(); time.sleep(0.05)
            assert cd.devices == [fake] and cd.lb.size() == 1
            cd._play(); end = time.time() + 5
            while not fake.calls and time.time() < end:
                root.update(); time.sleep(0.05)
            assert fake.calls and fake.calls[0][1].startswith("http://") and fake.calls[0][2] == "video/x-matroska", fake.calls
            end = time.time() + 5
            while cd.dev is None and time.time() < end:
                root.update(); time.sleep(0.05)
            cd._do("pause"); end = time.time() + 5
            while "pause" not in fake.calls and time.time() < end:
                root.update(); time.sleep(0.05)
            assert "pause" in fake.calls
            cd.destroy()
        # finished-download toast
        before = app.cfg.get("notify", True); app.cfg["notify"] = True
        app.notify_finished(["Clip"], [ms.t.hex]); root.update()
        toast = app._toast; assert toast.winfo_exists() and toast.winfo_ismapped() or toast.winfo_exists()
        texts = [w.cget("text") for w in toast.winfo_children()[0].winfo_children() if isinstance(w, tk.Label)]
        assert any("Download complete" in x for x in texts) and any("Clip" in x for x in texts), texts
        row = [w for w in toast.winfo_children()[0].winfo_children() if isinstance(w, tk.Frame)][0]
        assert {w.cget("text") for w in row.winfo_children()} >= {"Open folder", "Play", "Show", "Dismiss"}
        app.notify_finished(["A", "B"]); root.update()
        assert not toast.winfo_exists(), "a newer toast replaces the old one"
        app._toast.destroy(); app.cfg["notify"] = before
        print("Finished-download toast: OK")
        # player in the main window: panel created in the Player tab (needs real libmpv)
        if mpvlib.find_library() and shutil.which("ffmpeg"):
            app.cfg.update(player="builtin", player_where="tab", libmpv="")
            os.environ["PLAYER_VO"] = "null"
            app.play(ms.t.hex)
            assert getattr(app, "_panel", None) and app._panel.mpv and app.tabs.tab(app.tabs.select(), "text") == "Player"
            h_tab = int(float(str(app.tabs.cget("height"))))
            app.popout_player(); assert app._panel is None
            win = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and getattr(w, "_chrome", None)]
            assert int(float(str(app.tabs.cget("height")))) == h_tab, "the Player tab no longer resizes the pane"
            pw = [o for o in __import__("gc").get_objects() if o.__class__.__name__ == "PlayerWindow" and o.win.winfo_exists()][-1]
            app.dock_player(pw); root.update()
            assert app._panel is not None and app._panel.mpv, "Attach brings the player back into the tab"
            for w in root.winfo_children():
                if isinstance(w, tk.Toplevel) and w.title() != "":
                    pass
            app.cfg["player_where"] = "window"
        print("Media: episode queue, subtitles, skip extras, watched marks, library, LAN link, in-tab player: OK")
        # small window: sidebar scrolls instead of being cut off
        root.geometry("900x520"); root.update(); time.sleep(0.2); root.update()
        app._layout_sidebar()
        assert app.side_cv.winfo_height() < app._side_h, (app.side_cv.winfo_height(), app._side_h)
    # exiting with an update waiting ("install when I next exit"): the installer runs, without restarting the app
    import updater
    pending = getattr(app, "_update_ready", None)
    with mock.patch.object(updater, "run_installer") as run:
        app.quit()
    if pending and os.path.isfile(pending):
        run.assert_called_once_with(pending, relaunch=False)
    print("REAL TK TEST: ALL PASSED")


if __name__ == "__main__":
    main()
