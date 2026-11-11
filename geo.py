"""Where in the world is a peer? IPv4 / IPv6 -> country (offline, DB-IP Lite data) plus small national flag images.

IP data: DB-IP Lite country database, https://db-ip.com (CC BY 4.0).  Flags: flag-icons (MIT).
Country is the registered location of the address block: accurate for most users, but VPNs, CDNs and
mobile networks can put a peer somewhere else."""
import base64
import bisect
import ipaddress
import socket
import struct
import zlib

_tables = {}
_images = {}
# places in the IP data that flags_data has no entry for
_EXTRA_NAMES = {"CX": "Christmas Island", "EH": "Western Sahara", "TF": "French Southern Territories"}


def _load(v6=False):
    """(range starts, country index per range) for IPv4, or for IPv6 by /64 prefix."""
    if v6 not in _tables:
        import geo_data
        blob, n, size, fmt = ((geo_data.BLOB6, geo_data.COUNT6, 8, "Q") if v6 else
                              (geo_data.BLOB, geo_data.COUNT, 4, "I"))
        raw = zlib.decompress(base64.b64decode(blob))
        _tables[v6] = (struct.unpack(">%d%s" % (n, fmt), raw[:size * n]), raw[size * n:])
    return _tables[v6]


def country(ip):
    """Two-letter country code for an IPv4 or IPv6 address string, or None (private / unknown / not an address)."""
    try:
        addr = ipaddress.ip_address(ip.strip("[]"))
    except (ValueError, AttributeError):
        return None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if not addr.is_global or addr.is_multicast:
        return None
    import geo_data
    v6 = addr.version == 6
    starts, vals = _load(v6)
    i = bisect.bisect_right(starts, int(addr) >> 64 if v6 else int(addr)) - 1
    if i < 0 or vals[i] >= len(geo_data.CODES):
        return None
    return geo_data.CODES[vals[i]]


def name(code):
    import flags_data
    return flags_data.NAMES.get(code) or _EXTRA_NAMES.get(code) or code or ""


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
