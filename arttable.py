"""A table drawn on a Canvas, so the aurora artwork shows through it (ttk.Treeview paints every row solid and Tk
widgets cannot be transparent).

It implements the part of the ttk.Treeview API the app uses - insert / item / delete / detach / move / index /
exists / get_children / selection / selection_set / identify_row / bbox / heading / column / tag_configure / yview,
plus the yscrollcommand and height (rows) options - and fires <<TreeviewSelect>> like a Treeview does. Its data
lives in the same attributes as test_gui's fake Treeview (rows, order, sel, headings, columns, tags).

Only the visible rows are drawn; every change schedules one redraw on idle."""
import itertools
import tkinter as tk

ELLIPSIS = "\u2026"
_ids = itertools.count(1)


class ArtTable(tk.Canvas):
    def __init__(self, master, columns=(), show="headings", selectmode="extended", height=0, rowheight=40,
                 head_height=32, inset=0, palette=None, font=None, head_font=None, draw_bg=None, painters=None,
                 head_line=True, **kw):
        super().__init__(master, bd=0, highlightthickness=0, takefocus=1, **kw)
        self.rows, self.order, self.sel = {}, [], ()
        self.headings, self.columns, self.tags = {}, {}, {}
        self.cols = list(columns)
        self.show_tree = "tree" in str(show)
        self.selectmode = selectmode
        self.rowheight, self.head_height, self.inset = int(rowheight), int(head_height), int(inset)
        self.head_line = head_line       # a thin line under the heading row
        self._P, self._font, self._head_font, self._draw_bg = palette, font, head_font, draw_bg
        self._height = int(height)
        self._top = 0                  # index (in self.order) of the first visible row
        self._ysc = None
        self._anchor = None            # shift-click / arrow-key anchor
        self._job = None
        self._hover_head = None
        self._hover_row = None
        self.painters = dict(painters or {})   # column id -> fn(canvas, iid, x, y, w, h, selected) draws that cell
        self._layout = []              # [(column id, x, width)] of the last draw
        self._elide_cache = {}
        if self._height:
            super().configure(height=self.head_height + self._height * self.rowheight)
        tag = "ArtTable%d" % next(_ids)
        try:
            self.bindtags((str(self), tag) + tuple(t for t in self.bindtags() if t != str(self)))
        except (TypeError, tk.TclError):
            pass
        for seq, fn in (("<Configure>", lambda e: self.redraw()), ("<Button-1>", self._click),
                        ("<Shift-Button-1>", lambda e: self._click(e, shift=True)),
                        ("<Control-Button-1>", lambda e: self._click(e, ctrl=True)),
                        ("<B1-Motion>", self._drag_select), ("<ButtonRelease-1>", lambda e: self._end_drag()),
                        ("<MouseWheel>", self._wheel), ("<Button-4>", lambda e: self.yview("scroll", -3, "units")),
                        ("<Button-5>", lambda e: self.yview("scroll", 3, "units")),
                        ("<Motion>", self._motion), ("<Leave>", lambda e: self._set_hover(None)),
                        ("<Up>", lambda e: self._key(-1)), ("<Down>", lambda e: self._key(1)),
                        ("<Prior>", lambda e: self._key(-self._visible())), ("<Next>", lambda e: self._key(self._visible())),
                        ("<Home>", lambda e: self._key(-10 ** 9)), ("<End>", lambda e: self._key(10 ** 9))):
            self.bind_class(tag, seq, fn)

    # ---- Treeview API ------------------------------------------------------------------------------------
    def heading(self, col, option=None, **kw):
        h = self.headings.setdefault(col, {})
        if kw:
            h.update(kw)
            self.redraw()
        return h.get(option, "") if option else dict(h)

    def column(self, col, option=None, **kw):
        c = self.columns.setdefault(col, {})
        if kw:
            c.update(kw)
            self.redraw()
        if option:
            return c.get(option, {"width": 200, "minwidth": 20, "stretch": True, "anchor": "w"}.get(option, ""))
        return dict(c)

    def insert(self, parent, index, iid=None, values=(), tags=(), image="", text="", **kw):
        if iid is None:
            iid = "I%d" % next(_ids)
        self.rows[iid] = {"values": tuple(values), "tags": self._tags(tags), "image": image, "text": text}
        if iid in self.order:
            self.order.remove(iid)
        self.order.insert(len(self.order) if index == "end" else int(index), iid)
        self.redraw()
        return iid

    def item(self, iid, option=None, **kw):
        row = self.rows[iid]
        if kw:
            for k, v in kw.items():
                row[k] = tuple(v) if k == "values" else self._tags(v) if k == "tags" else v
            self.redraw()
        return row.get(option, "") if option else row

    def exists(self, iid):
        return iid in self.rows

    def get_children(self, item=""):
        return tuple(self.order)

    def delete(self, *ids):
        for i in self._flat(ids):
            self.rows.pop(i, None)
            if i in self.order:
                self.order.remove(i)
        self._drop_from_selection()
        self.redraw()

    def detach(self, *ids):
        for i in self._flat(ids):
            if i in self.order:
                self.order.remove(i)
        self._drop_from_selection()
        self.redraw()

    def move(self, iid, parent, index):
        if iid in self.order:
            self.order.remove(iid)
        self.order.insert(len(self.order) if index == "end" else int(index), iid)
        self.redraw()

    def index(self, iid):
        return self.order.index(iid)

    def selection(self):
        return tuple(self.sel)

    def selection_set(self, *ids):
        new = tuple(i for i in self._flat(ids) if i in self.rows)
        if self.selectmode == "browse":
            new = new[-1:]
        self.sel = new
        if new and self._anchor not in new:
            self._anchor = new[0]
        self.redraw()
        try:
            self.event_generate("<<TreeviewSelect>>", when="tail")
        except tk.TclError:
            pass

    def see(self, iid):
        if iid in self.order:
            i, vis = self.order.index(iid), max(1, self._visible())
            if i < self._top:
                self._top = i
            elif i >= self._top + vis:
                self._top = i - vis + 1
            self.redraw()

    def tag_configure(self, tag, **kw):
        self.tags.setdefault(tag, {}).update(kw)
        self.redraw()
        return dict(self.tags[tag])

    def identify_row(self, y):
        if y < self.head_height:
            return ""
        i = self._top + int((y - self.head_height) // self.rowheight)
        return self.order[i] if 0 <= i < len(self.order) else ""

    def bbox(self, iid, column=None):
        if iid not in self.order:
            return ""
        i = self.order.index(iid) - self._top
        if i < 0 or i >= self._visible():
            return ""
        y = self.head_height + i * self.rowheight
        if column is None:
            return (self.inset, y, max(self.winfo_width() - 2 * self.inset, 0), self.rowheight)
        for col, x, w in self._columns_layout():
            if col == column:
                return (x, y, w, self.rowheight)
        return ""

    def yview(self, *args):
        n, vis = len(self.order), self._visible()
        if args:
            if args[0] == "moveto":
                self._top = int(round(float(args[1]) * n))
            elif args[0] == "scroll":
                step = int(args[1]) * (max(1, vis - 1) if str(args[2]).startswith("page") else 1)
                self._top += step
            self._clamp()
            self.redraw()
            return None
        if not n:
            return (0.0, 1.0)
        return (self._top / n, min(1.0, (self._top + vis) / n))

    def configure(self, cnf=None, **kw):
        kw = dict(cnf or {}, **kw)
        if "yscrollcommand" in kw:
            self._ysc = kw.pop("yscrollcommand")
        if "height" in kw:                       # like a Treeview: height counts rows
            self._height = int(kw.pop("height"))
            kw["height"] = self.head_height + self._height * self.rowheight
        if kw:
            return super().configure(**kw)
        self.redraw()
    config = configure

    def cget(self, key):
        if key == "height":
            return self._height
        if key == "yscrollcommand":
            return self._ysc
        return super().cget(key)

    # ---- drawing -----------------------------------------------------------------------------------------
    def redraw(self):
        if not self._job:
            try:
                self._job = self.after_idle(self._draw)
            except tk.TclError:
                pass

    def _visible(self):
        try:
            h = int(self.winfo_height())
        except (tk.TclError, TypeError, ValueError):
            return 0
        return max(0, -(-(h - self.head_height) // self.rowheight))     # a partly shown last row counts

    def _clamp(self):
        full = max(0, int((self.winfo_height() - self.head_height) // self.rowheight))
        self._top = max(0, min(self._top, len(self.order) - max(full, 1)))

    def _columns_layout(self):
        ids = (["#0"] if self.show_tree else []) + self.cols
        spec = [(c, self.columns.get(c, {})) for c in ids]
        widths = [int(s.get("width", 200)) for _, s in spec]
        room = max(0, self.winfo_width() - 2 * self.inset)
        extra = room - sum(widths)
        flex = [i for i, (_, s) in enumerate(spec) if s.get("stretch", True)]
        if flex and extra > 0:
            total = sum(widths[i] for i in flex) or 1
            for i in flex:
                widths[i] += extra * widths[i] // total
        elif flex and extra < 0:
            for i in flex:
                give = min(-extra, widths[i] - int(spec[i][1].get("minwidth", 20)))
                if give > 0:
                    widths[i] -= give
                    extra += give
        out, x = [], self.inset
        for (c, _), w in zip(spec, widths):
            out.append((c, x, w))
            x += w
        return out

    def _elide(self, text, font, room):
        key = (text, room, id(font))
        hit = self._elide_cache.get(key)
        if hit is not None:
            return hit
        out = text
        if font.measure(text) > room:
            while out and font.measure(out + ELLIPSIS) > room:
                out = out[:-1]
            out = out + ELLIPSIS if out else ""
        if len(self._elide_cache) > 4000:
            self._elide_cache.clear()
        self._elide_cache[key] = out
        return out

    def _draw(self):
        self._job = None
        try:
            self._paint()
        except tk.TclError:          # destroyed while a redraw was pending
            pass

    def _paint(self):
        P = self._P()
        super().configure(bg=P["bg"])
        tk.Canvas.delete(self, "all")    # (self.delete is the Treeview-style row delete; same for move/index/bbox/insert)
        w, h = self.winfo_width(), self.winfo_height()
        if self._draw_bg:
            self._draw_bg(self)
        self._clamp()
        self._layout = layout = self._columns_layout()
        pad, hh, rh = 8, self.head_height, self.rowheight
        hf, f = self._head_font, self._font
        for col, x, cw in layout:                                     # headings: text over the artwork
            hd = self.headings.get(col, {})
            text = str(hd.get("text", ""))
            if text:
                hot = col == self._hover_head and hd.get("command")
                self.create_text(x + pad, hh // 2, anchor="w", font=hf, fill=P["fg"] if hot else P["muted"],
                                 text=self._elide(text, hf, cw - 2 * pad))
        if self.head_line:
            self.create_line(self.inset, hh - 1, w - self.inset, hh - 1, fill=P["border"])
        x0, x1 = self.inset + 1, w - self.inset - 2
        for n, iid in enumerate(self.order[self._top:self._top + self._visible()]):
            row, y = self.rows[iid], hh + n * rh
            on = iid in self.sel
            # no boxes: a selected or pointed-at row is bold white text over the artwork
            lit = on or iid == self._hover_row
            rf = self._bold_font() if lit else f
            fg = P["fg"] if lit else next((self.tags[t]["foreground"] for t in row["tags"]
                                           if self.tags.get(t, {}).get("foreground")), P["fg"])
            vals = row["values"]
            vi = 0
            for col, x, cw in layout:
                if col == "#0":
                    if row.get("image"):
                        self.create_image(x + cw // 2, y + rh // 2, image=row["image"])
                    continue
                v = vals[vi] if vi < len(vals) else ""
                vi += 1
                if col in self.painters:
                    self.painters[col](self, iid, x, y, cw, rh, on)
                    continue
                text = self._elide(str(v), rf, cw - 2 * pad)
                if not text:
                    continue
                anchor = self.columns.get(col, {}).get("anchor", "w")
                tx = x + pad if anchor == "w" else x + cw - pad if anchor == "e" else x + cw // 2
                self.create_text(tx, y + rh // 2, anchor=anchor if anchor in ("w", "e") else "center",
                                 font=rf, fill=fg, text=text)
        if self._ysc:
            first, last = self.yview()
            self._ysc(str(first), str(last))

    def _bold_font(self):
        b = getattr(self, "_bold", None)
        if b is None:
            try:
                from tkinter import font as tkfont
                spec = tkfont.Font(font=self._font).actual()
                spec["weight"] = "bold"
                b = tkfont.Font(**spec)
            except Exception:
                b = self._font
            self._bold = b
        return b

    # ---- mouse / keys ------------------------------------------------------------------------------------
    def _click(self, e, shift=False, ctrl=False):
        self.focus_set()
        if e.y < self.head_height:
            col = self._column_at(e.x)
            cmd = self.headings.get(col, {}).get("command") if col else None
            if cmd:
                cmd()
            return
        iid = self.identify_row(e.y)
        if not iid:
            return
        if self.selectmode == "extended" and ctrl:
            cur = list(self.sel)
            cur.remove(iid) if iid in cur else cur.append(iid)
            self._anchor = iid
            self.selection_set(tuple(cur))
        elif self.selectmode == "extended" and shift and self._anchor in self.order:
            a, b = sorted((self.order.index(self._anchor), self.order.index(iid)))
            self.selection_set(tuple(self.order[a:b + 1]))
        else:
            self._anchor = iid
            self.selection_set(iid)
        self._dragging = self.selectmode == "extended" and not (shift or ctrl)

    def _drag_select(self, e):
        """Press on a row and drag over others: they are all selected (mouse-only multiple selection). Past the
        top or bottom edge the list scrolls along."""
        if not getattr(self, "_dragging", False) or self._anchor not in self.order:
            return
        h = self.winfo_height()
        if e.y > h - self.rowheight // 3:
            self.yview("scroll", 1, "units")
        elif e.y < self.head_height + self.rowheight // 3:
            self.yview("scroll", -1, "units")
        y = min(max(e.y, self.head_height + 1), h - 2)
        iid = self.identify_row(y)
        if not iid or iid not in self.order:
            return
        a, b = sorted((self.order.index(self._anchor), self.order.index(iid)))
        rng = tuple(self.order[a:b + 1])
        if tuple(self.sel) != rng:
            self.selection_set(rng)

    def _end_drag(self):
        self._dragging = False

    def _key(self, step):
        if not self.order:
            return "break"
        cur = self.sel[-1] if self.sel and self.sel[-1] in self.order else None
        i = 0 if cur is None else max(0, min(len(self.order) - 1, self.order.index(cur) + step))
        self._anchor = self.order[i]
        self.selection_set(self.order[i])
        self.see(self.order[i])
        return "break"

    def _wheel(self, e):
        self.yview("scroll", -3 if e.delta > 0 else 3, "units")

    def _column_at(self, x):
        for col, cx, cw in self._layout:
            if cx <= x < cx + cw:
                return col
        return None

    def _motion(self, e):
        self._set_hover(self._column_at(e.x) if e.y < self.head_height else None, self.identify_row(e.y) or None)

    def _set_hover(self, col, row=None):
        if (col, row) != (self._hover_head, self._hover_row):
            self._hover_head, self._hover_row = col, row
            self.redraw()

    # ---- helpers -----------------------------------------------------------------------------------------
    @staticmethod
    def _flat(ids):
        out = []
        for i in ids:
            if isinstance(i, (list, tuple)):
                out.extend(i)
            elif isinstance(i, str) and " " in i.strip():
                out.extend(i.split())
            elif i not in (None, ""):
                out.append(i)
        return out

    @staticmethod
    def _tags(t):
        if isinstance(t, str):
            return tuple(t.split())
        return tuple(t or ())

    def _drop_from_selection(self):
        keep = tuple(i for i in self.sel if i in self.order)
        if keep != self.sel:
            self.sel = keep
            try:
                self.event_generate("<<TreeviewSelect>>", when="tail")
            except tk.TclError:
                pass


def blend(c1, c2, t):
    """Colour `t` of the way from c1 to c2 ("#rrggbb")."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def round_rect(c, x0, y0, x1, y1, r, **kw):
    """A rounded rectangle on canvas `c` (a smoothed polygon; Tk has no native one)."""
    r = max(1, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
    pts = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
    return c.create_polygon(pts, smooth=True, **kw)
