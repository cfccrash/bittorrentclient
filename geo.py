"""Where in the world is a peer? IPv4 -> country (offline, DB-IP Lite data) plus small national flag images.

IP data: DB-IP Lite country database, https://db-ip.com (CC BY 4.0).  Flags: flag-icons (MIT).
Country is the registered location of the address block: accurate for most users, but VPNs, CDNs and
mobile networks can put a peer somewhere else."""
import base64
import bisect
import ipaddress
import socket
import struct
import zlib

_starts = _vals = None
_images = {}


def _load():
    global _starts, _vals
    if _starts is None:
        import geo_data
        raw = zlib.decompress(base64.b64decode(geo_data.BLOB))
        n = geo_data.COUNT
        _starts = struct.unpack(">%dI" % n, raw[:4 * n])
        _vals = raw[4 * n:]
    return _starts, _vals


def country(ip):
    """Two-letter country code for an IPv4 address string, or None (private / unknown / not IPv4)."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return None
    import geo_data
    starts, vals = _load()
    i = bisect.bisect_right(starts, int(addr)) - 1
    if i < 0 or vals[i] >= len(geo_data.CODES):
        return None
    return geo_data.CODES[vals[i]]


def name(code):
    import flags_data
    return flags_data.NAMES.get(code, code or "")


def flag_image(code, hidpi=False):
    """A tkinter PhotoImage of the flag (cached; keep the reference alive by using the cache), or None."""
    key = (code, hidpi)
    if key not in _images:
        img = None
        try:
            import tkinter as tk
            import flags_data
            pair = flags_data.FLAGS.get(code)
            if pair:
                img = tk.PhotoImage(data=base64.b64encode(zlib.decompress(base64.b64decode(pair[1 if hidpi else 0]))))
        except Exception:
            img = None
        _images[key] = img
    return _images[key]
