"""Linux (freedesktop) integration: the menu entry, magnet links, start at sign-in, opening folders, desktop
notifications and the Pictures folder. Plain files under ~/.local/share and ~/.config plus the standard xdg-* tools,
so it works on Arch and any other desktop that follows freedesktop.org. Stdlib only."""
import os
import shlex
import shutil
import subprocess
import sys

DESKTOP_ID = "bittorrentclient.desktop"
NAME = "Bit Torrent Client"


def is_linux():
    return sys.platform.startswith("linux")


def _xdg(var, default):
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), default)


def data_home():
    """~/.local/share (or $XDG_DATA_HOME)."""
    return _xdg("XDG_DATA_HOME", os.path.join(".local", "share"))


def config_home():
    return _xdg("XDG_CONFIG_HOME", ".config")


def command(*extra):
    """How to start this program: the packaged binary / launcher, or python + the script when run from source."""
    if getattr(sys, "frozen", False):
        args = [sys.executable]
    else:
        launcher = shutil.which("bittorrentclient")
        args = [launcher] if launcher else [sys.executable, os.path.abspath(sys.argv[0])]
    return " ".join(shlex.quote(a) for a in args + list(extra))


def icon_path():
    """The app icon as a PNG file (written once into the data folder when there is no installed one)."""
    for p in ("/usr/share/pixmaps/bittorrentclient.png", "/usr/share/icons/hicolor/256x256/apps/bittorrentclient.png"):
        if os.path.isfile(p):
            return p
    p = os.path.join(data_home(), "BitTorrentClient", "bittorrentclient.png")
    if not os.path.isfile(p):
        try:
            import make_icon
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(make_icon.png(256))
        except Exception:
            return "bittorrentclient"
    return p


def desktop_entry(extra_args="%U", hidden=False):
    """A freedesktop .desktop file for this program (menu entry; magnet links and .torrent files open in it)."""
    lines = ["[Desktop Entry]", "Type=Application", "Name=%s" % NAME, "GenericName=BitTorrent Client",
             "Comment=Download and stream torrents", "Exec=%s" % command(*(extra_args.split() if extra_args else ())),
             "Icon=%s" % icon_path(), "Terminal=false", "Categories=Network;FileTransfer;P2P;",
             "MimeType=x-scheme-handler/magnet;application/x-bittorrent;", "StartupWMClass=Bittorrentclient"]
    if hidden:
        lines.append("X-GNOME-Autostart-enabled=true")
    return "\n".join(lines) + "\n"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def install_menu_entry():
    """The user's own menu entry (~/.local/share/applications), unless the package installed one already."""
    system = "/usr/share/applications/" + DESKTOP_ID
    if os.path.isfile(system):
        return system
    path = os.path.join(data_home(), "applications", DESKTOP_ID)
    _write(path, desktop_entry())
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(path)], capture_output=True)
    return path


def register_magnet():
    """Make this program the handler for magnet links and .torrent files. Returns (ok, message)."""
    try:
        install_menu_entry()
    except OSError as e:
        return False, "Could not write the menu entry: %s" % e
    if not shutil.which("xdg-mime"):
        return False, "xdg-mime is missing (install the xdg-utils package)."
    ok = True
    for mime in ("x-scheme-handler/magnet", "application/x-bittorrent"):
        r = subprocess.run(["xdg-mime", "default", DESKTOP_ID, mime], capture_output=True, text=True)
        ok = ok and r.returncode == 0
    return ok, ("Done. Magnet links and .torrent files now open in %s." % NAME if ok
                else "xdg-mime could not set the default application.")


AUTOSTART = lambda: os.path.join(config_home(), "autostart", DESKTOP_ID)


def autostart_enabled():
    return os.path.isfile(AUTOSTART())


def set_autostart(on):
    try:
        if on:
            _write(AUTOSTART(), desktop_entry("--minimized", hidden=True))
        elif os.path.isfile(AUTOSTART()):
            os.remove(AUTOSTART())
        return True
    except OSError:
        return False


def open_path(path):
    """Show a folder (or open a file) in the desktop's file manager."""
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if not opener:
        return False
    args = [opener, path] if not opener.endswith("gio") else [opener, "open", path]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def notify(title, text):
    """A desktop notification (libnotify's notify-send), if the system has one."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    subprocess.Popen([exe, "--app-name=%s" % NAME, "--icon=%s" % icon_path(), title, text],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def pictures_dir():
    """The user's Pictures folder (xdg-user-dir knows its localised name)."""
    if shutil.which("xdg-user-dir"):
        try:
            p = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True, timeout=3).stdout.strip()
            if p and os.path.isdir(p) and p != os.path.expanduser("~"):
                return p
        except (OSError, subprocess.SubprocessError):
            pass
    return os.path.join(os.path.expanduser("~"), "Pictures")
