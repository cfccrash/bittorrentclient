"""Remembers where you stopped in each file and which files you finished. A small JSON file shared by the main window
and the player process (each write replaces the file atomically; keys are '<infohash>:<file index>')."""
import json
import os
import tempfile
import threading
import time

WATCHED_AT = 0.92          # past this fraction of the length counts as watched
MIN_RESUME = 20            # seconds: do not resume (or remember) the very beginning
_lock = threading.Lock()


def path(data_dir):
    return os.path.join(data_dir, "media_state.json")


def load(data_dir):
    try:
        with open(path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data_dir, d):
    fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, path(data_dir))


def update(data_dir, key, pos, dur, title=""):
    """Record a position. Returns the stored entry."""
    if not key or dur <= 0:
        return None
    with _lock:
        d = load(data_dir)
        e = d.get(key, {})
        e.update(pos=float(pos), dur=float(dur), at=time.time())
        if title:
            e["title"] = title
        if pos >= dur * WATCHED_AT:
            e["watched"] = True
            e["pos"] = 0.0
        elif pos >= MIN_RESUME:
            e["watched"] = False
        if len(d) > 2000:                                  # keep the file small: drop the oldest entries
            for k in sorted(d, key=lambda k: d[k].get("at", 0))[:200]:
                d.pop(k, None)
        d[key] = e
        try:
            _write(data_dir, d)
        except OSError:
            pass
        return e


def resume_pos(data_dir, key):
    e = load(data_dir).get(key) or {}
    return e["pos"] if e.get("pos", 0) >= MIN_RESUME and not e.get("watched") else 0.0


def is_watched(data_dir, key):
    return bool((load(data_dir).get(key) or {}).get("watched"))


def set_watched(data_dir, key, on):
    with _lock:
        d = load(data_dir)
        e = d.setdefault(key, {})
        e.update(watched=bool(on), at=time.time())
        if on:
            e["pos"] = 0.0
        try:
            _write(data_dir, d)
        except OSError:
            pass


def continue_list(data_dir, limit=20):
    """[(key, entry)] of partly watched files, most recent first."""
    d = load(data_dir)
    items = [(k, e) for k, e in d.items() if e.get("pos", 0) >= MIN_RESUME and not e.get("watched")]
    items.sort(key=lambda kv: -kv[1].get("at", 0))
    return items[:limit]
