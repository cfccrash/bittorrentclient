"""Start at sign-in: on Windows a per-user Run entry (no administrator rights needed), on Linux a .desktop file in
~/.config/autostart (see linux.py)."""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "BitTorrentClient"      # the installer writes the same value, so the two stay in step


def available():
    return sys.platform == "win32" or sys.platform.startswith("linux")


def _linux():
    return sys.platform.startswith("linux")


def command():
    if getattr(sys, "frozen", False):
        return '"%s" --minimized' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return '"%s" "%s" --minimized' % (pyw, os.path.abspath(sys.argv[0]))


def is_enabled():
    if not available():
        return False
    if _linux():
        import linux
        return linux.autostart_enabled()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, NAME)
        return True
    except OSError:
        return False


def set_enabled(on):
    """True if the registry (Linux: the autostart file) now matches `on`."""
    if not available():
        return False
    if _linux():
        import linux
        return linux.set_autostart(on)
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(k, NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
