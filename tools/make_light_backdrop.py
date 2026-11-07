"""Make the light-theme aurora (assets_ui.BACKDROP_WIDE_LIGHT) from the dark one.   python tools/make_light_backdrop.py

BACKDROP_WIDE is a paletted PNG, so only its PLTE chunk changes: every colour's glow (how far it is from the dark
background) becomes a soft tint of the same hue over the light background. Stdlib only; re-run after changing the
dark artwork or the palettes."""
import base64
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import assets_ui  # noqa: E402
import ui  # noqa: E402

GAIN, STRENGTH, INK = 1.9, 0.55, 0.62   # how fast glow turns into colour, max cover, how dark the tint is


def rgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def recolour(palette, dark_bg, light_bg):
    out = bytearray()
    for i in range(0, len(palette), 3):
        p = palette[i:i + 3]
        diff = [max(0, c - b) for c, b in zip(p, dark_bg)]
        peak = max(diff)
        if peak == 0:
            out += bytes(light_bg)
            continue
        ink = [int(255 * d / peak * INK) for d in diff]                       # same hue, mid-dark
        a = min(1.0, peak / 255 * GAIN) * STRENGTH
        out += bytes(int(round(l * (1 - a) + k * a)) for l, k in zip(light_bg, ink))
    return bytes(out)


def main():
    raw = base64.b64decode(assets_ui.BACKDROP_WIDE)
    out, i = bytearray(raw[:8]), 8
    while i < len(raw):
        ln, typ = struct.unpack(">I4s", raw[i:i + 8])
        data = raw[i + 8:i + 8 + ln]
        if typ == b"PLTE":
            data = recolour(data, rgb(ui.DARK["bg"]), rgb(ui.LIGHT["bg"]))
        out += struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        i += 12 + ln
    line = "BACKDROP_WIDE_LIGHT = '%s'\n" % base64.b64encode(bytes(out)).decode()
    path = os.path.join(HERE, "assets_ui.py")
    src = open(path, encoding="utf-8").read()
    if re.search(r"^BACKDROP_WIDE_LIGHT = ", src, re.M):
        src = re.sub(r"^BACKDROP_WIDE_LIGHT = .*\n", lambda m: line, src, flags=re.M)
    else:
        src = src.rstrip("\n") + "\n" + line
    open(path, "w", encoding="utf-8", newline="").write(src)
    print("BACKDROP_WIDE_LIGHT written (%d bytes of PNG)" % len(out))


if __name__ == "__main__":
    main()
