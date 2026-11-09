"""Replace the peer flags in flags_data.py with the (waving, Noto Color Emoji style) flags found in a PDF.

Usage: python tools/flags_from_pdf.py flags_of_the_world.pdf

The PDF (made by WeasyPrint) draws each flag emoji as an image XObject named /i<font><glyph id>0; the emoji font's
ToUnicode map turns the glyph into its two regional-indicator letters = the ISO country code. Each image is trimmed to
its visible part and scaled down (area average, premultiplied alpha) into the 20x15 and 40x30 boxes the app uses.
Countries the PDF has no flag for keep the flag they had. Stdlib only.
"""
import base64
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SIZES = ((20, 15), (40, 30))


def _objects(data):
    return {int(n): b for n, b in re.findall(rb'(\d+) 0 obj(.*?)endobj', data, re.S)}


def _stream(b):
    m = re.search(rb'stream\r?\n', b)
    return zlib.decompress(b[m.end():b.rfind(b'endstream')])


def _unpredict(raw, w, bpp):
    """Undo the PNG row predictors (/Predictor 15)."""
    stride, out, prev, i = w * bpp, bytearray(), bytearray(w * bpp), 0
    while i < len(raw):
        ft, row = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = row[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ft == 1:
                row[x] = (row[x] + a) & 255
            elif ft == 2:
                row[x] = (row[x] + b) & 255
            elif ft == 3:
                row[x] = (row[x] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[x] = (row[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += row
        prev = row
    return bytes(out)


def pdf_flags(path):
    """{country code: (w, h, rgba bytes)} for every flag emoji image in the PDF."""
    data = open(path, 'rb').read()
    objs = _objects(data)
    text = b''.join(_stream(b) for b in objs.values() if b'/Type /ObjStm' in b).decode('latin1')
    names = {}                                  # glyph id -> image object
    for font, gid, obj in re.findall(r'/i([A-Z]+)(\d+)\s+(\d+) 0 R', text):
        names[int(gid) // 10] = int(obj)
    codes = {}                                  # glyph id -> country code (from the ToUnicode maps)
    for b in objs.values():
        if b'stream' not in b or b'/Subtype /Image' in b:
            continue
        try:
            s = _stream(b).decode('latin1')
        except zlib.error:
            continue
        if 'beginbfchar' not in s:
            continue
        for gid, uni in re.findall(r'<([0-9a-f]{4})>\s*<([0-9a-f]+)>', s):
            try:
                u = bytes.fromhex(uni).decode('utf-16-be')
            except ValueError:
                continue
            if len(u) == 2 and all(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in u):
                codes[int(gid, 16)] = ''.join(chr(ord(ch) - 0x1F1E6 + 65) for ch in u)
    out = {}
    for gid, obj in names.items():
        code, b = codes.get(gid), objs.get(obj)
        if not code or b is None or b'/DeviceRGB' not in b:
            continue
        w = int(re.search(rb'/Width (\d+)', b).group(1))
        h = int(re.search(rb'/Height (\d+)', b).group(1))
        rgb = _unpredict(_stream(b), w, 3)
        sm = re.search(rb'/SMask (\d+) 0 R', b)
        alpha = _unpredict(_stream(objs[int(sm.group(1))]), w, 1) if sm else bytes([255]) * (w * h)
        rgba = bytearray(w * h * 4)
        rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = rgb[0::3], rgb[1::3], rgb[2::3], alpha
        out[code] = (w, h, bytes(rgba))
    return out


def _trim(w, h, rgba):
    """Crop to the pixels that are not (almost) fully transparent."""
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if rgba[(y * w + x) * 4 + 3] > 8:
                xs.append(x)
                ys.append(y)
    x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
    cw, ch = x1 - x0, y1 - y0
    out = bytearray(cw * ch * 4)
    for y in range(ch):
        s = ((y0 + y) * w + x0) * 4
        out[y * cw * 4:(y + 1) * cw * 4] = rgba[s:s + cw * 4]
    return cw, ch, bytes(out)


def _fit(w, h, rgba, bw, bh):
    """Scale into a bw x bh box (aspect kept, centred, transparent padding) by area averaging."""
    s = min(bw / w, bh / h)
    tw, th = max(1, round(w * s)), max(1, round(h * s))
    ox, oy = (bw - tw) // 2, (bh - th) // 2
    out = bytearray(bw * bh * 4)
    for ty in range(th):
        sy0, sy1 = ty * h / th, (ty + 1) * h / th
        for tx in range(tw):
            sx0, sx1 = tx * w / tw, (tx + 1) * w / tw
            acc = [0.0, 0.0, 0.0, 0.0]
            area = 0.0
            y = int(sy0)
            while y < sy1 and y < h:
                fy = min(sy1, y + 1) - max(sy0, y)
                x = int(sx0)
                while x < sx1 and x < w:
                    f = (min(sx1, x + 1) - max(sx0, x)) * fy
                    i = (y * w + x) * 4
                    a = rgba[i + 3] / 255.0
                    acc[0] += rgba[i] * a * f
                    acc[1] += rgba[i + 1] * a * f
                    acc[2] += rgba[i + 2] * a * f
                    acc[3] += a * f
                    area += f
                    x += 1
                y += 1
            o = ((oy + ty) * bw + ox + tx) * 4
            if acc[3] > 0:
                out[o:o + 3] = bytes(min(255, int(round(c / acc[3]))) for c in acc[:3])
            out[o + 3] = min(255, int(round(255 * acc[3] / area))) if area else 0
    return bytes(out)


def _png(w, h, rgba):
    raw = b''.join(b'\0' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(k, x):
        return struct.pack('>I', len(x)) + k + x + struct.pack('>I', zlib.crc32(k + x) & 0xffffffff)
    return (bytes([137, 80, 78, 71, 13, 10, 26, 10]) + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def main(pdf):
    sys.path.insert(0, ROOT)
    import flags_data
    flags = dict(flags_data.FLAGS)
    found = pdf_flags(pdf)
    for code, (w, h, rgba) in sorted(found.items()):
        w, h, rgba = _trim(w, h, rgba)
        flags[code] = tuple(base64.b64encode(zlib.compress(_png(bw, bh, _fit(w, h, rgba, bw, bh)), 9)).decode()
                            for bw, bh in SIZES)
    with open(os.path.join(ROOT, 'flags_data.py'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('"""Country names and 20x15 / 40x30 PNG flags (zlib+base64). Waving flags from Noto Color Emoji (Apache 2.0, '
                'imported by tools/flags_from_pdf.py);\nthe rest from flag-icons (MIT, tools/build_geo.py)."""\n')
        f.write("NAMES = %r\nFLAGS = %r\n" % (flags_data.NAMES, flags))
    print("replaced %d flags, kept %d" % (len(found), len(flags) - len(found)))


if __name__ == '__main__':
    main(sys.argv[1])
