"""uTP (BEP 29): reliable, congestion-controlled streams over UDP (LEDBAT-style delay control).

UTPSocket owns the connections of one UDP socket. connect() returns an (asyncio.StreamReader,
writer) pair so the rest of the client treats uTP exactly like TCP.
"""
import asyncio
import random
import struct
import time

ST_DATA, ST_FIN, ST_STATE, ST_RESET, ST_SYN = range(5)
HDR = struct.Struct(">BBHIIIHH")
MSS = 1200              # payload bytes per packet (fits the IPv6 minimum MTU)
TARGET = 100_000        # LEDBAT target queuing delay (microseconds)
MAX_CWND = 512 * 1024
RECV_WND = 1 << 20
SEND_HIGH = 256 * 1024
MAX_CONNS = 300


def _us():
    return int(time.monotonic() * 1_000_000) & 0xFFFFFFFF


def _after(a, b):
    """True if 16-bit sequence number a is after b."""
    return 0 < ((a - b) & 0xFFFF) < 0x8000


class UTPWriter:
    kind = "utp"

    def __init__(self, conn):
        self.conn = conn

    def write(self, data):
        c = self.conn
        if c.closing or c.state == "CLOSED" or not data:
            return
        c.sendbuf += data
        if len(c.sendbuf) >= SEND_HIGH:
            c.drain_ev.clear()
        c._flush()

    async def drain(self):
        c = self.conn
        while c.state == "CONNECTED" and len(c.sendbuf) >= SEND_HIGH:
            c.drain_ev.clear()
            await c.drain_ev.wait()
        if c.state == "CLOSED":
            raise ConnectionResetError("uTP connection closed")

    def close(self):
        self.conn.close()

    def is_closing(self):
        return self.conn.closing or self.conn.state == "CLOSED"

    def get_extra_info(self, name, default=None):
        return self.conn.addr if name == "peername" else default


class UTPConnection:
    def __init__(self, sock, addr, recv_id, send_id):
        self.sock, self.addr = sock, addr
        self.recv_id, self.send_id = recv_id, send_id
        self.state = "NEW"
        self.seq_nr, self.ack_nr = 1, 0
        self.reader = asyncio.StreamReader()
        self.writer = UTPWriter(self)
        self.eof_fed = False
        self.out = {}            # seq -> [type, payload, sent_time, retries, size]
        self.sendbuf = bytearray()
        self.inflight = 0
        self.ooo = {}            # out-of-order packets waiting for a gap to fill
        self.peer_wnd = RECV_WND
        self.cwnd, self.ssthresh = float(2 * MSS), float(MAX_CWND)
        self.rtt, self.rtt_var, self.rto = None, 0.0, 1.0
        self.base, self.base_t = None, 0.0
        self.reply_micro = 0
        self.last_recv = time.monotonic()
        self.last_ack, self.dups, self.last_loss = None, 0, 0.0
        self.closing = self.fin_sent = self.fin_acked = self.fin_received = False
        self.fin_time = 0.0
        self.connected = asyncio.get_running_loop().create_future()
        self.drain_ev = asyncio.Event()
        self.drain_ev.set()

    # -- wire -------------------------------------------------------------
    def _wire(self, ptype, seq, payload=b"", cid=None, sack=None):
        ext, eb = 0, b""
        if sack:
            ext, eb = 1, bytes([0, len(sack)]) + sack
        hdr = HDR.pack((ptype << 4) | 1, ext, self.send_id if cid is None else cid,
                       _us(), self.reply_micro, RECV_WND, seq, self.ack_nr)
        self.sock.send(hdr + eb + payload, self.addr)

    def _send_state(self):
        sack = None
        if self.ooo:
            offs = [(s - self.ack_nr - 2) & 0xFFFF for s in self.ooo]
            nbytes = min(((max(offs) // 8) + 4) // 4 * 4, 32)
            mask = bytearray(nbytes)
            for o in offs:
                if o < nbytes * 8:
                    mask[o // 8] |= 1 << (o % 8)
            sack = bytes(mask)
        self._wire(ST_STATE, self.seq_nr, sack=sack)

    def send_syn(self):
        self.state = "SYN_SENT"
        self.out[self.seq_nr] = [ST_SYN, b"", time.monotonic(), 0, 0]
        self._wire(ST_SYN, self.seq_nr, cid=self.recv_id)
        self.seq_nr = (self.seq_nr + 1) & 0xFFFF

    def accept_syn(self, syn_seq):
        self.state = "CONNECTED"
        self.ack_nr = syn_seq
        self.seq_nr = random.randrange(1, 0xFFFF)
        self._send_state()

    def _retransmit(self, seq, now):
        pkt = self.out[seq]
        pkt[3] += 1
        pkt[2] = now
        self._wire(pkt[0], seq, pkt[1], cid=self.recv_id if pkt[0] == ST_SYN else None)

    # -- receiving --------------------------------------------------------
    def on_packet(self, ptype, ts, ts_diff, wnd, seq, ack, sack, payload):
        now = time.monotonic()
        self.last_recv = now
        self.reply_micro = (_us() - ts) & 0xFFFFFFFF
        self.peer_wnd = wnd
        if ptype == ST_RESET:
            self._abort(ConnectionResetError("uTP reset by peer"))
            return
        if self.state == "SYN_SENT":
            if ptype == ST_STATE:
                self.out.pop(1, None)
                self.ack_nr = (seq - 1) & 0xFFFF
                self.state = "CONNECTED"
                if not self.connected.done():
                    self.connected.set_result(True)
                self._flush()
            return
        if self.state != "CONNECTED":
            return
        self._process_ack(ack, sack, ts_diff, now, ptype == ST_STATE)
        if ptype in (ST_DATA, ST_FIN):
            self._on_data(ptype, seq, payload)
        self._flush()

    def _deliver(self, ptype, payload):
        if ptype == ST_DATA:
            if payload and not self.eof_fed:
                self.reader.feed_data(payload)
        else:
            self.fin_received = True
            if not self.eof_fed:
                self.eof_fed = True
                self.reader.feed_eof()

    def _on_data(self, ptype, seq, payload):
        expected = (self.ack_nr + 1) & 0xFFFF
        if seq == expected:
            self._deliver(ptype, payload)
            self.ack_nr = seq
            while True:
                nxt = (self.ack_nr + 1) & 0xFFFF
                item = self.ooo.pop(nxt, None)
                if item is None:
                    break
                self._deliver(*item)
                self.ack_nr = nxt
        elif _after(seq, expected) and ((seq - expected) & 0xFFFF) < 512 and len(self.ooo) < 512:
            self.ooo.setdefault(seq, (ptype, payload))
        self._send_state()

    # -- acks / congestion control ---------------------------------------
    def _release(self, seq, now):
        pkt = self.out.pop(seq, None)
        if pkt is None:
            return 0
        self.inflight -= pkt[4]
        if pkt[3] == 0:
            self._rtt_sample(now - pkt[2])
        if pkt[0] == ST_FIN:
            self.fin_acked, self.fin_time = True, now
        return pkt[4]

    def _rtt_sample(self, s):
        if self.rtt is None:
            self.rtt, self.rtt_var = s, s / 2
        else:
            self.rtt_var = 0.75 * self.rtt_var + 0.25 * abs(self.rtt - s)
            self.rtt = 0.875 * self.rtt + 0.125 * s
        self.rto = min(max(0.3, self.rtt + 4 * self.rtt_var), 10.0)

    def _process_ack(self, ack, sack, ts_diff, now, is_state):
        if _after(ack, (self.seq_nr - 1) & 0xFFFF):
            return  # acknowledges something we never sent
        acked = 0
        for seq in [s for s in self.out if not _after(s, ack)]:
            acked += self._release(seq, now)
        if sack:
            for bi, byte in enumerate(sack):
                if byte:
                    for bit in range(8):
                        if byte & (1 << bit):
                            acked += self._release((ack + 2 + bi * 8 + bit) & 0xFFFF, now)
        if acked:
            self._ledbat(ts_diff, acked, now)
            if self.rtt is not None:  # progress: undo any timeout back-off
                self.rto = min(max(0.3, self.rtt + 4 * self.rtt_var), 10.0)
        if sack and self.out:
            self._sack_loss(ack, sack, now)
        if ack != self.last_ack:
            self.last_ack, self.dups = ack, 0
        elif is_state and self.out:
            self.dups += 1
            if self.dups == 3:  # fast retransmit of the presumed-lost packet
                lost = (ack + 1) & 0xFFFF
                if lost in self.out:
                    self._retransmit(lost, now)
                    self._loss(now)

    def _sack_loss(self, ack, sack, now):
        """A packet is presumed lost once 3+ later packets have been selectively acked."""
        bits = [i for i in range(len(sack) * 8) if sack[i >> 3] >> (i & 7) & 1]
        if len(bits) < 3:
            return
        first_missing = (ack + 1) & 0xFFFF
        lost = False
        for m in list(self.out):
            if m == first_missing:
                later = len(bits)
            else:
                j = (m - ack - 2) & 0xFFFF
                if j >= len(sack) * 8:
                    continue
                later = sum(1 for b in bits if b > j)
            if later >= 3 and now - self.out[m][2] > max(self.rtt or 0.3, 0.1):
                self._retransmit(m, now)
                lost = True
        if lost:
            self._loss(now)

    def _loss(self, now):
        if now - self.last_loss > (self.rtt or 0.5):
            self.ssthresh = max(self.cwnd / 2, 2.0 * MSS)
            self.cwnd = self.ssthresh
            self.last_loss = now

    def _ledbat(self, ts_diff, acked, now):
        if now - self.base_t > 120 or self.base is None:
            self.base, self.base_t = ts_diff, now
        d = (ts_diff - self.base) & 0xFFFFFFFF
        if d >= 1 << 31:  # negative: new minimum
            self.base, d = ts_diff, 0
        if self.cwnd < self.ssthresh and d < TARGET // 2:
            self.cwnd += acked  # slow start
        else:
            off = (TARGET - d) / TARGET
            self.cwnd += off * acked * MSS / self.cwnd
        self.cwnd = max(2.0 * MSS if d < TARGET else float(MSS), min(self.cwnd, MAX_CWND))

    # -- sending ----------------------------------------------------------
    def _flush(self):
        if self.state != "CONNECTED":
            return
        now = time.monotonic()
        wnd = max(min(self.cwnd, self.peer_wnd), MSS)
        while self.sendbuf and (self.inflight == 0 or
                                self.inflight + min(len(self.sendbuf), MSS) <= wnd):
            chunk = bytes(self.sendbuf[:MSS])
            del self.sendbuf[:MSS]
            seq = self.seq_nr
            self.out[seq] = [ST_DATA, chunk, now, 0, len(chunk)]
            self.inflight += len(chunk)
            self._wire(ST_DATA, seq, chunk)
            self.seq_nr = (seq + 1) & 0xFFFF
        if self.closing and not self.sendbuf and not self.fin_sent:
            seq = self.seq_nr
            self.out[seq] = [ST_FIN, b"", now, 0, 0]
            self.fin_sent = True
            self._wire(ST_FIN, seq)
            self.seq_nr = (seq + 1) & 0xFFFF
        if len(self.sendbuf) < SEND_HIGH // 2:
            self.drain_ev.set()

    def tick(self, now):
        if self.state == "SYN_SENT":
            pkt = self.out.get(1)
            if pkt and now - pkt[2] > min(2.0 ** pkt[3], 4.0):
                if pkt[3] >= 3:
                    self._abort(TimeoutError("uTP connect timed out"))
                else:
                    self._retransmit(1, now)
            return
        if self.state != "CONNECTED":
            return
        if now - self.last_recv > 150:
            self._abort(TimeoutError("uTP peer silent"))
            return
        if self.out:
            expired = [q for q, p in self.out.items() if now - p[2] > self.rto][:4]
            if expired:
                if any(self.out[q][3] >= 8 for q in expired):
                    self._abort(TimeoutError("uTP retransmit limit"))
                    return
                self.ssthresh = max(self.cwnd / 2, 2.0 * MSS)
                self.cwnd = float(MSS)
                self.rto = min(self.rto * 2, 30.0)
                for q in expired:
                    self._retransmit(q, now)
        if self.fin_acked and (self.fin_received or now - self.fin_time > 3):
            self._finish()
            return
        self._flush()

    # -- teardown ---------------------------------------------------------
    def close(self):
        if self.state == "CLOSED" or self.closing:
            return
        if self.state != "CONNECTED":
            self._abort()
            return
        self.closing = True
        self._flush()

    def _finish(self):
        self.state = "CLOSED"
        if not self.eof_fed:
            self.eof_fed = True
            self.reader.feed_eof()
        self.drain_ev.set()
        self.sock.remove(self)

    def _abort(self, exc=None):
        if self.state == "CLOSED":
            return
        self.state = "CLOSED"
        if not self.connected.done():
            self.connected.set_exception(exc or ConnectionResetError("uTP closed"))
            self.connected.exception()  # mark retrieved
        if not self.eof_fed:
            self.eof_fed = True
            self.reader.feed_eof()
        self.drain_ev.set()
        self.sock.remove(self)


class UTPSocket:
    """Demultiplexes uTP packets arriving on one UDP socket."""

    def __init__(self, send, on_accept, can_accept=lambda: True):
        self._send, self.on_accept, self.can_accept = send, on_accept, can_accept
        self.conns = {}
        self.task = None

    def send(self, data, addr):
        try:
            self._send(data, addr)
        except (OSError, AttributeError):
            pass

    def start(self):
        self.task = asyncio.get_running_loop().create_task(self._tick())

    def close(self):
        if self.task:
            self.task.cancel()
        for c in list(self.conns.values()):
            c._abort()

    def remove(self, conn):
        self.conns.pop((conn.addr, conn.recv_id), None)

    async def _tick(self):
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            for c in list(self.conns.values()):
                c.tick(now)

    async def connect(self, addr, timeout=6):
        if len(self.conns) >= MAX_CONNS:
            raise OSError("too many uTP connections")
        rid = random.randrange(1, 0xFFFE)
        while (addr, rid) in self.conns:
            rid = random.randrange(1, 0xFFFE)
        conn = UTPConnection(self, addr, rid, (rid + 1) & 0xFFFF)
        self.conns[(addr, rid)] = conn
        conn.send_syn()
        try:
            await asyncio.wait_for(asyncio.shield(conn.connected), timeout)
        except BaseException:
            conn._abort()
            raise
        return conn.reader, conn.writer

    def datagram_received(self, data, addr):
        if len(data) < 20:
            return
        b0, ext, cid, ts, ts_diff, wnd, seq, ack = HDR.unpack_from(data)
        ptype, ver = b0 >> 4, b0 & 0x0F
        if ver != 1 or ptype > ST_SYN:
            return
        off, sack = 20, None
        while ext:
            if off + 2 > len(data):
                return
            nxt, ln = data[off], data[off + 1]
            body = data[off + 2:off + 2 + ln]
            if len(body) != ln:
                return
            if ext == 1:
                sack = body
            ext, off = nxt, off + 2 + ln
        payload = data[off:]
        if ptype == ST_SYN:
            key = (addr, (cid + 1) & 0xFFFF)
            conn = self.conns.get(key)
            if conn:  # retransmitted SYN: our STATE reply was lost
                conn._send_state()
            elif self.can_accept() and len(self.conns) < MAX_CONNS:
                conn = UTPConnection(self, addr, (cid + 1) & 0xFFFF, cid)
                self.conns[key] = conn
                conn.accept_syn(seq)
                self.on_accept(conn.reader, conn.writer)
            return
        conn = self.conns.get((addr, cid))
        if conn:
            conn.on_packet(ptype, ts, ts_diff, wnd, seq, ack, sack, payload)
