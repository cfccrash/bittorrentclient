"""Show an external player (VLC, MPC-HC, mpv ...) inside a frame of the main window (Windows only, best effort).

The player is started normally; once its window exists it is re-parented into the frame (SetParent), stripped of its
title bar and borders, and kept the size of the frame. `release()` gives it its own window back. Everything that touches
Windows is behind small functions so the logic can be tested with fakes. Players that hand the file to an
already-running copy of themselves (single-instance mode) have no window of their own to take; that is reported
through on_fail and the player simply stays in its own window."""
import sys

WS_CHILD, WS_VISIBLE = 0x40000000, 0x10000000
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU, WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_POPUP = 0xC00000, 0x40000, 0x80000, 0x20000, 0x10000, 0x80000000
GWL_STYLE = -16


class _Win:
    """The few Win32 calls needed (created lazily so the module imports everywhere)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        u = ctypes.windll.user32
        self.u = u
        H = ctypes.c_void_p
        u.SetParent.argtypes = [H, H]
        u.SetParent.restype = H
        u.MoveWindow.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
        u.GetWindowLongPtrW.argtypes = [H, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [H, ctypes.c_int, ctypes.c_ssize_t]
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
        u.IsWindowVisible.argtypes = [H]
        u.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowTextLengthW.argtypes = [H]
        u.ShowWindow.argtypes = [H, ctypes.c_int]
        u.SetFocus.argtypes = [H]
        u.IsWindow.argtypes = [H]
        self.EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, H, wintypes.LPARAM)

    def find(self, pid):
        """The biggest visible, titled top-level window of process `pid`, or None."""
        ct, wt = self.ct, self.wt
        found = []

        def cb(hwnd, _l):
            p = wt.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ct.byref(p))
            if p.value == pid and self.u.IsWindowVisible(hwnd) and self.u.GetWindowTextLengthW(hwnd) > 0:
                r = wt.RECT()
                self.u.GetWindowRect(hwnd, ct.byref(r))
                found.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
            return True
        self.u.EnumWindows(self.EnumProc(cb), 0)
        return max(found)[1] if found else None

    def style(self, hwnd):
        return self.u.GetWindowLongPtrW(hwnd, GWL_STYLE)

    def set_style(self, hwnd, v):
        self.u.SetWindowLongPtrW(hwnd, GWL_STYLE, v)

    def parent(self, hwnd, parent):
        self.u.SetParent(hwnd, parent)

    def move(self, hwnd, x, y, w, h):
        self.u.MoveWindow(hwnd, x, y, w, h, True)

    def rect(self, hwnd):
        r = self.wt.RECT()
        self.u.GetWindowRect(hwnd, self.ct.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def alive(self, hwnd):
        return bool(self.u.IsWindow(hwnd))

    def keys(self, hwnd, vks):
        """Press a chord (e.g. Ctrl+1) in another process's window: briefly share its input queue and focus it."""
        ct = self.ct
        k32 = ct.windll.kernel32
        me = k32.GetCurrentThreadId()
        pid = self.wt.DWORD()
        them = self.u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        self.u.AttachThreadInput(me, them, True)
        try:
            self.u.SetFocus(hwnd)
            for vk in vks:
                self.u.keybd_event(vk, 0, 0, 0)
            for vk in reversed(vks):
                self.u.keybd_event(vk, 0, 2, 0)           # KEYEVENTF_KEYUP
        finally:
            self.u.AttachThreadInput(me, them, False)


class Embedded:
    def __init__(self, root, host, proc, on_end=None, on_fail=None, win=None, tries=60, attach=True, on_attached=None):
        self.root, self.host, self.proc = root, host, proc
        self.on_end, self.on_fail, self.adopt, self.on_attached = on_end, on_fail, attach, on_attached
        self.win = win or _Win()
        self.hwnd, self.saved, self.tries, self.closed = None, None, tries, False
        self._bind = None
        self._job = root.after(300, self._poll)

    # -- finding the window
    def _poll(self):
        self._job = None
        if self.closed:
            return
        if self.adopt and self.proc.poll() is not None and not self.hwnd:       # exited (or handed over to a running copy) before showing a window
            self._fail("The player closed before showing a window.")
            return
        if not self.adopt:
            if self.proc.poll() is not None:
                self.closed = True
                self.on_end and self.on_end()
                return
        elif self.hwnd:
            if self.proc.poll() is not None or not self.win.alive(self.hwnd):
                self.closed = True
                self.on_end and self.on_end()
                return
        else:
            h = self.win.find(self.proc.pid)
            if h:
                self._attach(h)
            else:
                self.tries -= 1
                if self.tries <= 0:
                    self._fail("The player did not show a window to take over.")
                    return
        self._job = self.root.after(300, self._poll)

    def _fail(self, why):
        self.closed = True
        self.on_fail and self.on_fail(why)

    # -- taking it over
    def _attach(self, hwnd):
        w = self.win
        self.hwnd = hwnd
        self.saved = (w.style(hwnd), w.rect(hwnd))
        st = (self.saved[0] & ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_POPUP)) \
            | WS_CHILD | WS_VISIBLE
        w.set_style(hwnd, st)
        w.parent(hwnd, self.host.winfo_id())
        self._bind = self.host.bind("<Configure>", lambda e: self.fit(), add="+")
        self.fit()
        if self.on_attached:
            self.root.after(500, self.on_attached)        # let the player finish drawing first

    def keys(self, *vks):
        if self.hwnd and not self.closed:
            self.win.keys(self.hwnd, vks)

    def fit(self):
        if self.hwnd and not self.closed:
            self.win.move(self.hwnd, 0, 0, max(self.host.winfo_width(), 1), max(self.host.winfo_height(), 1))

    # -- giving it back / ending it
    def release(self):
        """Back to a normal window of its own (keeps playing)."""
        if self.hwnd and self.saved and not self.closed:
            w = self.win
            try:
                w.parent(self.hwnd, None)
                w.set_style(self.hwnd, self.saved[0])
                x, y, cw, ch = self.saved[1]
                w.move(self.hwnd, x, y, cw, ch)
            except Exception:
                pass
        self._stop_watching()

    def stop(self):
        """Close the player."""
        self._stop_watching()
        try:
            self.proc.terminate()
        except Exception:
            pass

    def _stop_watching(self):
        self.closed = True
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self._bind:
            try:
                self.host.unbind("<Configure>", self._bind)
            except Exception:
                pass
            self._bind = None


VK_CONTROL, VK_MENU, VK_RETURN, VK_1, VK_3 = 0x11, 0x12, 0x0D, 0x31, 0x33


def player_kind(exe):
    n = (exe or "").lower()
    return "mpc" if "mpc-" in n else "vlc" if "vlc" in n else "mpv" if "mpv" in n else "other"


def launch_args(exe, target, wid=None):
    """Command line for each player: its own instance, no clutter, and (mpv) drawing straight into our frame."""
    k = player_kind(exe)
    if k == "mpc":
        return [exe, target, "/new"]                    # /new: own window even if MPC-HC is already open
    if k == "vlc":
        return [exe, "--no-one-instance", "--qt-minimal-view", "--no-video-title-show", "--no-qt-fs-controller", target]
    if k == "mpv" and wid:
        return [exe, "--wid=%d" % wid, "--force-window=yes", "--keep-open=yes", target]
    return [exe, target]


def available():
    return sys.platform == "win32"
