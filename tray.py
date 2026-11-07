"""Windows notification-area (system tray) icon using only ctypes.

A hidden message-only style window receives the icon's mouse messages. It lives on the Tk thread, so Tk's own
message loop delivers them - no extra thread is needed. On other systems `available()` is False and nothing happens."""
import os
import sys

WM_USER = 0x0400
WM_TRAY = WM_USER + 1
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIN_BALLOONUSERCLICK = WM_USER + 5
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
MF_STRING, MF_SEPARATOR = 0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_BOTTOMALIGN = 0x2, 0x100, 0x20
ID_OPEN, ID_QUIT = 1, 2


def available():
    return sys.platform == "win32"


class Tray:
    def __init__(self, on_open, on_quit, tip="Bit Torrent Client", icon_file=None):
        self.on_open, self.on_quit, self.tip = on_open, on_quit, tip[:127]
        self.icon_file = icon_file
        self.visible = False
        self.temporary = False     # shown only to deliver a notification
        self._hwnd = None
        self._hicon = None
        self._proc = None          # keep a reference: ctypes callbacks are freed when garbage collected
        self._created_msg = 0

    # -- Windows plumbing --------------------------------------------------
    def _setup(self):
        import ctypes
        from ctypes import wintypes
        self._ct, self._wt = ctypes, wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD)]
        self._NID = NOTIFYICONDATA

        # Every handle must be declared pointer-sized: ctypes' default return type is a 32-bit int, which cut the
        # module handle in half (0x7ff7... -> 0xffff...). RegisterClassW / CreateWindowExW then read through the bad
        # handle and raised an access violation that Windows swallowed - but faulthandler logged it on every start.
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          wintypes.HWND, wintypes.LPVOID]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyWindow.argtypes = [wintypes.HWND]

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_TRAY:
                    if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_BALLOONUSERCLICK):
                        self.on_open()
                    elif lparam == WM_RBUTTONUP:
                        self._menu()
                    return 0
                if self._created_msg and msg == self._created_msg and self.visible:   # Explorer restarted
                    self._notify(NIM_ADD)
                    return 0
            except Exception:
                pass
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(wndproc)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._proc
        cls.hInstance = kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "BitTorrentClientTray"
        user32.RegisterClassW(ctypes.byref(cls))
        self._hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "Bit Torrent Client tray", 0, 0, 0, 0, 0, None,
                                            None, cls.hInstance, None)
        self._created_msg = user32.RegisterWindowMessageW("TaskbarCreated")
        self._hicon = self._load_icon()

    def _load_icon(self):
        ct, user32 = self._ct, self._ct.windll.user32
        user32.LoadImageW.restype = self._wt.HANDLE
        user32.LoadImageW.argtypes = [self._wt.HINSTANCE, self._wt.LPCWSTR, self._wt.UINT, ct.c_int, ct.c_int, self._wt.UINT]
        cx, cy = user32.GetSystemMetrics(49), user32.GetSystemMetrics(50)   # SM_CXSMICON / SM_CYSMICON
        for path in (self.icon_file, sys.executable if getattr(sys, "frozen", False) else None):
            if not path or not os.path.isfile(path):
                continue
            if path.lower().endswith(".ico"):
                h = user32.LoadImageW(None, path, 1, cx, cy, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
                if h:
                    return h
            else:   # the packaged .exe carries the icon as a resource
                shell32 = ct.windll.shell32
                shell32.ExtractIconW.restype = self._wt.HICON
                shell32.ExtractIconW.argtypes = [self._wt.HINSTANCE, self._wt.LPCWSTR, self._wt.UINT]
                h = shell32.ExtractIconW(None, path, 0)
                if h and h != 1:
                    return h
        user32.LoadIconW.restype = self._wt.HICON
        user32.LoadIconW.argtypes = [self._wt.HINSTANCE, ct.c_void_p]
        return user32.LoadIconW(None, ct.c_void_p(32512))   # IDI_APPLICATION

    def _notify(self, op, balloon=None):
        d = self._NID()
        d.cbSize = self._ct.sizeof(self._NID)
        d.hWnd, d.uID = self._hwnd, 1
        d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        d.uCallbackMessage, d.hIcon, d.szTip = WM_TRAY, self._hicon, self.tip
        if balloon:
            d.uFlags |= NIF_INFO
            d.szInfoTitle, d.szInfo = balloon[0][:63], balloon[1][:255]
            d.dwInfoFlags = 1   # NIIF_INFO
        return self._ct.windll.shell32.Shell_NotifyIconW(op, self._ct.byref(d))

    def _menu(self):
        user32 = self._ct.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open Bit Torrent Client")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Exit")
        pt = self._wt.POINT()
        user32.GetCursorPos(self._ct.byref(pt))
        user32.SetForegroundWindow(self._hwnd)   # without this the menu does not close when you click elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_BOTTOMALIGN, pt.x, pt.y, 0, self._hwnd, None)
        user32.DestroyMenu(menu)
        if cmd == ID_OPEN:
            self.on_open()
        elif cmd == ID_QUIT:
            self.on_quit()

    # -- public API ----------------------------------------------------------
    def show(self, balloon=None):
        """Put the icon in the notification area (optionally with a one-off balloon message)."""
        if not available():
            return False
        try:
            if self._hwnd is None:
                self._setup()
            self.temporary = False
            if not self.visible:
                self.visible = bool(self._notify(NIM_ADD))
            if self.visible and balloon:
                self._notify(NIM_MODIFY, balloon)
            return self.visible
        except Exception:
            return False

    def notify(self, title, text):
        """Pop up a notification balloon, adding the icon first if it is not there (it then counts as temporary)."""
        if not available():
            return False
        try:
            if self._hwnd is None:
                self._setup()
            if not self.visible:
                self.visible = bool(self._notify(NIM_ADD))
                self.temporary = self.visible
            if self.visible:
                self._notify(NIM_MODIFY, (title, text))
            return self.visible
        except Exception:
            return False

    def set_tip(self, text):
        text = text[:127]
        if text == self.tip:
            return
        self.tip = text
        if self.visible:
            try:
                self._notify(NIM_MODIFY)
            except Exception:
                pass

    def hide(self):
        if self.visible:
            try:
                self._notify(NIM_DELETE)
            except Exception:
                pass
            self.visible = False
            self.temporary = False

    def destroy(self):
        self.hide()
        if self._hwnd:
            try:
                self._ct.windll.user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
