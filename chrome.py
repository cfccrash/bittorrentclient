"""Frameless windows with a title strip of our own (aurora artwork, minimise / maximise / close), drag to move,
edge and corner grips to resize. Replaces the standard Windows title bar so every window shares the app's look.

Windows details: a borderless Tk window loses its taskbar button and cannot minimise, so the extended style is
switched to "app window" and minimising goes through ShowWindow. Everything is best effort: if anything fails the
window simply keeps the standard frame (see enabled())."""
import os
import sys
import tkinter as tk
from tkinter import font as tkfont

STRIP = "#080A19"          # colour at the edges of the artwork, so the picture melts into the strip
_banner_cache = {}


def enabled(cfg=None):
    """Custom frames on Windows (and wherever BTC_CHROME=1 forces them, used by the tests)."""
    if os.environ.get("BTC_CHROME") == "0":
        return False
    if os.environ.get("BTC_CHROME") == "1":
        return True
    return os.name == "nt" and not (cfg or {}).get("native_frame", False)


def _banner(win, height):
    """The aurora band as a PhotoImage scaled to roughly `height` pixels (cached per window)."""
    key = (str(win), height)
    if key in _banner_cache:
        return _banner_cache[key]
    img = None
    try:
        import assets_ui
        img = tk.PhotoImage(data=assets_ui.BANNER, master=win)        # 1584 x 72
        n = max(1, round(img.height() / max(1, height)))
        if n > 1:
            img = img.subsample(n, n)
    except Exception:
        img = None
    _banner_cache[key] = img
    return img


def _hwnd(win):
    import ctypes
    return ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()


class Chrome:
    def __init__(self, win, P, px, title="", main=False, resizable=True, buttons=("min", "max", "close"), on_close=None,
                 font=None, before=None, snap=False):
        self.win, self.P, self.px, self.main = win, P, px, main
        win._chrome = self
        self.resizable = resizable
        self.buttons = list(buttons) if main else ["close"]
        self.on_close = on_close
        self.maxed = False
        self.normal_geo = None
        self.full = False
        self.hover = None
        self.title_text = title
        self.h = px(38 if main else 34)
        self.font = font or tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.bw = px(46)                                                   # width of each caption button
        # snap: keep a real Windows frame (hidden behind our strip) so Snap Layouts, drag-to-edge snapping, Win+arrows
        # and native resizing work - see NativeFrame. Otherwise: a borderless window that we move and size ourselves.
        self.native = NativeFrame(self) if (snap and os.name == "nt" and resizable) else None
        if not self.native:
            win.overrideredirect(True)
        try:
            win.configure(highlightthickness=1, highlightbackground=P["border"], highlightcolor=P["border"])
        except tk.TclError:
            pass
        self.bar = tk.Canvas(win, height=self.h, bd=0, highlightthickness=0, bg=STRIP, cursor="arrow")
        if before is not None:
            self.bar.pack(side="top", fill="x", before=before)
        else:
            kids = win.pack_slaves()
            self.bar.pack(side="top", fill="x", **({"before": kids[0]} if kids else {}))
        self.img = _banner(win, self.h) if P.get("bg", "#fff")[1:2] < "8" else None      # artwork only on dark themes
        self.art = None        # optional fn(canvas): the owner draws its own artwork behind the strip (main window)
        self.bar.bind("<Configure>", lambda e: self.draw())
        self.bar.bind("<Motion>", self._motion)
        self.bar.bind("<Leave>", lambda e: self._set_hover(None))
        self.bar.bind("<ButtonPress-1>", self._press)
        self.bar.bind("<B1-Motion>", self._drag)
        self.bar.bind("<ButtonRelease-1>", self._release)
        self.bar.bind("<Double-Button-1>", self._double)
        win.bind("<Alt-F4>", lambda e: self.close())
        self.grips = []
        if resizable:                                                  # native frame: the grips start Windows' own resize
            self._make_grips()
        self._press_info = None
        win.after(30, self._after_map)
        self.draw()

    def set_resizable(self, on):
        on = bool(on)
        if on and not self.grips:
            self.resizable = True
            self._make_grips()
            self.lift_grips()
        self.resizable = on
        self._grips_visible(on)

    # ---- drawing
    def set_title(self, text):
        self.title_text = text
        self.draw()

    def restyle(self, P):
        self.P = P
        self.img = _banner(self.win, self.h) if P.get("bg", "#fff")[1:2] < "8" else None   # theme may have changed
        try:
            self.win.configure(highlightbackground=P["border"], highlightcolor=P["border"])
        except tk.TclError:
            pass
        self.draw()

    def _button_boxes(self):
        w = self.bar.winfo_width()
        out, x = {}, w
        for name in reversed(self.buttons):
            x -= self.bw
            out[name] = (x, 0, x + self.bw, self.h)
        return out

    def draw(self):
        c, P, h = self.bar, self.P, self.h
        c.delete("all")
        w = max(c.winfo_width(), 1)
        if self.art is not None:              # one picture across the whole window, the strip included
            c.configure(bg=P["bg"])
            self.art(c)
        elif self.img:
            c.configure(bg=STRIP)
            c.create_image(w // 2, h // 2, image=self.img)
        else:
            c.configure(bg=P["surface2"])
            c.create_line(0, h - 1, w, h - 1, fill=P["border"])
        c.create_text(self.px(14), h // 2, text=self.title_text, anchor="w", fill=P["fg"], font=self.font)
        glyph = P["fg"]
        for name, (x0, y0, x1, y1) in self._button_boxes().items():
            hot = self.hover == name
            if hot:
                c.create_rectangle(x0, y0, x1, y1, fill="#C42B1C" if name == "close" else P["hover"], width=0)
            cx, cy, s = (x0 + x1) // 2, (y0 + y1) // 2, self.px(5)
            col = "#FFFFFF" if hot and name == "close" else glyph
            lw = max(1, self.px(1))
            if name == "min":
                c.create_line(cx - s, cy, cx + s + 1, cy, fill=col, width=lw)
            elif name == "max":
                if self.maxed:
                    c.create_rectangle(cx - s + 2, cy - s, cx + s, cy + s - 2, outline=col, width=lw)
                    c.create_rectangle(cx - s, cy - s + 2, cx + s - 2, cy + s, outline=col, width=lw, fill=c["bg"] if not hot else P["hover"])
                else:
                    c.create_rectangle(cx - s, cy - s, cx + s, cy + s, outline=col, width=lw)
            elif name == "close":
                c.create_line(cx - s, cy - s, cx + s + 1, cy + s + 1, fill=col, width=lw)
                c.create_line(cx - s, cy + s, cx + s + 1, cy - s - 1, fill=col, width=lw)

    def _which(self, x, y):
        for name, (x0, y0, x1, y1) in self._button_boxes().items():
            if x0 <= x < x1 and 0 <= y < y1:
                return name
        return None

    def _set_hover(self, name):
        if name != self.hover:
            self.hover = name
            self.draw()

    def _motion(self, e):
        self._set_hover(self._which(e.x, e.y))

    # ---- move / buttons
    def _press(self, e):
        name = self._which(e.x, e.y)
        if name:
            self._press_info = ("btn", name)
            return
        if self.native and not self.full:          # Windows takes over once the mouse really moves (see _drag)
            self._press_info = ("native", e.x_root, e.y_root)
            return
        self._press_info = ("drag", e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y())

    def _drag(self, e):
        info = self._press_info
        if info and info[0] == "native":           # past the drag threshold: Windows moves it (snapping, no trail)
            if self.native.past_drag_threshold(e.x_root - info[1], e.y_root - info[2]):
                self._press_info = None
                self.native.begin_move(info[1], info[2])
            return
        if not info or info[0] != "drag" or self.full:
            return
        _k, sx, sy, wx, wy = info
        if self.maxed:                                               # dragging a maximised window restores it first
            frac = (sx - wx) / max(1, self.win.winfo_width())
            self.restore()
            wx = sx - int(self.win.winfo_width() * frac)
            wy = sy - self.px(12)
            self._press_info = ("drag", sx, sy, wx, wy)
        self.win.geometry("+%d+%d" % (wx + e.x_root - sx, wy + e.y_root - sy))

    def _release(self, e):
        info, self._press_info = self._press_info, None
        if info and info[0] == "btn" and self._which(e.x, e.y) == info[1]:
            {"min": self.minimize, "max": self.toggle_max, "close": self.close}[info[1]]()

    def _double(self, e):
        if "max" in self.buttons and not self._which(e.x, e.y):
            self.toggle_max()

    def close(self):
        if self.on_close:
            return self.on_close()
        cmd = self.win.protocol("WM_DELETE_WINDOW")
        if cmd:
            self.win.tk.call(cmd)
        else:
            self.win.destroy()

    def minimize(self):
        try:
            import ui
            ui.Tooltip.hide_all()
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(_hwnd(self.win), 6)       # SW_MINIMIZE
        except Exception:
            try:
                self.win.iconify()
            except tk.TclError:
                pass

    def toggle_max(self):
        self.restore() if self.maxed else self.maximize()

    def maximize(self):
        if self.maxed or self.full:
            return
        if self.native:                       # a real maximise: Windows keeps the restore size, Win+Down undoes it
            self.native.show(3)               # SW_MAXIMIZE
            return
        import ui
        self.normal_geo = self.win.geometry()
        x, y, w, h = ui.work_area(self.win)
        self.maxed = True
        self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._grips_visible(False)
        self.draw()

    def restore(self):
        if not self.maxed:
            return
        if self.native:
            self.native.show(9)               # SW_RESTORE
            return
        self.maxed = False
        if self.normal_geo:
            self.win.geometry(self.normal_geo)
        self._grips_visible(True)
        self.draw()

    def fullscreen(self, on):
        """Whole-screen mode for a video window: no strip, no grips."""
        if on == self.full:
            return
        if on:
            self.normal_geo = self.win.geometry()
            self.full = True
            self.bar.pack_forget()
            self._grips_visible(False)
            self.win.geometry("%dx%d+0+0" % (self.win.winfo_screenwidth(), self.win.winfo_screenheight()))
        else:
            self.full = False
            kids = [k for k in self.win.pack_slaves() if k is not self.bar]
            self.bar.pack(side="top", fill="x", **({"before": kids[0]} if kids else {}))
            if self.normal_geo:
                self.win.geometry(self.normal_geo)
            self._grips_visible(not self.maxed)

    # ---- resize grips
    def _make_grips(self):
        t = max(4, self.px(5))
        spec = {"n": (dict(relx=0, y=0, relwidth=1, height=t), "size_ns"), "s": (dict(relx=0, rely=1, anchor="sw", relwidth=1, height=t), "size_ns"),
                "w": (dict(x=0, rely=0, width=t, relheight=1), "size_we"), "e": (dict(relx=1, rely=0, anchor="ne", width=t, relheight=1), "size_we"),
                "nw": (dict(x=0, y=0, width=2 * t, height=2 * t), "size_nw_se"), "ne": (dict(relx=1, y=0, anchor="ne", width=2 * t, height=2 * t), "size_ne_sw"),
                "sw": (dict(x=0, rely=1, anchor="sw", width=2 * t, height=2 * t), "size_ne_sw"), "se": (dict(relx=1, rely=1, anchor="se", width=2 * t, height=2 * t), "size_nw_se")}
        x11 = {"size_ns": "sb_v_double_arrow", "size_we": "sb_h_double_arrow", "size_nw_se": "bottom_right_corner",
               "size_ne_sw": "bottom_left_corner"}
        for edge, (place, cursor) in spec.items():
            if os.name != "nt":
                cursor = x11[cursor]
            g = tk.Frame(self.win, cursor=cursor, bd=0, highlightthickness=0, bg=self.P["bg"])
            g.place(**place)
            g.bind("<ButtonPress-1>", lambda e, edge=edge: self._rs_start(e, edge))
            g.bind("<B1-Motion>", self._rs_move)
            self.grips.append((g, place))

    def _grips_visible(self, on):
        for g, place in self.grips:
            if on:
                g.place(**place)
                g.lift()
            else:
                g.place_forget()

    def lift_grips(self):
        for g, _p in self.grips:
            if g.winfo_manager():
                g.lift()

    def _rs_start(self, e, edge):
        if self.native:                            # Windows' resize loop (snap-aware, no redraw trail)
            self._rs = None
            self.native.begin_resize(edge, e.x_root, e.y_root)
            return
        w = self.win
        self._rs = (edge, e.x_root, e.y_root, w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())

    def _rs_move(self, e):
        if not getattr(self, "_rs", None) or self.maxed:
            return
        edge, sx, sy, x, y, w, h = self._rs
        dx, dy = e.x_root - sx, e.y_root - sy
        mw, mh = self.win.minsize()
        nx, ny, nw, nh = x, y, w, h
        if "e" in edge:
            nw = max(mw, w + dx)
        if "s" in edge:
            nh = max(mh, h + dy)
        if "w" in edge:
            nw = max(mw, w - dx)
            nx = x + (w - nw)
        if "n" in edge:
            nh = max(mh, h - dy)
            ny = y + (h - nh)
        self.win.geometry("%dx%d+%d+%d" % (nw, nh, nx, ny))

    # ---- Windows specifics
    def _after_map(self):
        self.lift_grips()
        if os.name != "nt":
            return
        if self.native:
            self.native.install()
            return
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = _hwnd(self.win)
            u = ctypes.windll.user32
            if self.main:                                              # taskbar button + Alt-Tab entry for a borderless window
                ex = u.GetWindowLongW(hwnd, -20)
                u.SetWindowLongW(hwnd, -20, (ex & ~0x80) | 0x40000)
                if self.win.state() == "normal":
                    self.win.withdraw()
                    self.win.after(20, self.win.deiconify)
            dwm = ctypes.windll.dwmapi

            class MARGINS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]
            dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(MARGINS(1, 1, 1, 1)))      # soft drop shadow
            v = ctypes.c_int(2)
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(v), ctypes.sizeof(v))          # rounded corners (Windows 11)
        except Exception:
            pass


class NativeFrame:
    """A real Windows frame whose title bar is replaced by the Chrome strip, so Windows treats the window like any
    other: drag the strip to snap it to an edge, hover the maximise button for Snap Layouts (Windows 11),
    Win+arrow keys, double-click to maximise, resize from the edges.

    How: the window keeps WS_OVERLAPPEDWINDOW; its procedure is subclassed so that
      WM_NCCALCSIZE says the whole window is client area (no standard title bar or border is drawn), and
      WM_NCHITTEST tells Windows what is under the pointer - edges -> resize, the strip -> caption, our maximise
      button -> HTMAXBUTTON (that is what shows the Snap Layouts flyout).
    The procedure runs inside Windows' message dispatch, so it never calls Tk: it works from Win32 calls and plain
    numbers; maximise-button hover and the maximised state are picked up by a short Tk timer (poll)."""
    HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
    EDGES = {(0, 0): 13, (1, 0): 12, (2, 0): 14, (0, 1): 10, (2, 1): 11, (0, 2): 16, (1, 2): 15, (2, 2): 17}

    def __init__(self, chrome):
        self.c = chrome
        self.hwnd = None
        self._proc = self._old = None
        self.nc_hover = False          # pointer over our maximise button (non-client area: Tk does not see it)
        self.zoomed = False

    # -- set up
    def install(self):
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        self.u, self.ct, self.wt = u, ctypes, wintypes
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.CallWindowProcW.restype = LRESULT
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SetWindowLongPtrW.restype = ctypes.c_void_p
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        u.GetParent.restype = wintypes.HWND
        u.GetParent.argtypes = [wintypes.HWND]
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.SendMessageW.restype = LRESULT
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.ReleaseCapture.argtypes = []
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetDoubleClickTime.restype = wintypes.UINT
        self._child_old = {}
        win = self.c.win
        self.hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        self._proc = self.WNDPROC(self._wndproc)                      # keep a reference: freed callbacks crash
        self._old = u.SetWindowLongPtrW(self.hwnd, -4, self.ct.cast(self._proc, ctypes.c_void_p))   # GWLP_WNDPROC
        u.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0027)            # SWP_FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER
        # the strip canvas and the toplevel's client window sit between the pointer and the frame
        self._pass_through([h for h in (self.c.bar.winfo_id(), win.winfo_id()) if h and h != self.hwnd])
        try:
            dwm = ctypes.windll.dwmapi
            v = ctypes.c_int(2)
            dwm.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(v), ctypes.sizeof(v))   # rounded corners (Windows 11)
        except Exception:
            pass
        self._poll()

    def show(self, cmd):
        if self.hwnd:
            self.u.ShowWindow(self.hwnd, cmd)
            self.c.maxed = bool(self.u.IsZoomed(self.hwnd))
            self.c._grips_visible(not self.c.maxed)
            self.c.draw()

    # -- moving / resizing through Windows' own loops (what a real title bar / border does)
    def _nc_down(self, ht, x, y):
        """Start Windows' own move (HTCAPTION) or size (edge code) loop. Posted, not sent: the Tk click handler returns
        first and Windows runs the loop from the normal message queue. (Sending WM_NCLBUTTONDOWN from inside the Tk
        handler nested the loop in the callback and it never saw the button go up.)"""
        if not self.hwnd:
            return
        self.u.ReleaseCapture()                                       # Tk holds the mouse after a press
        if ht == self.HTCAPTION:
            sc = 0xF012                                                # SC_MOVE | HTCAPTION: move with the mouse
        else:
            sc = 0xF000 | {10: 1, 11: 2, 12: 3, 13: 4, 14: 5, 15: 6, 16: 7, 17: 8}[ht]   # SC_SIZE | WMSZ_* edge
        self.u.PostMessageW(self.hwnd, 0x0112, sc, ((y & 0xFFFF) << 16) | (x & 0xFFFF))   # WM_SYSCOMMAND

    def past_drag_threshold(self, dx, dy):
        g = self.u.GetSystemMetrics
        return abs(dx) > g(68) or abs(dy) > g(69)                     # SM_CXDRAG / SM_CYDRAG

    def begin_move(self, x, y):
        """Windows moves the window from here on (snapping to edges and corners, Snap Assist, no redraw trail)."""
        self._nc_down(self.HTCAPTION, x, y)

    def begin_resize(self, edge, x, y):
        ht = {"w": 10, "e": 11, "n": 12, "nw": 13, "ne": 14, "s": 15, "sw": 16, "se": 17}[edge]
        self._nc_down(ht, x, y)

    # -- Snap Layouts: the strip's own windows let the hit test through over our maximise button
    def _pass_through(self, hwnds):
        """Every Tk widget is a child window, and Windows asks the window under the pointer first. Over our maximise
        button the strip canvas and the toplevel's client window answer HTTRANSPARENT, so the question reaches the
        frame, which answers HTMAXBUTTON - that is what makes Windows 11 show the Snap Layouts flyout."""
        self._child_procs = []
        for h in hwnds:
            def proc(hwnd, msg, wp, lp, _h=h):
                try:
                    if msg == 0x0084 and self._over_max(lp):
                        return -1                                      # HTTRANSPARENT
                except Exception:
                    pass
                return self.u.CallWindowProcW(self._child_old[_h], hwnd, msg, wp, lp)
            cb = self.WNDPROC(proc)
            self._child_procs.append(cb)
            self._child_old[h] = self.u.SetWindowLongPtrW(h, -4, self.ct.cast(cb, self.ct.c_void_p))

    def _over_max(self, lp):
        ct, wt, u = self.ct, self.wt, self.u
        x, y = ct.c_short(lp & 0xFFFF).value, ct.c_short((lp >> 16) & 0xFFFF).value
        pt = wt.POINT(x, y); u.ScreenToClient(self.hwnd, ct.byref(pt))
        cr = wt.RECT(); u.GetClientRect(self.hwnd, ct.byref(cr))
        c = self.c
        if c.full or "max" not in c.buttons or not 0 <= pt.y < c.h:
            return False
        i = list(reversed(c.buttons)).index("max")
        return cr.right - (i + 1) * c.bw <= pt.x < cr.right - i * c.bw

    # -- Tk side: reflect what happened in the window procedure
    def _poll(self):
        c = self.c
        try:
            z = bool(self.u.IsZoomed(self.hwnd))
            hov = "max" if self.nc_hover else (c.hover if c.hover != "max" else None)
            if z != c.maxed or hov != c.hover:
                if z != c.maxed:
                    c._grips_visible(not z)                            # no resizing a maximised window
                c.maxed, c.hover = z, hov
                c.draw()
            c.win.after(60, self._poll)
        except Exception:
            pass

    # -- the window procedure
    def _frame_size(self):
        g = self.u.GetSystemMetrics
        return g(32) + g(92)          # SM_CXSIZEFRAME + SM_CXPADDEDBORDER

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == 0x0083 and wp:                                   # WM_NCCALCSIZE: all of it is client area
                if self.u.IsZoomed(hwnd):                              # a maximised window overhangs the screen by its frame
                    r = self.ct.cast(lp, self.ct.POINTER(self.wt.RECT)).contents
                    f = self._frame_size()
                    r.left += f; r.top += f; r.right -= f; r.bottom -= f
                return 0
            if msg == 0x0084:                                          # WM_NCHITTEST
                return self._hit(hwnd, lp)
            if msg == 0x0112 and (wp & 0xFFF0) == 0xF100 and lp == 0:  # WM_SYSCOMMAND SC_KEYMENU from Alt alone:
                return 0         # the (hidden) system menu would take the keyboard and the app would seem frozen
                                 # (Alt+Space, lp == ' ', still opens it)
            if msg in (0x00A0, 0x02A2):                                # WM_NCMOUSEMOVE / WM_NCMOUSELEAVE
                self.nc_hover = msg == 0x00A0 and wp == self.HTMAXBUTTON
            if wp == self.HTMAXBUTTON and msg in (0x00A1, 0x00A3):     # WM_NCLBUTTONDOWN / DBLCLK on our button
                return 0
            if wp == self.HTMAXBUTTON and msg == 0x00A2:               # WM_NCLBUTTONUP: maximise / restore
                self.u.ShowWindow(hwnd, 9 if self.u.IsZoomed(hwnd) else 3)
                return 0
        except Exception:
            pass
        return self.u.CallWindowProcW(self._old, hwnd, msg, wp, lp)

    def _hit(self, hwnd, lp):
        ct, wt, u = self.ct, self.wt, self.u
        x, y = ct.c_short(lp & 0xFFFF).value, ct.c_short((lp >> 16) & 0xFFFF).value
        wr = wt.RECT(); u.GetWindowRect(hwnd, ct.byref(wr))
        zoomed = u.IsZoomed(hwnd)
        if not zoomed:                                                 # resize from the edges (and corners)
            b = max(4, self.c.px(6))
            col = 0 if x < wr.left + b else 2 if x >= wr.right - b else 1
            row = 0 if y < wr.top + b else 2 if y >= wr.bottom - b else 1
            if (col, row) != (1, 1):
                return self.EDGES[(col, row)]
        pt = wt.POINT(x, y); u.ScreenToClient(hwnd, ct.byref(pt))
        cr = wt.RECT(); u.GetClientRect(hwnd, ct.byref(cr))
        c = self.c
        if c.full or pt.y < 0 or pt.y >= c.h:
            return self.HTCLIENT
        right = cr.right                                              # caption buttons from the right: ... min, max, close
        for i, name in enumerate(reversed(c.buttons)):
            if right - (i + 1) * c.bw <= pt.x < right - i * c.bw:
                return self.HTMAXBUTTON if name == "max" else self.HTCLIENT   # min / close stay Tk buttons
        return self.HTCAPTION


def attach(win, P, px, title="", main=False, resizable=True, **kw):
    """Give `win` the custom frame, or return None (and keep the standard frame) if that is not possible here."""
    try:
        return Chrome(win, P, px, title, main=main, resizable=resizable, **kw)
    except Exception:
        try:
            win.overrideredirect(False)
        except Exception:
            pass
        return None


def set_title(win, text):
    """Window title (taskbar, Alt-Tab) and the text on our own strip."""
    win.title(text)
    c = getattr(win, "_chrome", None)
    if c:
        c.set_title(text)
