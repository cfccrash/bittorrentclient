"""Drag .torrent files from Explorer onto the window (Windows only, ctypes; WM_DROPFILES)."""
import sys

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4


def available():
    return sys.platform == "win32"


def enable(root, callback):
    """callback(list_of_paths) is called on the Tk thread (deferred with `after`, never inside the window procedure)."""
    if not available():
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell32.DragFinish.argtypes = [ctypes.c_void_p]
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        old = [None]

        def proc(h, msg, wparam, lparam):
            # nothing in here may raise: an exception inside a window procedure must never take the app down
            try:
                if msg == WM_DROPFILES:
                    paths = []
                    try:
                        n = min(shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0), 200)
                        for i in range(n):
                            buf = ctypes.create_unicode_buffer(32768)
                            if shell32.DragQueryFileW(wparam, i, buf, len(buf)):
                                paths.append(buf.value)
                    except Exception:
                        pass
                    finally:
                        try:
                            shell32.DragFinish(wparam)
                        except Exception:
                            pass
                    try:
                        root.after(10, lambda: callback(paths))
                    except Exception:
                        pass
                    return 0
                return user32.CallWindowProcW(old[0], h, msg, wparam, lparam)
            except Exception:
                return 0

        cb = WNDPROC(proc)
        enable.keep = (cb, proc)                          # ctypes callbacks must stay referenced
        old[0] = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))
        shell32.DragAcceptFiles(hwnd, True)
        return True
    except Exception:
        return False
