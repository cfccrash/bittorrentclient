"""Look and feel for Bit Torrent Client: Windows 11 style light/dark palettes, fonts, ttk styles, flat toolbar
buttons and tooltips. Pure tkinter, no third-party packages."""
import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# "Glass" palettes: cool, frosted tints that sit naturally beside the Windows 11 Mica title bar
LIGHT = dict(
    bg="#E6ECF5", surface="#F8FAFD", surface2="#EDF1F8", border="#CBD5E3",
    fg="#14181F", muted="#586274", disabled="#9AA6B8",
    accent="#0A64C8", accent_hover="#0852A6", accent_fg="#FFFFFF",
    hover="#DCE6F4", press="#CBD9EE", sel="#CFE1F8", sel_fg="#14181F",
    trough="#D5DEEB", scroll="#B4C0D2", scroll_hover="#93A2BA",
    good="#0E7A2E", warn="#9A5B00", bad="#C42B1C",
    bar_down="#0A64C8", bar_seed="#13A04A", bar_check="#E3A008", bar_idle="#98A5B8", bar_err="#C42B1C",
    tip_bg="#1F2631", tip_fg="#FFFFFF",
)
# "Aurora": near-black navy with a blue / violet glow (the app's artwork lives in assets_ui.py)
DARK = dict(
    bg="#04060E", surface="#080A19", surface2="#060813", border="#16203C",
    fg="#E4ECFF", muted="#7C8AB2", disabled="#434C6B",
    accent="#3D8BFF", accent_hover="#2E74E6", accent_fg="#FFFFFF",
    hover="#0F1830", press="#162448", sel="#112B58", sel_fg="#FFFFFF",
    trough="#0C1530", scroll="#222F55", scroll_hover="#394C80",
    good="#5FD39A", warn="#F5C451", bad="#FF7B86", glow="#7A5CFF",
    bar_down="#3D8BFF", bar_seed="#46C98B", bar_check="#E3A008", bar_idle="#56648D", bar_err="#E5534B",
    tip_bg="#0F1830", tip_fg="#FFFFFF",
)
LIGHT["glow"] = "#6A4CE0"

# Segoe Fluent Icons / Segoe MDL2 Assets code points (Windows 11 / 10)
ICON_ADD, ICON_LINK, ICON_PLAY, ICON_PAUSE = "\uE710", "\uE71B", "\uE768", "\uE769"
ICON_DELETE, ICON_FOLDER, ICON_SETTINGS = "\uE74D", "\uE838", "\uE713"
ICON_FILES = "\uE8FD"
ICON_SEARCH = "\uE721"
ICON_RESTART, ICON_EXIT = "\uE72C", "\uE7E8"
ICON_CREATE = "\uE70F"
ICON_RSS = "\uE895"   # Sync
ICON_LIBRARY = "\uE714"   # Video


def system_is_dark():
    """True when Windows is set to dark app mode."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def palette(mode):
    """mode: 'system' | 'light' | 'dark'."""
    dark = system_is_dark() if mode == "system" else mode == "dark"
    return DARK if dark else LIGHT


def _rgb(hexcol):
    h = hexcol.lstrip("#")
    return int(h[0:2], 16) | (int(h[2:4], 16) << 8) | (int(h[4:6], 16) << 16)   # COLORREF 0x00BBGGRR


def set_glass(win, P):
    """Windows 11 glass window frame: Mica backdrop, caption/border tinted to the palette, matching dark mode.
    Every call is best-effort; older Windows versions and other systems silently keep the plain frame."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        dwm = ctypes.windll.dwmapi

        def attr(code, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, code, ctypes.byref(v), ctypes.sizeof(v))
        attr(20, 1 if P is DARK else 0)       # DWMWA_USE_IMMERSIVE_DARK_MODE
        attr(38, 2)                           # DWMWA_SYSTEMBACKDROP_TYPE = Mica
        attr(35, _rgb(P["bg"]))               # DWMWA_CAPTION_COLOR: title bar blends into the window
        attr(36, _rgb(P["fg"]))               # DWMWA_TEXT_COLOR
        attr(34, _rgb(P["border"]))           # DWMWA_BORDER_COLOR: hairline frame
        attr(33, 2)                           # DWMWA_WINDOW_CORNER_PREFERENCE = rounded
    except Exception:
        pass


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def fmt_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024


def fmt_rate(r):
    return fmt_size(r) + "/s" if r >= 1 else "-"


def fmt_eta(s):
    if s is None:
        return "-"
    s = int(s)
    if s > 86400 * 30:
        return "-"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return ("%dd %dh" % (h // 24, h % 24)) if h >= 24 else (
        "%dh %02dm" % (h, m) if h else "%dm %02ds" % (m, sec))


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------
class Fonts:
    def __init__(self, root):
        fams = set(tkfont.families(root))
        base = "Segoe UI" if "Segoe UI" in fams else tkfont.nametofont("TkDefaultFont").actual("family")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=base, size=10)
            except tk.TclError:
                pass
        self.ui = tkfont.Font(root, family=base, size=10)
        self.small = tkfont.Font(root, family=base, size=9)
        self.bold = tkfont.Font(root, family=base, size=10, weight="bold")
        self.title = tkfont.Font(root, family=base, size=15, weight="bold")
        self.big = tkfont.Font(root, family=base, size=12)
        icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
        mono = next((f for f in ("Consolas", "Cascadia Mono", "Courier New") if f in fams),
                    tkfont.nametofont("TkFixedFont").actual("family"))
        self.mono = tkfont.Font(root, family=mono, size=9)
        self.has_icons = icon_family is not None
        self.icon = tkfont.Font(root, family=icon_family or base, size=11)


# --------------------------------------------------------------------------
# ttk styles
# --------------------------------------------------------------------------
BAR_KINDS = {"Down": "bar_down", "Seed": "bar_seed", "Check": "bar_check", "Idle": "bar_idle", "Err": "bar_err"}


def _png(w, h, rgba_rows):
    """Encode RGBA rows (lists of (r, g, b, a)) as a PNG - stdlib only, so the round tick boxes keep real alpha."""
    import struct, zlib
    raw = b"".join(b"\0" + bytes(v for px_ in row for v in px_) for row in rgba_rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def circle_png(size, ring=None, fill=None, dot=None, gap=0, stroke=None):
    """A round tick box: `ring` outline colour, `fill` a lit disc (lighter at the centre), `dot` a smaller disc
    (part-ticked). Anti-aliased by 4x4 supersampling; `gap` adds transparent pixels on the right (space before the text)."""
    import base64
    w, n = size + gap, 4
    r_out = size / 2.0 - 0.5
    sw = stroke or max(1.5, size / 11.0)
    cx = cy = size / 2.0
    ring_c, fill_c, dot_c = (_rgb(c) if c else None for c in (ring, fill, dot))
    rows = []
    for y in range(size):
        row = []
        for x in range(w):
            cov_ring = cov_fill = cov_dot = 0
            for sy in range(n):
                for sx in range(n):
                    dx = x + (sx + 0.5) / n - cx
                    dy = y + (sy + 0.5) / n - cy
                    d = (dx * dx + dy * dy) ** 0.5
                    if d <= r_out:
                        if d >= r_out - sw:
                            cov_ring += 1
                        if fill_c and d < r_out - sw + 0.5:
                            cov_fill += 1
                        if dot_c and d <= r_out * 0.42:
                            cov_dot += 1
            tot = float(n * n)
            px_ = (0, 0, 0, 0)
            if fill_c and (cov_fill or cov_ring):
                d = (((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5) / max(r_out, 1)
                glow = max(0.0, 1.0 - d) * 0.45          # lit: lighter towards the middle
                col = tuple(int(c + (255 - c) * glow) for c in fill_c)
                a = (cov_fill + cov_ring) / tot
                if cov_ring and ring_c:
                    t = cov_ring / max(cov_fill + cov_ring, 1)
                    col = tuple(int(a_ * (1 - t) + b_ * t) for a_, b_ in zip(col, ring_c))
                px_ = col + (int(255 * min(1.0, a)),)
            elif cov_dot:
                px_ = dot_c + (int(255 * cov_dot / tot),)
            elif cov_ring and ring_c:
                px_ = ring_c + (int(255 * cov_ring / tot),)
            row.append(px_)
        rows.append(row)
    return base64.b64encode(_png(w, size, rows))


_CHECKS = {}     # name -> PhotoImage: round tick boxes, repainted in place when the theme changes


def check_images(root=None, P=None, size=None, gap=0):
    """The round tick-box images ('off', 'on', 'part', 'hover', 'dis', 'dis_on'); green when ticked.
    Called with a palette to (re)paint them; without, returns the current set."""
    if P is None:
        return _CHECKS
    on = P.get("bar_seed", P["accent"])
    spec = dict(off=dict(ring=P["muted"]), hover=dict(ring=P["fg"]), on=dict(ring=on, fill=on),
                part=dict(ring=on, dot=on), dis=dict(ring=P["disabled"]), dis_on=dict(ring=P["disabled"], fill=P["disabled"]))
    for name, kw in spec.items():
        for g, key in ((gap, name), (0, name + "_tight")):
            data = circle_png(size, gap=g, **kw)
            img = _CHECKS.get(key)
            try:
                if img is None or str(img) not in root.image_names():
                    _CHECKS[key] = tk.PhotoImage(master=root, data=data, format="png")
                else:
                    img.configure(data=data, format="png")
            except (tk.TclError, TypeError, AttributeError):
                _CHECKS[key] = tk.PhotoImage(master=root, data=data)
    return _CHECKS


_BOLD = [None]


def _bold_selection(e):
    """Treeview selection shown as bold text (the selected-row colour is the normal row colour)."""
    t = e.widget
    try:
        t.tag_configure("_bold", font=_BOLD[0])
        for iid in t.tag_has("_bold"):
            t.item(iid, tags=tuple(x for x in t.item(iid, "tags") if x != "_bold"))
        for iid in t.selection():
            t.item(iid, tags=tuple(t.item(iid, "tags")) + ("_bold",))
    except tk.TclError:
        pass


def style_ttk(root, P, F, px):
    """(Re)configure every ttk style for palette P. `px` scales pixel sizes for the display's DPI."""
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=P["bg"], foreground=P["fg"], fieldbackground=P["surface"],
                 bordercolor=P["border"], lightcolor=P["surface"], darkcolor=P["surface"],
                 troughcolor=P["trough"], focuscolor=P["bg"], font=F.ui)
    # clam lights up any widget in the 'active' state; frames must never change colour (see-through dialogs)
    st.map(".", background=[("disabled", P["bg"]), ("active", P["bg"])])
    st.configure("TFrame", background=P["bg"])
    st.configure("Card.TFrame", background=P["surface"])
    st.configure("TLabel", background=P["bg"], foreground=P["fg"])
    st.configure("Muted.TLabel", background=P["bg"], foreground=P["muted"])
    st.configure("Title.TLabel", background=P["bg"], foreground=P["fg"], font=F.title)
    st.configure("Bad.TLabel", background=P["bg"], foreground=P["bad"])

    st.configure("TButton", padding=(px(16), px(7)), relief="flat", borderwidth=1, background=P["surface"],
                 foreground=P["fg"], bordercolor=P["border"], lightcolor=P["surface"], darkcolor=P["surface"])
    # No boxes anywhere: buttons are plain text on the background, bold when pointed at (like the left column)
    flat = dict(background=P["bg"], foreground=P["fg"], bordercolor=P["bg"], lightcolor=P["bg"], darkcolor=P["bg"],
                focuscolor=P["bg"], borderwidth=0, relief="flat")
    st.configure("TButton", **flat)
    st.map("TButton", background=[("pressed", P["bg"]), ("active", P["bg"])], bordercolor=[("focus", P["bg"])],
           font=[("pressed", F.bold), ("active", F.bold)], foreground=[("disabled", P["disabled"])])
    st.configure("Accent.TButton", **flat)
    st.map("Accent.TButton", background=[("pressed", P["bg"]), ("active", P["bg"])], bordercolor=[("focus", P["bg"])],
           font=[("pressed", F.bold), ("active", F.bold)], foreground=[("disabled", P["disabled"])])

    for w in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(w, fieldbackground=P["surface"], foreground=P["fg"], insertcolor=P["fg"],
                     bordercolor=P["border"], lightcolor=P["surface"], darkcolor=P["surface"],
                     padding=px(6), arrowcolor=P["muted"], background=P["surface"])
        st.map(w, bordercolor=[("focus", P["muted"])], arrowcolor=[("active", P["fg"])])
    st.map("TCombobox", fieldbackground=[("readonly", P["surface"])], foreground=[("readonly", P["fg"])],
           selectbackground=[("readonly", P["surface"])], selectforeground=[("readonly", P["fg"])])
    root.option_add("*TCombobox*Listbox.background", P["surface"])
    root.option_add("*TCombobox*Listbox.foreground", P["fg"])
    root.option_add("*TCombobox*Listbox.foreground", P["muted"])        # the chosen one is white, no blue bar
    root.option_add("*TCombobox*Listbox.selectBackground", P["surface"])
    root.option_add("*TCombobox*Listbox.selectForeground", P["fg"])

    st.configure("TCheckbutton", background=P["bg"], foreground=P["fg"])
    # round tick boxes that light up green when ticked (instead of clam's small square)
    im = check_images(root, P, size=px(18), gap=px(8))
    try:
        st.element_create("Round.indicator", "image", im["off"],
                          ("disabled", "selected", im["dis_on"]), ("disabled", im["dis"]),
                          ("selected", im["on"]), ("alternate", im["part"]), ("active", im["hover"]), sticky="")
    except tk.TclError:
        pass                                  # already made: the images it uses were repainted in place above
    st.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Round.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})])
    st.map("TCheckbutton", background=[("active", P["bg"])], foreground=[("disabled", P["disabled"])],
           font=[("active", F.bold)])
    st.configure("TLabelframe", background=P["bg"], bordercolor=P["bg"], lightcolor=P["bg"], darkcolor=P["bg"],
                 borderwidth=0, relief="flat")                               # no box lines around the groups
    st.configure("TLabelframe.Label", background=P["bg"], foreground=P["muted"], font=F.bold)

    st.configure("TNotebook", background=P["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    # main window details pane: pages only, the tab row is drawn by the App over the artwork
    st.configure("Bare.TNotebook", background=P["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0),
                 bordercolor=P["bg"], lightcolor=P["bg"], darkcolor=P["bg"])   # no frame line through the artwork
    st.layout("Bare.TNotebook", [])          # no client element: clam would inset the page 2 px (a seam in the art)
    st.layout("Bare.TNotebook.Tab", [])
    st.configure("TNotebook.Tab", padding=(px(18), px(8)), background=P["bg"], foreground=P["muted"], borderwidth=0)
    tabpad = (px(18), px(8))
    st.configure("TNotebook.Tab", lightcolor=P["bg"], bordercolor=P["bg"], focuscolor=P["bg"])
    # clam grows/shifts the selected tab through its own style map; pin every state to the same padding
    st.map("TNotebook.Tab", background=[("selected", P["bg"]), ("active", P["bg"])],
           foreground=[("selected", P["fg"]), ("active", P["fg"])],
           font=[("selected", F.bold), ("active", F.bold)],
           padding=[("selected", tabpad), ("!selected", tabpad)],
           expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
           lightcolor=[("selected", P["bg"])], bordercolor=[("selected", P["bg"])],
           focuscolor=[("selected", P["bg"]), ("!selected", P["bg"])])

    # table
    st.configure("Treeview", background=P["surface"], fieldbackground=P["surface"], foreground=P["fg"],
                 rowheight=px(40), borderwidth=0, relief="flat", font=F.ui)
    # selected rows: bold white text, no bar (the bold comes from the "_bold" tag, see _bold_selection)
    st.map("Treeview", background=[("selected", P["surface"])], foreground=[("selected", P["fg"])])
    _BOLD[0] = F.bold
    if not getattr(root, "_bold_sel_bound", False):
        root.bind_class("Treeview", "<<TreeviewSelect>>", _bold_selection, add="+")
        root._bold_sel_bound = True
    st.configure("Treeview.Heading", background=P["surface2"], foreground=P["muted"], relief="flat",
                 borderwidth=0, padding=(px(4), px(9)), font=F.small)   # text lines up with the cells below
    st.map("Treeview.Heading", background=[("active", P["surface2"])], foreground=[("active", P["fg"])])
    st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])   # no border around the table
    st.configure("Dialog.Treeview", rowheight=px(30))
    st.configure("Compact.Treeview", rowheight=px(24))

    # slim modern scrollbar: no arrow buttons, just a rounded-looking thumb on a quiet trough
    st.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    st.configure("Vertical.TScrollbar", background=P["scroll"], troughcolor=P["surface"], bordercolor=P["surface"],
                 arrowsize=px(11), relief="flat", gripcount=0, lightcolor=P["scroll"], darkcolor=P["scroll"], width=px(11))
    st.map("Vertical.TScrollbar", background=[("active", P["scroll_hover"]), ("pressed", P["scroll_hover"])])

    for kind, key in BAR_KINDS.items():  # one slim, flat progress bar style per torrent state
        st.configure(kind + ".Horizontal.TProgressbar", background=P[key], troughcolor=P["trough"],
                     bordercolor=P["trough"], lightcolor=P[key], darkcolor=P[key], thickness=px(8))
    return st


# --------------------------------------------------------------------------
# widgets
# --------------------------------------------------------------------------
class ToolButton(tk.Frame):
    """Flat toolbar button: icon + label, hover/pressed/disabled states, optional accent colour."""

    def __init__(self, master, fonts, icon, text, command, accent=False, pady=8, raised=False):
        super().__init__(master, cursor="hand2", bd=0, highlightthickness=0)
        self.command, self.accent, self.raised = command, accent, raised   # raised: a visible button face at rest
        self.enabled, self._hover, self._down, self.P = True, False, False, None
        self.fonts = fonts
        self.icon = tk.Label(self, text=icon, font=fonts.icon, bd=0) if (icon and fonts.has_icons) else None
        self.label = tk.Label(self, text=text, font=fonts.ui, bd=0)
        if self.icon:
            self.icon.pack(side="left", padx=(12, 6), pady=pady)
        self.label.pack(side="left", padx=(0 if self.icon else 14, 14 if text or not self.icon else 6), pady=pady)
        for w in (self, self.label, self.icon):
            if w is not None:
                w.bind("<Enter>", self._enter)
                w.bind("<Leave>", self._leave)
                w.bind("<ButtonPress-1>", self._press)
                w.bind("<ButtonRelease-1>", self._release)

    def restyle(self, P):
        self.P = P
        self._paint()

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            self.configure(cursor="hand2" if on else "arrow")
            self._paint()

    def _paint(self):
        P = self.P
        if P is None:
            return
        bg = P["surface"]                         # no boxes: pointed at, the text turns bold instead
        fg = P["fg"] if self.enabled else P["disabled"]
        self.label.configure(font=self.fonts.bold if (self.enabled and (self._hover or self._down)) else self.fonts.ui)
        for w in (self, self.label, self.icon):
            if w is not None:
                w.configure(bg=bg)
        self.label.configure(fg=fg)
        if self.icon:
            self.icon.configure(fg=fg)

    def _enter(self, _e):
        self._hover = True
        self._paint()

    def _leave(self, _e):
        self._hover = self._down = False
        self._paint()

    def _press(self, _e):
        self._down = True
        self._paint()

    def _release(self, _e):
        fire = self._down and self._hover and self.enabled
        self._down = False
        self._paint()
        if fire:
            self.command()


class Tooltip:
    """Small hover tooltip. `text` may be a string or a callable returning one."""

    def __init__(self, widget, text, get_palette, above=False, delay=450, region=None):
        self.widget, self.text, self.get_palette, self.above, self.delay = widget, text, get_palette, above, delay
        self.region = region     # optional: () -> screen (x0, y0, x1, y1) of the part of the widget the tip is for
        self.tip, self._after, self._watch = None, None, None
        Tooltip.live.append(self)
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Unmap>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    live = []

    @classmethod
    def hide_all(cls):
        """Close every tooltip (window hidden, minimised, closed, or a dialog opened)."""
        for t in list(cls.live):
            try:
                t._hide()
            except Exception:
                cls.live.remove(t)

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        self._after = None
        text = self.text() if callable(self.text) else self.text
        if not text or self.tip or not self._inside():
            return
        try:
            if not self.widget.winfo_viewable() or self.widget.winfo_toplevel().state() in ("withdrawn", "iconic"):
                return
        except tk.TclError:
            return
        P = self.get_palette()
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tk.Label(tw, text=text, bg=P["tip_bg"], fg=P["tip_fg"], justify="left", wraplength=520,
                 padx=10, pady=6, bd=0).pack()
        tw.update_idletasks()
        x = self.widget.winfo_rootx() + 8
        r = self.region() if self.region else None
        if r:
            x, y = r[0] + 8, (r[1] - tw.winfo_reqheight() - 6) if self.above else r[3] + 6
        elif self.above:
            y = self.widget.winfo_rooty() - tw.winfo_reqheight() - 6
        else:
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tw.wm_geometry("+%d+%d" % (x, max(0, y)))
        self._watching()

    def _watching(self):
        """A tooltip must never outlive the pointer hovering its widget (Leave events can be lost)."""
        self._watch = None
        if not self.tip:
            return
        if not self._inside():
            self._hide()
            return
        try:
            self._watch = self.widget.after(150, self._watching)
        except tk.TclError:
            self._hide()

    def _inside(self):
        try:
            px_, py_ = self.widget.winfo_pointerxy()
            if self.region:
                r = self.region()
                return bool(r) and r[0] <= px_ < r[2] and r[1] <= py_ < r[3]
            x, y = self.widget.winfo_rootx(), self.widget.winfo_rooty()
            return x <= px_ < x + self.widget.winfo_width() and y <= py_ < y + self.widget.winfo_height()
        except tk.TclError:
            return False

    def _hide(self, e=None):
        if e is not None and getattr(e, "type", None) == tk.EventType.Leave and self._inside():
            return  # pointer only moved onto a child widget of the same button
        self._cancel()
        if self._watch:
            try:
                self.widget.after_cancel(self._watch)
            except tk.TclError:
                pass
            self._watch = None
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


def center_over(win, parent):
    """Centre a Toplevel over its parent window."""
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
    win.geometry("+%d+%d" % (max(0, x), max(0, y)))


def draw_icon(cv, name, cx, cy, size, col, bold=False, tags=()):
    """Toolbar / sidebar icons drawn with lines - where the Windows icon fonts are missing (Linux)."""
    h = size / 2.0
    w = max(1.5, size / 10.0) * (1.6 if bold else 1.0)
    kw = dict(fill=col, width=w, capstyle="round", joinstyle="round", tags=tags)
    L = lambda *p: cv.create_line(*p, **kw)
    O = lambda x0, y0, x1, y1: cv.create_oval(x0, y0, x1, y1, outline=col, width=w, tags=tags)
    R = lambda x0, y0, x1, y1: cv.create_rectangle(x0, y0, x1, y1, outline=col, width=w, tags=tags)
    A = lambda r, start, extent, dx=0, dy=0: cv.create_arc(cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r,
                                                           start=start, extent=extent, style="arc", outline=col,
                                                           width=w, tags=tags)
    if name == "power":
        A(h * .75, 120, 300)
        L(cx, cy - h * .95, cx, cy - h * .1)
    elif name == "refresh":
        A(h * .75, 60, 280)
        L(cx + h * .38, cy - h * 1.0, cx + h * .38, cy - h * .6, cx + h * .78, cy - h * .6)
    elif name == "gear":
        import math
        O(cx - h * .3, cy - h * .3, cx + h * .3, cy + h * .3)
        O(cx - h * .65, cy - h * .65, cx + h * .65, cy + h * .65)
        for i in range(8):
            a = i * math.pi / 4
            L(cx + math.cos(a) * h * .65, cy + math.sin(a) * h * .65, cx + math.cos(a) * h * .92, cy + math.sin(a) * h * .92)
    elif name == "play":
        cv.create_polygon(cx - h * .5, cy - h * .75, cx + h * .75, cy, cx - h * .5, cy + h * .75, outline=col, fill="",
                          width=w, joinstyle="round", tags=tags)
    elif name == "pause":
        R(cx - h * .55, cy - h * .7, cx - h * .2, cy + h * .7)
        R(cx + h * .2, cy - h * .7, cx + h * .55, cy + h * .7)
    elif name == "trash":
        L(cx - h * .8, cy - h * .55, cx + h * .8, cy - h * .55)
        L(cx - h * .25, cy - h * .55, cx - h * .25, cy - h * .85, cx + h * .25, cy - h * .85, cx + h * .25, cy - h * .55)
        L(cx - h * .6, cy - h * .55, cx - h * .5, cy + h * .85, cx + h * .5, cy + h * .85, cx + h * .6, cy - h * .55)
    elif name == "list":
        for dy in (-.55, 0, .55):
            L(cx - h * .8, cy + dy * h, cx - h * .65, cy + dy * h)
            L(cx - h * .35, cy + dy * h, cx + h * .85, cy + dy * h)
    elif name == "folder":
        L(cx - h * .85, cy + h * .7, cx - h * .85, cy - h * .65, cx - h * .3, cy - h * .65, cx - h * .15, cy - h * .45,
          cx + h * .85, cy - h * .45, cx + h * .85, cy + h * .7, cx - h * .85, cy + h * .7)
    elif name == "plus":
        L(cx, cy - h * .75, cx, cy + h * .75)
        L(cx - h * .75, cy, cx + h * .75, cy)
    elif name == "link":
        A(h * .38, 90, 180, dx=-h * .35)
        A(h * .38, -90, 180, dx=h * .35)
        L(cx - h * .35, cy - h * .38, cx + h * .1, cy - h * .38)
        L(cx - h * .1, cy + h * .38, cx + h * .35, cy + h * .38)
        L(cx - h * .3, cy, cx + h * .3, cy)
    elif name == "search":
        O(cx - h * .75, cy - h * .75, cx + h * .25, cy + h * .25)
        L(cx + h * .15, cy + h * .15, cx + h * .8, cy + h * .8)
    elif name == "pencil":
        cv.create_polygon(cx - h * .8, cy + h * .8, cx - h * .7, cy + h * .35, cx + h * .45, cy - h * .8,
                          cx + h * .8, cy - h * .45, cx - h * .35, cy + h * .7, outline=col, fill="", width=w,
                          joinstyle="round", tags=tags)
    elif name == "rss":
        cv.create_oval(cx - h * .8, cy + h * .45, cx - h * .45, cy + h * .8, fill=col, outline="", tags=tags)
        for r in (.75, 1.3):
            cv.create_arc(cx - h * .7 - h * r, cy + h * .7 - h * r, cx - h * .7 + h * r, cy + h * .7 + h * r,
                          start=0, extent=90, style="arc", outline=col, width=w, tags=tags)
    elif name == "video":
        R(cx - h * .85, cy - h * .55, cx + h * .3, cy + h * .55)
        cv.create_polygon(cx + h * .35, cy - h * .1, cx + h * .85, cy - h * .45, cx + h * .85, cy + h * .45,
                          cx + h * .35, cy + h * .1, outline=col, fill="", width=w, tags=tags)


def open_path(path):
    """Show a folder (or open a file) with the system: Explorer on Windows, the file manager on Linux."""
    if hasattr(os, "startfile"):
        os.startfile(path)
        return True
    if sys.platform.startswith("linux"):
        import linux
        return linux.open_path(path)
    import subprocess
    subprocess.Popen(["open", path])
    return True


def work_area(root):
    """(x, y, width, height) of the screen area not covered by the taskbar."""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):   # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 0, 0, int(root.winfo_screenwidth()), int(root.winfo_screenheight())


def autohide(sb, **pack):
    """Wrap a scrollbar's set() so the bar disappears while everything fits. Returns the function to use as
    the widget's yscrollcommand. `pack` are the options used to show it again."""
    state = {"shown": True}

    def setter(first, last):
        sb.set(first, last)
        need = float(first) > 0.0 or float(last) < 1.0
        if need and not state["shown"]:
            sb.pack(**pack)
            state["shown"] = True
        elif not need and state["shown"]:
            sb.pack_forget()
            state["shown"] = False
    return setter
