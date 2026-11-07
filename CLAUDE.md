# Bit Torrent Client – notes for Claude Code

Pure-Python (stdlib only) BitTorrent client for Windows 11: Tkinter GUI, asyncio engine in a thread, optional libmpv player.
Current version is in `version.py`; release notes are the sections at the bottom of `README.md` (add one per change, bump the version).

## Run / test / build
- Run: `python bittorrentclient.py` (Python 3.10+ with Tk). Player child process: `exe --player --session <json>`.
- Tests: `python run_tests.py` (all) or `python run_tests.py real gui chrome`. Tk tests need a display (Linux: `Xvfb :99 &`, `DISPLAY=:99`).
  `test_gui` uses a fake Tk; `test_real`, `test_chrome`, `test_player`, `test_embed` use real Tk. `test_player` needs libmpv + ffmpeg and skips otherwise.
  A failing test's last lines are printed and its full output saved to `test_logs/` (run_tests sets BTC_TRACEBACKS so crashlog echoes errors).
  Windows: network filters (AdGuard) drop the first UDP packet per socket and reset ~5% of loopback TCP -> random failures in
  test_local / test_real / test_cast / test_rss. Pause them before trusting a red run; `netcheck.check()` tells you.
- Build: `build.bat` (PyInstaller onedir + Inno Setup; bundles/downloads libmpv via `libmpv_get.py`). CI: `.github/workflows/build.yml`.
- Data folder: `%APPDATA%\BitTorrentClient` (state.json + .bak, search_plugins, mpv, media state). `crashlog.py` writes the error log.

## Layout
- Engine: `torrent_core.py` (Manager, Session, Peer, piece picker, streaming ranges), DHT/uTP/MSE/UPnP in `utp.py mse.py portmap.py upnp.py`, `rss.py schedule.py`.
- Streaming/media: `stream.py` (localhost + LAN HTTP server), `mpvlib.py` (ctypes libmpv), `player.py` (PlayerPanel / PlayerWindow, external players),
  `embed.py` (re-parents an external player window into the Player tab), `cast.py` (DLNA + Chromecast), `thumbs.py library.py subtitles.py media_state.py libmpv_get.py`.
- UI: `bittorrentclient.py` (App), `dialogs.py` (dialogs, SearchWindow, themed MessageDialog), `ui.py` (palettes LIGHT/DARK="Aurora", ttk styles, Tooltip),
  `chrome.py` (frameless windows with own title strip/resize grips), `assets_ui.py` (embedded banner/backdrop PNGs), `panels.py`, `tray.py`, `dnd.py`.
- Artwork: `assets_ui.BACKDROP_WIDE` (dark, paletted PNG) and `BACKDROP_WIDE_LIGHT` (made from it by `tools/make_light_backdrop.py`), picked by `App._art()`.
- Main list + details tables are `arttable.ArtTable` (Canvas drawn over the aurora, Treeview-like API). Its `delete/move/index/bbox/insert` are the
  Treeview row methods, shadowing Canvas ones: inside it use `tk.Canvas.delete(self, ...)` for canvas items. Details pane art = `App._draw_art(c)`.

## Things learned the hard way (Windows behaviour I could not test from Linux)
- Frameless windows (`overrideredirect`) are not stacked by Windows like owned dialogs: modal dialogs must stay `-topmost`, and must never `wait_visibility()`;
  otherwise a dialog can hide behind another window while holding the grab = "app froze". See `_Dialog.show`.
- Tk `Canvas.lift/tkraise` raise canvas *items*; use `tk.Misc.tkraise(widget)`.
- ctypes: declare `restype` for every Win32 call returning a handle/pointer (default is 32-bit int: a 64-bit HMODULE
  gets truncated). faulthandler logs even faults Windows handles, so "access violation" in error.log can mean this.
- Tooltips must not rely on `<Leave>` alone (watchdog in `ui.Tooltip`).
- Settings pages/column counts are asserted in tests; update them with the UI.
- `test_real` selects rows through the Files tab: set `app._tab = "Files"; app._update_tabs()` first.
- Native `filedialog` boxes cannot be themed. External player embedding (embed.py) is best effort: single-instance players, Qt redraw issues.
- Existing configs migrate with `theme_ver` / `ui_ver` in `App.__init__`.

## Conventions
- Stdlib only at runtime (PIL was used once to generate `assets_ui.py`; do not import it in the app).
- Keep UI sizes in `self.px(...)` (4K at 200% DPI is the main target). Colours come from the palette dict, never hard-coded.
- After UI changes, look at a real screenshot on Windows (the user sends them); Xvfb screenshots with ImageMagick `import -window root` are a rough check only.

## Open items
- Verify on Windows: libmpv auto-download names (`libmpv_get.pick_asset`), OpenSubtitles/TMDB live calls, external-player embedding with MPC-HC, key/click handling inside the embedded mpv window.
- Ideas: IP blocklist, web seeds, code signing, remove the unused category feature.
