"""BitTorrent Message Stream Encryption (a.k.a. Protocol Encryption / MSE-PE).

Diffie-Hellman key exchange (768-bit), RC4 stream cipher keyed from the torrent's info-hash,
with the obfuscated handshake described in the Vuze/libtorrent MSE specification.
"""
import asyncio
import hashlib
import os
import random
import struct

P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22"
        "514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6"
        "F44C42E9A63A36210000000000090563", 16)
G = 2
VC = bytes(8)
CRYPTO_PLAIN, CRYPTO_RC4 = 1, 2


class RC4:
    def __init__(self, key, drop=1024):
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) & 255
            S[i], S[j] = S[j], S[i]
        self.S, self.i, self.j = S, 0, 0
        if drop:
            self._keystream(drop)

    def _keystream(self, n):
        S, i, j = self.S, self.i, self.j
        out = bytearray(n)
        for k in range(n):
            i = (i + 1) & 255
            a = S[i]
            j = (j + a) & 255
            b = S[j]
            S[i] = b
            S[j] = a
            out[k] = S[(a + b) & 255]
        self.i, self.j = i, j
        return out

    def process(self, data):
        n = len(data)
        if not n:
            return b""
        ks = self._keystream(n)
        return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(n, "big")


def _sha(*parts):
    return hashlib.sha1(b"".join(parts)).digest()


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


class CryptReader:
    """Wraps a stream reader: decrypts (if a cipher is given) and can start with pre-read bytes."""

    def __init__(self, reader, cipher=None, prefix=b""):
        self.r, self.c, self.buf = reader, cipher, bytearray(prefix)

    async def readexactly(self, n):
        if len(self.buf) >= n:
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out
        data = await self.r.readexactly(n - len(self.buf))
        if self.c:
            data = self.c.process(data)
        out = bytes(self.buf) + data
        self.buf.clear()
        return out


class CryptWriter:
    def __init__(self, writer, cipher):
        self.w, self.c = writer, cipher

    def write(self, data):
        self.w.write(self.c.process(data))

    async def drain(self):
        await self.w.drain()

    def close(self):
        self.w.close()

    def is_closing(self):
        return self.w.is_closing()

    def get_extra_info(self, name, default=None):
        return self.w.get_extra_info(name, default)

    def __getattr__(self, name):  # e.g. .kind of the underlying transport
        return getattr(self.w, name)


async def initiate(reader, writer, skey, allow_plain=False, plain_only=False):
    """Outgoing side. Returns (reader, writer) ready for the normal BitTorrent handshake.
    plain_only: use the encrypted handshake (which hides the BitTorrent header) but ask for plain data afterwards -
    RC4 in pure Python is slow, so this is much faster for big transfers."""
    x = int.from_bytes(os.urandom(20), "big")
    ya = pow(G, x, P).to_bytes(96, "big")
    writer.write(ya + os.urandom(random.randint(0, 200)))
    yb = await reader.readexactly(96)
    s = pow(int.from_bytes(yb, "big"), x, P).to_bytes(96, "big")
    enc, dec = RC4(_sha(b"keyA", s, skey)), RC4(_sha(b"keyB", s, skey))
    expect_vc = RC4(_sha(b"keyB", s, skey)).process(VC)  # B's encrypted VC
    allow_plain = allow_plain or plain_only
    provide = CRYPTO_PLAIN if plain_only else CRYPTO_RC4 | (CRYPTO_PLAIN if allow_plain else 0)
    padc = os.urandom(random.randint(0, 64))
    writer.write(_sha(b"req1", s) + _xor(_sha(b"req2", skey), _sha(b"req3", s))
                 + enc.process(VC + struct.pack(">IH", provide, len(padc)) + padc + struct.pack(">H", 0)))
    # B pads its reply (up to 512 bytes) - scan for the encrypted VC
    buf = bytearray()
    while not buf.endswith(expect_vc):
        buf += await reader.readexactly(1)
        if len(buf) > 512 + 8:
            raise ValueError("MSE: verification constant not found")
    dec.process(VC)  # advance cipher past the VC
    select, padlen = struct.unpack(">IH", dec.process(await reader.readexactly(6)))
    if padlen > 512:
        raise ValueError("MSE: bad padding")
    dec.process(await reader.readexactly(padlen))
    if select == CRYPTO_RC4:
        return CryptReader(reader, dec), CryptWriter(writer, enc)
    if select == CRYPTO_PLAIN and allow_plain:
        return reader, writer
    raise ValueError("MSE: unacceptable crypto_select")


async def respond(reader, writer, first20, skeys, allow_plain=True, prefer_plain=False):
    """Incoming side; `first20` are the 20 bytes already read. Returns (reader, writer, skey)."""
    ya = first20 + await reader.readexactly(76)
    y = int.from_bytes(os.urandom(20), "big")
    writer.write(pow(G, y, P).to_bytes(96, "big") + os.urandom(random.randint(0, 200)))
    s = pow(int.from_bytes(ya, "big"), y, P).to_bytes(96, "big")
    req1 = _sha(b"req1", s)
    buf = bytearray()
    while not buf.endswith(req1):
        buf += await reader.readexactly(1)
        if len(buf) > 512 + 20:
            raise ValueError("MSE: req1 hash not found")
    req23 = await reader.readexactly(20)
    skey = next((k for k in skeys if _xor(_sha(b"req2", k), _sha(b"req3", s)) == req23), None)
    if skey is None:
        raise ValueError("MSE: unknown torrent")
    dec, enc = RC4(_sha(b"keyA", s, skey)), RC4(_sha(b"keyB", s, skey))
    head = dec.process(await reader.readexactly(14))
    if head[:8] != VC:
        raise ValueError("MSE: bad verification constant")
    provide, padlen = struct.unpack(">IH", head[8:])
    if padlen > 512:
        raise ValueError("MSE: bad padding")
    dec.process(await reader.readexactly(padlen))
    ialen = struct.unpack(">H", dec.process(await reader.readexactly(2)))[0]
    ia = dec.process(await reader.readexactly(ialen)) if ialen else b""
    if prefer_plain and allow_plain and provide & CRYPTO_PLAIN:
        select = CRYPTO_PLAIN
    elif provide & CRYPTO_RC4:
        select = CRYPTO_RC4
    elif provide & CRYPTO_PLAIN and allow_plain:
        select = CRYPTO_PLAIN
    else:
        raise ValueError("MSE: no acceptable crypto_provide")
    padd = os.urandom(random.randint(0, 64))
    writer.write(enc.process(VC + struct.pack(">IH", select, len(padd)) + padd))
    if select == CRYPTO_RC4:
        return CryptReader(reader, dec, ia), CryptWriter(writer, enc), skey
    return CryptReader(reader, None, ia), writer, skey
