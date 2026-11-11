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
- Updates: `updater.py` reads releases of the PUBLIC repo `updater.RELEASES_REPO` (the source repo is private); CI publishes
  there on `v*` tags with the `RELEASES_TOKEN` secret. Installer flags: `/RELAUNCH=1` restarts the app after a silent update.
- Build: `build.bat` (PyInstaller onedir + Inno Setup; bundles/downloads libmpv via `libmpv_get.py`). CI: `.github/workflows/build.yml`.
- Data folder: `%APPDATA%\BitTorrentClient` (state.json + .bak, search_plugins, mpv, media state). `crashlog.py` writes the error log.

## Layout
- Engine: `torrent_core.py` (Manager, Session, Peer, piece picker, streaming ranges), DHT/uTP/MSE/UPnP in `utp.py mse.py portmap.py upnp.py`, `rss.py schedule.py`.
- Streaming/media: `stream.py` (localhost + LAN HTTP server), `mpvlib.py` (ctypes libmpv), `player.py` (PlayerPanel / PlayerWindow, external players),
  `embed.py` (re-parents an external player window into the Player tab), `cast.py` (DLNA + Chromecast), `thumbs.py library.py subtitles.py media_state.py libmpv_get.py`.
- UI: `bittorrentclient.py` (App), `dialogs.py` (dialogs, SearchWindow, themed MessageDialog), `ui.py` (palettes LIGHT/DARK="Aurora", ttk styles, Tooltip),
  `chrome.py` (frameless windows with own title strip/resize grips), `assets_ui.py` (embedded banner/backdrop PNGs), `panels.py`, `tray.py`, `dnd.py`.
- Flags: `flags_data.FLAGS` (20x15 / 40x30 PNGs). 194 come from a WeasyPrint PDF of Noto emoji flags via `tools/flags_from_pdf.py`
  (re-run it after `tools/build_geo.py`, which would put the flat flag-icons back).
- Artwork: `assets_ui.BACKDROP_WIDE` (dark, paletted PNG) and `BACKDROP_WIDE_LIGHT` (made from it by `tools/make_light_backdrop.py`), picked by `App._art()`.
- Main list + details tables are `arttable.ArtTable` (Canvas drawn over the aurora, Treeview-like API). Its `delete/move/index/bbox/insert` are the
  Treeview row methods, shadowing Canvas ones: inside it use `tk.Canvas.delete(self, ...)` for canvas items. Details pane art = `App._draw_art(c)`.
- One artwork behind the whole window: `App._art()` (stretched 1.5x wide, 2x tall) drawn by every canvas with
  `_art_at(c, self.root)` - title strip (`chrome.art`), toolbar (`App.toolbar`), sidebar, list, details, status bar
  (`App.statusbar`). Root <Configure> -> `_art_moved` redraws all. Toolbar/status items are `_DrawnButton` /
  `_DrawnText` handled by `_DrawnBar` (hover, click on release, region tooltips). No separator line frames.
- Sidebar = one canvas `App.side_cv` (see-through): buttons (`_SideButton`, in `App.btn`), separators and filter rows are all drawn (`_layout_sidebar`,
  `_draw_sidebar`; `side_rows` values are `_SideText`). List + column art is centred on `App._main` via `_art_at`; art is stretched 1.5x.
- Dialogs (`_Dialog._see_through`): -transparentcolor = P['bg'] plus a borderless topmost backdrop Toplevel under the
  dialog drawing `_art_at(canvas, app.root)` (only the art shows through, not the main window's text). `_follow` acts
  only on a real geometry change - lifting windows from <Configure> loops forever.
  Colour-keyed pixels are click-through: hover comes from `_hover_poll` (pointer position -> `_widget_at`, ttk
  'active' state / `_hovered`), Enter/Leave are swallowed by a per-dialog bindtag, and clicks landing on the backdrop
  are re-generated on the widget under them (`_fw_press/_fw_release`). Backdrop is transient(root) and painted from
  the position we set (winfo_rootx is stale until Windows moves it).
  `_backdrop_watch` removes a backdrop whose dialog is gone/hidden. SettingsDialog is NOT modal (own `show`):
  `app._dropdown`; a root <ButtonPress> outside it (not on the gear), F11, or root resize (`_art_moved`) closes it.
- Tick boxes: `ui.check_images()` (PNG made with stdlib zlib, real alpha) -> `Round.indicator` element in the TCheckbutton layout;
  the Files dialog and the context menu use the `*_tight` images.

## Things learned the hard way (Windows behaviour I could not test from Linux)
- Main window = `chrome.NativeFrame` (snap=True): a real WS_OVERLAPPEDWINDOW with a subclassed wndproc - WM_NCCALCSIZE
  removes the standard frame, WM_NCHITTEST maps strip -> HTCAPTION, our max button -> HTMAXBUTTON (Snap Layouts), edges -> resize.
  The wndproc must not call Tk (it runs inside Windows' dispatch); a 60 ms poll mirrors IsZoomed/hover. Dialogs stay borderless.
  Real pointers hit the innermost child window first: the strip canvas + toplevel client answer HTTRANSPARENT over the max
  button (else no Snap Layouts). Moving/sizing: PostMessage WM_SYSCOMMAND SC_MOVE|HTCAPTION / SC_SIZE|edge after ReleaseCapture,
  only past SM_CXDRAG (a SendMessage'd WM_NCLBUTTONDOWN from a Tk handler never ends; starting at press eats double-clicks).
  Full screen (`Chrome.fill_screen` / `NativeFrame.fill_screen`, F11): drop WS_CAPTION|WS_THICKFRAME, square DWM corners,
  SetWindowPos to rcMonitor - Windows then hides the taskbar; restore style + saved rect (+ re-maximise).
  While full: WM_NCPAINT -> 0, WM_NCACTIVATE with lParam -1, WM_STYLECHANGING keeps CAPTION/THICKFRAME off (else a
  classic caption is painted over ours). `_poll` re-hooks if Tk rebuilds the wrapper (`wm resizable` can).
  Never call ShowWindow/SendMessage synchronously from a Tk callback via ctypes (minimise did): Tk's wndproc runs
  Python bindings while ctypes has released the GIL -> fatal 'PyEval_RestoreThread'. PostMessage instead.
  Block SC_KEYMENU with lParam 0 (lone Alt) or the hidden system menu grabs the keyboard. Test with real input from a separate
  process (mouse_event absolute): SetCursorPos/mouse_event from the app's own process stall during a move loop.
- Player controls (`PlayerPanel`): a borderless, alpha 0.9, WS_EX_NOACTIVATE Toplevel (`osd`) over the bottom of mpv's
  native video window, drawn on a canvas; `_osd_tick` (120 ms, pointer polling - mpv's window eats Tk motion) places,
  shows and hides it; topmost mirrors the window's (mini player). mpvlib sets keep-open: the end of a file is
  'eof-reached', there is no end-file event. Making a window topmost right after geometry() undoes the move
  (update_idletasks first). test_player makes its clips with libmpv's encoder when ffmpeg is missing.
  Embedded mpv gets no mouse input: clicks / double-clicks / wheel on the video are Tk bindings on `panel.video`.
  In the tab, player keys are bind_all + key_scope (tab showing). Panel timers go through `panel.after` (close
  cancels only those - `after info` lists the whole app's timers).
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
