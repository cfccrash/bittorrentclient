# Bit Torrent Client

A small BitTorrent client for Windows 11 (Python + Tkinter, no third-party dependencies).

> Formerly *PyTorrent*. On first launch your saved torrents and settings are carried over from
> `%APPDATA%\PyTorrent`. If you had registered the magnet-link handler, click the button in Settings again
> (it points at the new program name).

## Install it on Windows 11

**Users:** download the installer from the public releases page,
**https://github.com/cfccrash/bittorrentclient-releases** (direct link:
`https://github.com/cfccrash/bittorrentclient-releases/releases/latest/download/BitTorrentClient-Setup.exe`).
Installed copies keep themselves up to date (see 1.9.21 below).

**Publishing a release:** bump `version.py`, add its `### x.y.z` section at the end of this file, commit, push, then
push a matching tag (`git tag v1.9.22`, `git push origin v1.9.22`). The GitHub build tests, builds and publishes it
there; details under "Releases (for the maintainer)" in the 1.9.21 notes.

**Building an installer on this PC** (to try a change before releasing it):
1. Install Python 3.10+ from https://www.python.org/downloads/ (tick "Add python.exe to PATH") and
   **Inno Setup 6 or 7** from https://jrsoftware.org/isdl.php.
2. Double-click `build.bat`. It runs the self-test, builds the program, bundles libmpv, then writes
   `installer\BitTorrentClient-Setup-<version>.exe` (and the program folder `dist\BitTorrentClient`).
   On Windows editions with winget it can install Inno Setup itself; LTSC / IoT editions have no winget.

**Running the installer**
- Installs for you only by default (no administrator prompt); choose "for all users" on the first page to install
  into Program Files.
- Adds a Start menu entry (and an optional desktop shortcut) and an entry in Settings > Apps > Installed apps, so it can be
  uninstalled normally. Upgrading is just running a newer installer over the old one.
- Optional tasks: start with Windows (minimised to the tray), make the app available for `.torrent` files and magnet
  links, and (all-users install only) allow it through Windows Firewall.
- Windows keeps your default-app choice to itself: after installing, pick Bit Torrent Client under Settings > Apps >
  Default apps (the installer offers to open that page). The Settings > General button in the app also registers magnet links.
- Uninstalling removes the program, shortcuts, registry entries and firewall rule. Your torrents and settings stay in
  `%APPDATA%\BitTorrentClient` so a reinstall picks up where you left off; delete that folder to remove them.
- To change the version number, edit `version.py`.

(Or just run it directly: `py bittorrentclient.py`.)

## Using it
The window follows the Windows 11 look: a flat toolbar, a table with a slim coloured progress bar per torrent
(blue downloading, green seeding, amber checking, grey paused, red error), a details strip for the selected
torrent and a status bar. It matches Windows' light/dark setting (or force one in Settings > General).

- **Add torrent** (Ctrl+O): pick `.torrent` files; a dialog lists what is inside, lets you choose the save folder
  (with a free-space check) and whether to start right away.
- **Magnet link** (or Ctrl+V while a magnet link is on the clipboard): paste the link, choose a folder.
  Bit Torrent Client fetches the torrent's metadata from peers, then starts the real download automatically.
- **Resume / Pause / Remove / Open folder** act on the selected rows; they grey out when they do not apply.
  Space pauses or resumes, Delete removes, right-click shows the same actions. Click a column heading to sort.
- Status bar: total speeds, torrent counts, DHT size, your port, and a router port-forwarding indicator
  (green = forwarded, amber = working on it / double NAT, red = unavailable; hover for details).
- Settings > General > **Use Bit Torrent Client for magnet links** makes clicking a magnet link in your browser open it
  here (only one window ever runs). Windows 11 may ask you to confirm under Settings > Apps > Default apps.
- Torrents and progress persist across restarts (stored in `%APPDATA%\BitTorrentClient`). On launch, existing
  data is re-verified so downloads resume where they left off.
- First run: allow Bit Torrent Client through Windows Firewall (private networks) so peers can connect to you.

## Settings (toolbar button; tabs General, Speed, Connection)
- **Theme** (match Windows / light / dark) and default save folder.
- **Download / upload limit** in KiB/s (0 = unlimited), applied to all torrents immediately.
- **Connection encryption** (Message Stream Encryption, RC4): *Prefer* (default) tries an encrypted
  connection first and falls back to plain; *Require* refuses plaintext peers (both directions);
  *Disabled* never encrypts.
- **Transport**: *TCP first, uTP as fallback* (default), *Prefer uTP*, or *TCP only*. uTP runs over the same
  UDP port as the DHT (default 6881).
- **Automatic router port forwarding**: on by default.
  - Bit Torrent Client finds out which protocol your router speaks - **PCP**, then **NAT-PMP**, then **UPnP** -
    forwards its TCP and UDP port, renews the mapping before it expires and removes it when you quit.
  - *Different external port*: if the router cannot (or will not) use your port number, whatever port it
    offers is accepted, and that port is what trackers, the DHT, peer exchange and other peers are told. UDP
    is steered to the same number as TCP so uTP peers can find you. The status bar then reads e.g.
    "Port 6882 (local 6881)".
  - *NAT-PMP / PCP housekeeping*: Bit Torrent Client follows the protocol's rules for router restarts - it tracks the
    router's uptime counter and re-creates the mappings the moment it goes backwards, listens for the router's own
    "I rebooted / my address changed" broadcasts (224.0.0.1:5350), and checks the router's external address every
    10 minutes. Temporary errors ("network failure" while the WAN link is still coming up) are retried after a minute.
  - The status bar shows the outcome, e.g. "Port forwarding: forwarded TCP 6882 (local 6881), UDP 6882
    (local 6881) via NAT-PMP (public IP ...)".
    If your router supports none of this, or sits behind another router (double NAT / CGNAT), forward the ports
    by hand.

- **Listening port**: chosen at random (49152-65000) on first run and then kept, so peers and trackers keep finding you. Change it, or press *Random*, in Settings > Connection (applies after restart).

- **System tray**: the window's close button hides the app to the notification area and downloads keep going. Click the tray icon to reopen it; right-click for *Exit*. Turn this off in Settings > General to make the close button quit.

- **Choosing files**: when you add a multi-file .torrent, *Choose files...* lets you tick what to download. For torrents
  already in the list (including magnet links once their metadata has arrived) select it and press *Files* (or
  right-click > Files...). Ticking more files later downloads them; unticking stops that file from being fetched. A torrent whose
  chosen files are done shows **Finished** (it keeps seeding what it has) and **Seeding** once everything is complete.
  Files at the edge of a piece may be partly written even when unticked, because pieces are verified whole.
- **Download queue**: Settings > Speed > *Active downloads* limits how many torrents download at once (0 = no limit).
  The rest show **Queued (1)**, **Queued (2)** and start automatically as slots free up. Right-click > Queue moves a torrent to the top, up, down
  or bottom; the order is saved. Seeding and paused torrents never take a slot.
- **Notifications**: a Windows notification appears when a download finishes (Settings > General turns it off). It is
  delivered by the tray icon, which appears briefly if the window is open. Clicking the notification opens the window.

- **Sidebar and search**: the list on the left filters by state (All, Downloading, Seeding, Queued, Paused,
  Errors) and shows how many torrents are in each. The box above it filters by name.
- **Details tabs** under the list: *Overview*, *Files* (per-file progress and whether each is downloaded), *Peers*
  (address, client, flags, progress, speeds; flags: E = encrypted, I = they connected to you, u = uTP), *Trackers*
  (last result of each announce) and *Speed* (a five-minute download/upload graph for the selected torrent, or for
  everything when nothing is selected).
- **Drag and drop**: drop `.torrent` files onto the window to add them.
- **Fast restarts**: the verified-piece map and file timestamps are saved, so unchanged torrents are not re-hashed
  at start-up. Right-click > *Verify data* forces a full check if you ever want one. Any change to a file (size or
  modified time) triggers an automatic full check of that torrent.
- **Encryption modes** (Settings > Connection): *Encrypt the handshake only* is the default for new installs: the
  BitTorrent header is hidden but the data itself is sent plainly, which is far faster than full RC4 (the cipher
  runs in Python, roughly 8 MB/s). Peers that insist on RC4 are still served with it. *Prefer/Require full encryption* encrypt everything.
  If you installed an earlier version your old choice is kept.
- **Speed**: pieces are hashed and written to disk on worker threads, uploads read from disk off the network thread,
  and the number of outstanding requests per peer grows with the peer's speed (10 up to 64) so fast peers on
  high-latency links are not left idle.

### If port forwarding still fails
- Click the **port-forwarding indicator** in the status bar, or Settings > Connection > **Diagnostics...**. It lists what
  was found on your network and what was tried, with plain-language hints. **Re-scan router** retries immediately and
  **Copy to clipboard** gives you a report to send if you need help.
- Discovery was hardened for real routers: the UPnP search goes out of every network adapter and is also sent straight
  to the gateway (Windows Firewall often drops multicast replies), five search targets are tried, well-known description
  URLs are probed, odd XML is tolerated, a guessed gateway (x.x.x.1) is used when Windows reports none, and a router that
  rejects timed leases is retried with a permanent one.
- **Windows Firewall**: if you dismissed the first-run prompt Windows silently creates a *Block* rule. Settings >
  Connection > **Allow in Windows Firewall** replaces it with one allow rule (one permission prompt).

## Seeding, priority, limits, categories
- **Seeding rules** (Settings > Downloads): stop at a share ratio and/or after N minutes, then pause the torrent or remove it
  from the list (files are kept). A torrent you resume yourself keeps seeding.
- **Per-torrent**: right-click > *Speed limit...* (download/upload KiB/s for just that torrent), *Priority* (High / Normal /
  Low - high-priority torrents start first when the queue is limited), *Category*.
- **Categories** (Settings > Downloads): each can have its own save folder, picked automatically in the Add dialog, and gets a
  filter row in the sidebar. *Move finished downloads to* relocates completed torrents (they keep seeding from the new place).
- **Create torrent** (sidebar): hash a file or folder, add trackers / comment / private flag, optionally start seeding it.
- **Updates**: enter a GitHub `owner/name` in Settings > General; the app checks its latest release about once a day.

## Files, order, schedule, peers and the log (1.4)

- **Per-file priority** - on the Files tab select one or more files, right-click and choose *High priority*, *Normal priority* or *Don't download*. High-priority files are fetched before the rest.
- **Download in order** - right-click a torrent and tick *Download in order (for streaming)*: pieces arrive from the start of the file onwards, so you can watch while it downloads (it is slower to find rare pieces than the default).
- **Alternative speed and schedule** - Settings > Speed holds a second pair of limits. Click *Alt speed* in the status bar to switch it on or off, or tick the schedule to use it automatically between two times on the days you choose (22:00 to 07:00 runs overnight). Switching it off during a scheduled window keeps normal speed until the window ends.
- **Disk space** - adding a torrent that will not fit asks first, and a download stops with "Disk full" if the drive runs below 32 MB free (resume it after freeing space).
- **Peers** - right-click a peer on the Peers tab to copy its address or ban it. Banned addresses are refused and never contacted; manage them in Settings > Connection > Banned peers.
- **Log tab** - newest first: torrents added, paused, resumed, removed and finished, errors, router changes, bans and speed-mode changes (kept for the current session).
- **List badges** - the list shows each torrent's category in its own column, and an arrow before the name for high (up) or low (down) priority.
- **About** - Settings > General > About shows the version and credits.

## RSS feeds (1.5)

Click **RSS feeds** in the left column. *Add feed* takes the feed address (RSS 2.0 or Atom; http or https), words the title must contain (all of them; `"quoted phrases"`, or `re:` plus a regular expression), words it must not contain, a check interval, and an optional category and save folder. **Preview** shows which items in the feed would be taken right now. A new feed only records what is already in it and then downloads what appears afterwards, unless you tick *Also take the items already in the feed*. Items are added without opening any dialog; what happened is in the Log tab and the feed's status line. A .torrent that cannot be fetched is retried at the next check.

**Finding feeds** - in the RSS window, *Find feeds* takes either a website address (its `<link>` feed tags are read, then the usual `/feed`, `/rss`, `/atom.xml` ... paths are tried; if you give the address of a feed itself, that feed is used) or a keyword, which searches a public feed directory (best effort: it needs that service to be reachable). Every candidate is fetched, so the list shows how many downloadable torrents it really has, with feeds you can download from first. Select one and *Subscribe* opens the normal add-feed form filled in.

**Links** - you do not need to hunt for a feed's address: copy the website's normal address and press Ctrl+V in the main window (not in a text box), or drop a link from the browser's address bar onto the window as a shortcut file, and *Find feeds* opens with it already searching. Magnet links pasted or dropped the same way go to *Add magnet link*.

**Error log** - problems (including anything that goes wrong in a callback or a worker thread) are written to `error.log` in the app's data folder (`%APPDATA%\BitTorrentClient`); Settings > General > About > *Open error log* opens it. If the app ever closes by itself, that file is the first place to look.

**Searching your feeds** - once you have a feed, the *Search online* window has a source called **My RSS feeds** (first in the list). It looks through the items of all your switched-on feeds (kept for 5 minutes, so repeated searches are instant), using the same word rules as the filters. Leave the box empty and press Search to list everything in your feeds. Double-click a result to add it, exactly like any other source; the Source column shows which feed it came from.

## Speed

`python tools/bench.py [size_MiB] [piece_KiB] [--profile=down|up]` moves a file between two copies of the engine on this computer and prints the speed (and, with `--profile`, where the time went). 1.5 made the engine about 3x faster on this test: the piece picker no longer rescans every piece for every request, uploads read the disk in 256 KiB chunks and serve blocks from a small cache, and per-message timeouts were replaced by a cheap idle check. Real downloads are limited by the swarm and your line, so expect gains mainly on fast connections, large torrents and torrents with small pieces.

Peer handling is also smarter: while downloading, the peers that send you the most are the ones you upload to (plus one rotating "optimistic" slot so newcomers get a chance); while seeding, the peers that take the most; peers that unchoke you but send nothing for 30 s are dropped, and when the peer list is nearly full the slowest useless connections make room for new ones.

## Peer locations
The **Peers** tab shows a national flag and country name for every peer, looked up offline from a bundled IPv4 database
(no network requests, nothing about your peers leaves your PC). Private / LAN addresses show "Local / unknown". The country is
where the address block is registered, so VPN, proxy and CDN peers can appear in another country. IP data: DB-IP Lite country
database by [DB-IP.com](https://db-ip.com) (CC BY 4.0); flags from [flag-icons](https://github.com/lipis/flag-icons) (MIT).
Refresh the data with `python3 tools/build_geo.py` (needs npm and rsvg-convert).

## Search online
**Search online** (sidebar, Ctrl+Shift+F) opens a search window: choose a source, type a query, double-click a result and it
goes through the normal add dialog (folder, file selection). The built-in source is the **Internet Archive** (freely
shareable items such as public-domain films, music, software, Linux images). More sources are plugins: small `.py` files in
`%APPDATA%\\BitTorrentClient\\search_plugins` (the window's *Plugins...* button opens the folder; `_example.py` is a template).
A plugin defines `NAME` and `search(query, limit)` returning dicts with `name`, `size`, `seeders`, and a `magnet` or
`torrent_url`. Plugins run with the app's permissions - only install ones you trust. You are responsible for what you download.

## Look
The light and dark themes use a frosted "glass" palette, and on Windows 11 the title bar uses the system Mica backdrop, tinted to match the window with rounded corners (Windows 10 keeps its normal frame).

## Supported / not supported
Supported: `.torrent` files and magnet links (BEP 9/10), DHT (BEP 5), **peer exchange
(BEP 11)**, **Message Stream Encryption**, **uTP (BEP 29)**, **global speed limits**, **automatic port forwarding with any external port (PCP, NAT-PMP, UPnP)**, single and multi-file torrents, HTTP/HTTPS and UDP trackers, hash
verification, resume, seeding (also serves metadata to magnet users), rarest-first piece picking, end-game
mode. Private torrents never use DHT or PEX (BEP 27).

Network notes: allow Bit Torrent Client through Windows Firewall. Port forwarding normally opens **TCP and UDP 6881** on
your router for you (UDP carries the DHT and uTP); otherwise forward them manually. The status bar shows DHT nodes; the app bootstraps from the public DHT
routers on startup.

Verification status: PCP, NAT-PMP and UPnP were tested against simulated routers (detection order, mapping,
lease fallback, refusals, busy external ports, router reboots and renumbering, removal on exit); real routers vary, so check the status bar
on first run. Encryption, uTP, PEX,
limits and the DHT were tested between two Bit Torrent Client instances
(including uTP across a simulated network with packet loss, reordering and duplication). Interoperability
with other clients (qBittorrent, Transmission, ...) could not be tested from the
build environment; the formats follow the BEP / MSE specifications. If a particular peer will not connect,
try Settings -> Encryption "Prefer" and Transport "TCP first".

Not supported: BEP 42 DHT node-id hardening, per-torrent speed limits, scheduling.
Notes: IPv6 is deliberately not supported (it was removed in 1.1.4); the client is IPv4 only. IPv6 peers from trackers, PEX and the DHT are ignored.

Only download content you have the right to share.

## Developing
`python test_rss.py` tests feed parsing, filters and an end-to-end check against a local feed; `python test_v14.py` tests file priority, in-order download, banned peers, the disk-full guard and the schedule; `python test_features.py` tests seeding rules, priority, relocation and torrent creation; `xvfb-run -a python3 test_real.py` drives the real window (needs tkinter and a display, skipped otherwise). `python test_local.py` runs the engine tests (loopback networking, fake routers). `python test_gui.py` runs the
interface logic against a fake tkinter (no display needed); it cannot judge how the window looks. `python make_icon.py`
regenerates `BitTorrentClient.ico` (used by `build.bat`) and `icon_data.py` (the window icon).


## Playing while downloading (1.6)

Select a torrent and choose **Play** (right-click menu), or double-click / right-click **Play** on a file in the **Files** tab.
The client starts a small web server that only listens on this computer (127.0.0.1, with a random secret in the address) and
hands the file to a player while it downloads. Whatever you jump to is fetched first: the pieces at the play position, the
start and end of the file (where most formats keep their index) and a window ahead of you. A seek to a part that is not
downloaded yet waits for it instead of failing.

* **Built-in player** (`player.py`) is based on libmpv, so it plays practically every format, with audio/subtitle track
  menus, subtitle loading, speed, volume, full screen (F) and a seek bar that shows which parts are already downloaded.
  It also works as an app of its own: `python player.py movie.mkv` (or `BitTorrentClient.exe --player movie.mkv`).
  Keys: Space pause, Left/Right 5 s (Shift 30 s), Up/Down volume, F full screen, M mute, Ctrl+O open.
* **libmpv is not included** in this source folder. Download a Windows libmpv build (for example the "libmpv" / "mpv-dev"
  packages published by the mpv project's Windows builders at mpv.io/installation) and put `libmpv-2.dll` (or `mpv-2.dll`)
  next to `BitTorrentClient.exe`, or next to `build.bat` before building so it is bundled, or choose it in
  Settings > General > Media player. Use the 64-bit build.
* **Other player**: without libmpv, or when you pick "Other player" in Settings, the same stream address is opened in
  VLC or mpv if installed, otherwise in the system default player. Any player that supports HTTP seeking works.
* Licensing: libmpv is LGPL 2.1+ or GPL 2+ depending on how that build was compiled. If you share the program with
  libmpv bundled, follow that licence (keep its notices, and for GPL builds offer the corresponding source).
* Tests: `python test_stream.py` (server + seeking), `xvfb-run python3 test_player.py` (needs libmpv + ffmpeg).

## Media features (1.7)

* **Get libmpv**: Settings > Media > "Get libmpv…" (or answer Yes when Play asks) downloads the newest 64-bit libmpv
  build from GitHub and keeps it in `%APPDATA%\BitTorrentClient\mpv`. Unpacking .7z archives uses the `tar` that ships
  with Windows 10/11 (or 7-Zip if installed). If it fails, download libmpv yourself (see above) and choose the file.
* **Resume and watched**: the player remembers where you stopped (`media_state.json`); a file past 92 % is marked
  watched (a tick in the Files tab; right-click to mark by hand). Pressing Play resumes.
* **Subtitles**: subtitle files in the torrent are loaded automatically. Tracks menu: load a file, download from
  OpenSubtitles (free API key + account in Settings > Media), size and delay controls (`,` `.` `z` `x`).
  Preferred audio/subtitle languages in Settings > Media (e.g. `eng,jpn`).
* **Buffer status**: the status line shows how many seconds are downloaded ahead and warns when the download is slower
  than the video's bitrate. The look-ahead window follows the video's bitrate (about 45 s of video).
* **Episodes**: Play queues the rest of the season in natural order (E2 before E10) and starts fetching the next
  episode when you are 85 % through. N / P (or the |< >| buttons) skip. Samples, .nfo, .jpg, .txt, .exe files are
  not downloaded while you watch (Settings > Media turns this off).
* **Player in the main window**: Settings > Media > "Built-in player shows in" > Player tab. "Pop out" moves it to a
  window; full screen always uses a window.
* **Hardware decoding** is on by default (`hwdec=auto-safe`); untick it in Settings > Media if video glitches.
* **Seek bar**: chapter marks, hover time/chapter name, a Chapters menu, PageUp/PageDown for chapters. `?` lists keys.
  (Thumbnail previews are not included.)
* **TV / phone**: "Copy link for TV / phone…" shares one file on your network (off until you allow it). Open the link
  in VLC, Kodi or a TV player. It is a plain HTTP link with a random secret: anyone on the network with the link can
  watch. Casting protocols (DLNA/Chromecast discovery) are not implemented.
* **Library** (side column): all your torrents with video/audio, "continue watching" first. A free TMDB API key adds
  descriptions; posters are converted for Tk with Pillow if installed, else Windows' own imaging (PowerShell), else libmpv.
* Tests: `python test_media.py`; `xvfb-run python3 test_player.py` (libmpv + ffmpeg).

## Media, round 2 (1.8)

* **Seek-bar previews**: hover the seek bar for a thumbnail (a second, hidden libmpv grabs the frame). For a file that is
  still downloading, previews appear only where that part is already downloaded, and never move the download's
  priority. Turn off in Settings > Media.
* **Cast to TV…** (torrent or file right-click menu): finds DLNA/UPnP TVs and receivers (SSDP) and Chromecast / Google TV
  devices (mDNS + the Cast protocol), then plays the video on the one you pick, with pause / resume / stop. The device
  pulls the stream from this computer, so keep the program running; Windows may ask to allow network access. Chromecast
  cannot play every codec (it often refuses MKV with HEVC or multichannel audio); DLNA TVs mostly can.
* **Safer network links**: links expire (default 8 hours, Settings > Media), only addresses on your own network
  (private / loopback ranges) are served, and "Stop sharing on the network" (torrent menu) revokes every link at once.
  A password is not offered because TVs and cast devices cannot send one.
* **Clicks and keys inside the video** now work through mpv's own bindings (click pauses, wheel = volume, space, arrows,
  PageUp/PageDown, N/P, F), so they work even when the video window has the keyboard focus. The volume slider follows.
* **Posters without Pillow**: converted via Windows' imaging or libmpv.
* Tests: `python test_cast.py` (fake DLNA TV and fake Chromecast on localhost), `python test_media.py`.

## Aurora look and modern windows (1.9)

- **Aurora theme** – the app's dark theme is now a near-black navy with blue/violet glow, taken from the supplied wallpaper. It is the default; *Settings → General → Theme* still offers Light and Match Windows.
- **No standard title bars** – on Windows every window (main, dialogs, Search, the video player, the libmpv download window) is drawn without the grey/white Windows title bar. Each has its own slim strip with the Aurora artwork and its own minimise / maximise / close buttons, drag to move, double-click to maximise, and grab-the-edge resizing. Dialogs have a close button only.
- **Themed message boxes** – Yes/No, warnings and errors are Aurora-styled dialogs (Enter = first button, Esc = cancel) instead of the native grey boxes.
- **Escape hatch** – *Settings → General → Windows integration → "Use the standard Windows title bars…"* (restart needed) switches back to the normal frames.
- Not themed: the Windows file/folder pickers ("Open", "Browse…") – those are drawn by Windows itself.
- Tests: `test_chrome.py` (frame, maximise/restore, resize, dialogs, message boxes); run the suite with `BTC_CHROME=1` to exercise the custom frames under Xvfb.

### 1.9.1
- The aurora artwork now fills the whole torrent list when it is empty and fades into the window instead of sitting in a box.
- Fixed a freeze: a dialog opened while the Search window was up (e.g. *Download selected*) could appear behind it while holding the input grab, so the app looked hung. Dialogs now come to the front and no longer wait on a visibility event.

### 1.9.2
- The aurora artwork stays behind the torrent list when torrents are added (it fills the space below the last row).
- Playing now opens in the **Player tab** by default (the tab grows to a proper video size). **Detach** moves the video to its own window; **Attach** there puts it back and continues where it was. Existing installs are switched to the tab once; *Settings → Media* still lets you choose a separate window.

### 1.9.3
- Player controls: **Stop** button (remembers the position; Play starts again from it) and an **Aspect** menu – Original, 16:9, 4:3, 21:9, 2.35:1, 1:1, Fill (crop edges), Stretch to window. Keys: **A** cycles the aspect ratio, **S** stops. Pause/Play, ±10 s, previous/next, speed, volume, Full and Detach were already there.

### 1.9.4
- The left column always shows every entry (down to *Errors*): the details pane at the bottom now gives up height until the whole sidebar fits.
- Selecting the Player tab no longer resizes anything; use **Detach** (or **Full**) for a bigger picture.

### 1.9.5
- Fixed a freeze when a dialog was opened from another dialog (Settings → About): the main window could come to the front and hide them while the hidden one still held the input. Modal dialogs now stay on top of the main window while they are open.

### 1.9.6
- Tooltips no longer get stranded on screen: they close when the pointer leaves, on click, when the window is minimised, hidden to the tray or closed, and a watchdog removes any whose "pointer left" event was missed.

### 1.9.7
- Full screen: the mouse pointer now hides after about 2.5 s without movement (together with the control bar) and comes back as soon as you move the mouse.

### 1.9.8
- Search plugins: a plugin copied into the `search_plugins` folder shows up without restarting – the Search window re-reads the folder when it gets the focus, when you open the source list, and before every search.
- Search results: each source is now asked for up to 1000 results (was 100). Sources return as many as they allow up to that; the Pirate Bay (apibay) API itself caps at about 100 per query.

### 1.9.9
- **External players in the Player tab** (Windows): with *Settings → Media → Play in: Inside the main window*, VLC / MPC-HC / mpv (whichever you chose or was found) is started as before and then its window is taken into the Player tab. The strip under the video has **Stop** (closes the player) and **Detach** (gives it back its own window, still playing). If the player closes, the tab returns to its hint.
- Best effort: this re-parents another program's window, so it depends on the player. Players that pass the file to an already-open copy of themselves (single-instance mode, e.g. MPC-HC's default) have no window to take – the log then says so and it stays in its own window; turn off "Allow only one instance" in that player to embed it. Players built on Qt/DirectX may redraw oddly when resized; Detach is the escape hatch. `test_embed.py` covers the logic with a fake Windows layer.

### 1.9.10
- External players in the Player tab are tidier: **MPC-HC / K-Lite** is started with `/new` (always its own window, so it can be taken even if MPC-HC is open) and switched to its *Minimal* view (Ctrl+1) so the whole frame is picture instead of toolbar and seek bar; **VLC** starts with `--qt-minimal-view`, no title overlay and no single-instance hand-over; **mpv** draws straight into the frame (`--wid`).
- The strip under the video gained **Full screen** (detaches the player and sends its full-screen key) and, for MPC-HC, **Controls** (toggles Minimal / Normal view). Stop and Detach as before.

### 1.9.11
- **libmpv is built into the installer.** `build.bat` and the GitHub Actions workflow now download libmpv (`python libmpv_get.py dist\BitTorrentClient`) and put it in the program folder, so an installed copy has the built-in player straight away and never shows the "needs libmpv" question. A `libmpv-2.dll` placed next to `build.bat` / in the repository is still used instead of downloading. If the download fails at build time the app falls back to offering it on first use, as before.

### 1.9.12
- DHT: a node whose routing table is still empty (for example because its first bootstrap packet was lost) now retries after 5 s, backing off to 60 s, instead of waiting a full minute before trying again.
- **Incoming connections on Windows with Hyper-V / WSL / Docker:** those reserve blocks of ports in the 49152+ range, and a random port that fell into one (plus the 19 after it) left the client running with *no incoming connections* on every start. The client now falls back to a free port chosen by Windows, and new random ports (first run and the **Random** button in Settings) come from 20000–40000, outside that range.
- A torrent sent back to the download queue shows **Queued** straight away instead of **Paused** for a few seconds while the trackers are told it stopped.
- Tests run on Windows: `test_gui` no longer depends on the real desktop size or the real autostart registry entry, `test_chrome` waits for the window's start-up re-show before resizing and uses a temporary data folder instead of `%APPDATA%\BitTorrentClient`.

### 1.9.13
- **The aurora artwork now runs through the details pane** (dark themes): the tab row (Overview … Player), the Overview page and the idle Player tab draw it as one continuous picture. The Files, Peers, Trackers, Log and Speed pages keep their solid backgrounds so the tables stay easy to read; the light theme stays plain.
- The tab row is now drawn by the app (the selected tab gets an accent underline); clicking a tab name works as before.
- **The artwork now shows behind every table too**: the torrent list and the Files, Peers, Trackers and Log tables are drawn over it (new `arttable.py`, a canvas table that behaves like the old ones: click, Shift/Ctrl-click, arrow keys, mouse wheel, right-click menus, sortable headings), and the Speed graph draws its lines over it.
- The torrent list keeps the artwork **full size** however many torrents are added (it used to shrink to the space below the last row).

### 1.9.14
- **Network filter check:** a few seconds after start the app tests whether something on the PC is filtering local connections (AdGuard was found doing this: it drops the first UDP packet a new socket receives and resets or stalls some local connections, which breaks DHT start-up and playback inside the app at random). If so, the Log says so and **Diagnostics** names the program and what to exclude; **Rescan** repeats the check.
- **Progress bars are drawn into the list:** rounded bars in the state colour with a soft halo while downloading, on top of the artwork. They no longer lag behind (a finished torrent could show an empty bar until the next repaint).
- **Selection and hover:** the selected row gets an accent stripe and outline instead of a solid band, so the artwork stays visible; the row under the pointer gets a faint outline.
- **Light theme artwork:** a light version of the aurora behind the list and details pane (generated from the dark one by `tools/make_light_backdrop.py`).
- Switching between light and dark while running now also updates the title bar artwork (it kept the dark banner until a restart).
- `run_tests.py` prints the end of a failing test's output (including errors the app's crash log used to swallow) and keeps the whole output in `test_logs/`.

### 1.9.15
- **Settings → About:** About is now a page in the Settings list (under Connection) instead of a small button under Updates: version, credits, where your data lives, and Open data folder / Open error log.
- Building: `build.bat` and the GitHub build find Inno Setup 7 as well as 6.

### 1.9.16
- **Updating while the app runs:** the installer (and uninstaller) now closes a running copy first instead of stopping at "Setup was unable to automatically close all applications". Closing the window only hides the app to the tray, so Windows' own close request never worked; Setup now runs `BitTorrentClient.exe --quit`, which saves and exits like **Exit** in the tray menu. A copy that does not answer within about 12 s (such as 1.9.15 and older, which do not know `--quit`) is stopped.
- `BitTorrentClient.exe --quit` can also be used from scripts; it does nothing when the app is not running.

### 1.9.17
- **Fixed the "Windows fatal exception: access violation" entries in the error log.** They were not crashes: setting up the tray icon passed Windows a module handle that ctypes had cut down to 32 bits, Windows caught the resulting fault, and the error log recorded it on every start (or whenever the tray icon appeared). The handle is now passed at full size, so the tray icon is created correctly and the log stays clean.

### 1.9.18
- Port forwarding: once the router has mapped the ports, the status (e.g. "forwarded … via UPnP (public IP …)") is shown together with them. Before, the mappings were published while the router was still being asked for its public address, so the status bar could briefly keep saying "Searching for router…" (and a self-test failed about one run in seven).

### 1.9.19
- **Search online → All sources:** the first entry in the source list searches every source at once (built-in, your plugins and your RSS feeds). Results appear as each site answers; a torrent found on several sites is listed once (the copy with the most seeders); sites that fail are named in the status line and do not stop the others.
- **No result limit:** a checkbox next to the source list. Each source is asked for everything instead of up to 1000, and plugins that read result pages (they define `MAX_PAGES`) may read up to 100 pages, stopping when a site runs out. Some sites have their own ceiling (the Pirate Bay API returns at most 100). Both choices are remembered.

### 1.9.20
- **New Aurora icon** for the program, shortcuts, taskbar, tray and installer: a dark navy tile like the app window, with a thin blue edge, a soft glow and a download arrow shading from sky blue to violet (was a bright blue-to-teal tile). Generated by `python make_icon.py`, which now also runs on Windows (it wrote a preview to a Linux-only path).

### 1.9.21
- **Automatic updates.** About once a day the app looks for a new version, downloads it in the background, checks it against the size and SHA-256 checksum GitHub publishes, and asks whether to install it now (the app closes and reopens by itself) or when you next exit. Settings → General → Updates has "Keep Bit Torrent Client up to date automatically" and **Check now**; the GitHub repository field is gone - the app knows where its updates come from.
- Releases for updates are published to a separate public repository that holds only installers; the source code stays private.

#### Releases (for the maintainer)
1. Once: create a **public** GitHub repository `cfccrash/bittorrentclient-releases` (empty is fine).
2. Once: create a fine-grained personal access token with access to **only** that repository and the permission *Contents: Read and write*; add it to this (private) repository under *Settings → Secrets and variables → Actions* as `RELEASES_TOKEN`.
3. Each release: bump `version.py`, add its `### x.y.z` section here, commit, then push a matching tag: `git tag v1.9.21` and `git push origin v1.9.21`. The GitHub build makes the installer and publishes it as a release there, with that section as the notes; installed copies pick it up within a day.
4. Share **https://github.com/cfccrash/bittorrentclient-releases** (front page with screenshots) or the direct, never-changing
   download link **https://github.com/cfccrash/bittorrentclient-releases/releases/latest/download/BitTorrentClient-Setup.exe**
   (each release also carries the installer under that fixed name; the updater uses the versioned one).

### 1.9.22
- Maintenance release, and the first one delivered by the automatic updater: if you have 1.9.21 installed, it offers this version by itself.
- Release builds use Python 3.14 (as development does) and the current GitHub build actions (the old ones ran on a Node.js version GitHub is retiring).
- The install instructions at the top of this file point at the public releases page and describe building on an LTSC Windows (no winget).

### 1.9.23
- Updating now replaces the program's runtime folder completely instead of copying over it, so files an older version installed but a newer one no longer uses are removed (for example, graphics-library files from 1.9.21 were still lying next to the newer ones). Your settings and torrents are not in that folder and are not touched.

### 1.9.24
- **Toolbar lines up with the window below it:** Exit, Restart and Settings sit over the sidebar (Settings is now a proper button), a dividing line continues the sidebar's edge, and Resume / Pause / Remove / Files / Open folder start where the torrent list starts.

### 1.9.25
- **Windows Snap works with the Aurora title bar:** hover the maximise button for Snap Layouts (Windows 11), drag the title bar to a screen edge or corner, use Win + arrow keys, or double-click the title bar - the window now behaves like any other. It also resizes from its edges natively. (The main window keeps a real Windows frame behind the Aurora strip instead of being a borderless window.)
- The window can be made small enough to sit side by side with another window, or in a quarter of the screen.
- The left column is only as wide as its buttons need, and the Exit / Restart / Settings buttons above it are spread evenly across it.

### 1.9.26
- **Snap really works now** (1.9.25 did not): dragging the title bar to a screen edge or corner snaps the window, double-click maximises / restores, the edges resize it, and hovering the maximise button is answered like a standard Windows maximise button (Snap Layouts). Moving the window no longer flickers or leaves a ghost trail - Windows moves it itself.
- Pressing Alt on its own no longer makes the window stop responding until a click.

### 1.9.27
- **One aurora behind the whole window:** the artwork now runs corner to corner as a single picture - behind the title bar, toolbar, left column, torrent list, details tabs and status bar - with no lines or bands between them. The toolbar and status bar are drawn on it too (their buttons and links light up when you point at them).
- **The aurora artwork now runs under the left column:** the dividing line between the column and the torrent list is gone, and the artwork is centred on the column and list together and stretched wider, so its wings reach out across the column. The column's buttons (Add torrent, Magnet link, Search online, Create torrent, RSS feeds, Library) no longer sit in boxes: they are drawn straight on the artwork and only light up when you point at them.
- **Round tick boxes:** every tick box in the app (Settings, add / create torrent, RSS, search, the Files dialog and the "Download in order" menu item) is now a circle that lights up green when it is ticked.

### 1.9.28
- The left column has no boxes at all now: the chosen filter (All, Downloading ...) is shown in bold white text instead of on a blue bar, and pointing at a button or filter makes its text bold instead of drawing a box behind it.

### 1.9.29
- Toolbar: no divider line between Remove and Files, and no grey box behind the Settings button.
- The list's column headings (Name, Size, Progress ...) now show even when there are no torrents.
- Blue buttons (Search, Add, Download selected, Save ...) have white text in the Aurora theme, so they are easier to read.
- Toolbar: Resume, Pause, Remove, Files and Open folder now start exactly above the list's Name heading.
- **Full screen:** press F11, or click the new four-corners button left of minimise on the title bar, and the window covers the whole screen, taskbar included. F11, Esc or the same button bring it back to how it was (maximised or not).

### 1.9.30
- Full screen: Windows can no longer draw its own (old-style) title bar over the Aurora one.
- Clicking the minimise button no longer risks closing the app with an error.

### 1.9.31
- No line under the torrent list's column headings.

### 1.9.32
- **Settings drops down from the gear button:** it opens just under it, showing only the list of sections (General, Media, Downloads, Speed, Connection, About) with Save and Cancel. Click a section and its page opens beside the list, only as big as it needs; click it again to fold it away. A page taller than the screen scrolls (mouse wheel).

### 1.9.33
- **No more boxes anywhere:** buttons (Save, Cancel, Browse ...), the Settings sections, the details tabs, torrent rows, lists in dialogs, drop-down lists, tick boxes and the right-click menu no longer show a blue or grey box. Like the left column, whatever you point at - or have chosen - turns bold white instead. (Only the window's close button keeps its red.)
- Settings: no lines between the section list and its page, or around the groups of options.
- F11 works again after closing Settings or any other dialog (the keyboard went back nowhere).

### 1.9.34
- Settings: no border line around the drop-down, and the update options (automatic updates, Check now) moved from General to About.
- **The aurora shows through Settings and the other option windows** (Add torrent, magnet link, messages ...): their background is see-through onto the artwork, lined up with the main window's; text, tick boxes and input fields stay solid.

### 1.9.35
- **Settings behaves like a drop-down menu:** the rest of the window keeps working while it is open, and clicking anywhere else (the list, the full-screen button, the toolbar ...), pressing F11 or clicking the gear again closes it - the click or key still does its job. Resizing the window closes it too.
- A box of artwork could be left stuck on the screen after an option window closed; the see-through backgrounds now always clean themselves up.
