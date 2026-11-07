"""Bandwidth schedule: pick the speed limits that apply right now (pure functions, easy to test)."""
import datetime

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse_hhmm(text, default=0):
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    return default


def valid_hhmm(text):
    try:
        h, m = str(text).strip().split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except ValueError:
        return False


def in_window(cfg, now=None):
    """True when the schedule is on and `now` falls inside its time window on a chosen day."""
    if not cfg.get("sched_on"):
        return False
    now = now or datetime.datetime.now()
    days = set(cfg.get("sched_days", range(7)))
    start, end = parse_hhmm(cfg.get("sched_start"), 22 * 60), parse_hhmm(cfg.get("sched_end"), 7 * 60)
    t = now.hour * 60 + now.minute
    if start == end:
        return now.weekday() in days            # a whole day
    if start < end:
        return start <= t < end and now.weekday() in days
    # overnight window: belongs to the day it starts on
    if t >= start:
        return now.weekday() in days
    if t < end:
        return (now.weekday() - 1) % 7 in days
    return False


def effective(cfg, now=None):
    """-> (down KiB/s, up KiB/s, mode) with mode 'normal', 'alt' (switched on by hand) or 'schedule'."""
    if cfg.get("alt_on"):
        return int(cfg.get("alt_down", 0)), int(cfg.get("alt_up", 0)), "alt"
    if in_window(cfg, now) and not cfg.get("sched_override"):
        return int(cfg.get("alt_down", 0)), int(cfg.get("alt_up", 0)), "schedule"
    return int(cfg.get("down_limit", 0)), int(cfg.get("up_limit", 0)), "normal"
