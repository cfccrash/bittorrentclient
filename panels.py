"""Pieces of the main window that are mostly logic: sidebar filters and the speed graph."""
import collections

from ui import fmt_rate

FILTERS = (("all", "All"), ("down", "Downloading"), ("seed", "Seeding"), ("queued", "Queued"),
           ("paused", "Paused"), ("error", "Errors"))
_DOWN = ("Downloading", "Checking", "Fetching metadata")
_SEED = ("Seeding", "Finished")


def matches_filter(key, s):
    """Does the snapshot row `s` belong under sidebar filter `key`?"""
    if key == "all":
        return True
    if key.startswith("cat:"):
        return s.get("category", "") == key[4:]
    if key == "error":
        return bool(s["error"])
    if s["error"]:
        return False
    state = s["raw_state"]
    return {"down": state in _DOWN, "seed": state in _SEED, "queued": state == "Queued",
            "paused": state == "Paused"}.get(key, False)


def count_filters(snap):
    return {key: sum(1 for s in snap if matches_filter(key, s)) for key, _ in FILTERS}


def matches_search(query, s):
    q = query.strip().casefold()
    return not q or q in s["name"].casefold()


class SpeedGraph:
    """Rolling download/upload history (one sample per refresh) drawn on a canvas."""
    LENGTH = 300            # samples kept (about five minutes)

    def __init__(self):
        self.hist = {}

    def push(self, key, down, up):
        self.hist.setdefault(key, collections.deque(maxlen=self.LENGTH)).append((float(down), float(up)))

    def forget(self, alive):
        for key in [k for k in self.hist if k is not None and k not in alive]:
            del self.hist[key]

    def draw(self, canvas, key, title, P, px, bg=None):
        """`bg(canvas)` paints a background first (the app's artwork)."""
        canvas.delete("all")
        if bg:
            bg(canvas)
        w, h = max(canvas.winfo_width(), 50), max(canvas.winfo_height(), 40)
        samples = list(self.hist.get(key, ()))
        peak = max([max(d, u) for d, u in samples] + [1.0])
        peak = _nice_ceiling(peak)
        pad_l, pad_r, pad_t, pad_b = px(20), px(20), px(30), px(12)   # page has no padding of its own
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        for i in range(4):                                   # grid lines with the scale on the left
            y = pad_t + gh * i / 3
            canvas.create_line(pad_l, y, w - pad_r, y, fill=P["border"])
        canvas.create_text(w - pad_r, pad_t - px(4), text=fmt_rate(peak), anchor="se", fill=P["muted"])

        def points(idx):
            n = self.LENGTH
            pts = []
            for i, sample in enumerate(samples):
                x = pad_l + gw * (n - len(samples) + i) / (n - 1)
                pts += [x, pad_t + gh * (1 - sample[idx] / peak)]
            return pts
        for idx, color in ((1, P["good"]), (0, P["bar_down"])):
            pts = points(idx)
            if len(pts) >= 4:
                canvas.create_line(*pts, fill=color, width=max(2, px(2)), smooth=False)
        d, u = samples[-1] if samples else (0.0, 0.0)
        canvas.create_text(pad_l, px(14), anchor="w", fill=P["fg"],
                           text="%s     ↓ %s     ↑ %s" % (title, fmt_rate(d), fmt_rate(u)))


def _nice_ceiling(v):
    """Round a peak rate up to 1, 2, 5 x 10^n so the scale does not jitter every second."""
    for base in (10_000.0, 20_000.0, 50_000.0, 100_000.0, 200_000.0, 500_000.0, 1e6, 2e6, 5e6, 1e7, 2e7, 5e7, 1e8, 2e8, 5e8, 1e9):
        if v <= base:
            return base
    return v
