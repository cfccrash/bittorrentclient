"""Engine tests for 1.4: file priority, in-order download, banned peers, disk-full guard, bandwidth schedule."""
import datetime, os, socket, sys, tempfile, time, types
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schedule
from torrent_core import Manager, create_torrent


def wait(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class FakePeer:
    def __init__(self, has):
        self.has = has


def picks(s, peer, n):
    """First piece numbers handed out by next_block, in order, until n distinct pieces were started."""
    seen = []
    for _ in range(n * 4):
        r = s.next_block(peer)
        if r is None:
            break
        s.partial[r[0]].req.setdefault(r[1], {})[peer] = time.time()      # what Peer.fill does after asking for the block
        if r[0] not in seen:
            seen.append(r[0])
        if len(seen) == n:
            break
    return seen


def test_schedule():
    def at(day, hh, mm=0):      # 2026-11-02 is a Monday
        return datetime.datetime(2026, 11, 2 + day, hh, mm)
    cfg = dict(down_limit=0, up_limit=0, alt_down=100, alt_up=50, sched_on=True, sched_start="08:00", sched_end="23:00",
               sched_days=list(range(7)))
    assert schedule.effective(cfg, at(0, 9)) == (100, 50, "schedule")
    assert schedule.effective(cfg, at(0, 7, 59)) == (0, 0, "normal") and schedule.effective(cfg, at(0, 23)) == (0, 0, "normal")
    cfg["sched_days"] = [0, 1, 2, 3, 4]                       # weekdays only
    assert schedule.effective(cfg, at(5, 12))[2] == "normal" and schedule.effective(cfg, at(4, 12))[2] == "schedule"
    cfg.update(sched_start="22:00", sched_end="07:00", sched_days=[4])   # Friday night, running into Saturday morning
    assert schedule.in_window(cfg, at(4, 23)) and schedule.in_window(cfg, at(5, 3)) and not schedule.in_window(cfg, at(5, 8))
    assert not schedule.in_window(cfg, at(3, 23)) and not schedule.in_window(cfg, at(6, 3))
    cfg["sched_on"] = False
    assert schedule.effective(cfg, at(4, 23))[2] == "normal"
    cfg["alt_on"] = True
    assert schedule.effective(cfg, at(0, 12)) == (100, 50, "alt")
    cfg.update(alt_on=False, sched_on=True, sched_override=True)
    assert schedule.effective(cfg, at(4, 23))[2] == "normal"
    assert schedule.valid_hhmm("08:30") and not schedule.valid_hhmm("25:00") and not schedule.valid_hhmm("noon")
    print("bandwidth schedule (window / overnight / days / manual / override): OK")


class ChokePeer:
    def __init__(self, rate, interested=True, since=None):
        self.rate_in = self.rate_out = rate
        self.peer_interested = interested
        self.am_choking = True
        self.choked = False
        self.outstanding = set()
        self.last_data = self.last_rx = time.time()
        self.since = since if since is not None else time.time() - 300
        self.sent, self.closed = [], False

    def send(self, mid, payload=b""):
        self.sent.append(mid)

    def close(self):
        self.closed = True


def test_choking(mgr, tmp):
    from torrent_core import MAX_PEERS, UNCHOKE_SLOTS
    src = os.path.join(tmp, "choke.bin")
    open(src, "wb").write(os.urandom(70000))
    s, _ = mgr.add(create_torrent(src, piece_length=32768), os.path.join(tmp, "ck"), start=False)
    peers = [ChokePeer(1000 * i) for i in range(1, 9)]          # rates 1000 .. 8000, all interested
    lazy = ChokePeer(99999, interested=False)                    # fastest, but not interested
    s.peers.update(peers + [lazy])
    s.rechoke()
    open_ = [p for p in peers if not p.am_choking]
    assert len(open_) == UNCHOKE_SLOTS + 1 and lazy.am_choking, len(open_)
    assert all(not p.am_choking for p in peers[-UNCHOKE_SLOTS:]), "the fastest four must be unchoked"
    first_opt = s._opt
    assert first_opt in peers[:4]
    s.rechoke()                                                   # inside the rotation time: same optimistic peer
    assert s._opt is first_opt
    s._opt_since -= 31
    s.rechoke()
    assert len([p for p in peers if not p.am_choking]) == UNCHOKE_SLOTS + 1
    peers[7].peer_interested = False                              # a regular peer loses interest: choked, slot refilled
    s.rechoke()
    assert peers[7].am_choking and len([p for p in peers if not p.am_choking]) == UNCHOKE_SLOTS + 1
    assert peers[7].sent[-1] == 0 and peers[7].sent[0] == 1
    mgr.set_limits(0, 16)                                         # a 16 KiB/s upload limit leaves room for two slots
    assert s.slots() == 3
    mgr.set_limits(0, 0)
    assert s.slots() == UNCHOKE_SLOTS + 1
    # snubbed peers are dropped; so are useless ones when the list is full and others are waiting
    snub = ChokePeer(0); snub.outstanding = {(0, 0)}; snub.last_data = time.time() - 40
    ok = ChokePeer(0); ok.outstanding = {(0, 0)}
    s.peers.update([snub, ok])
    s.churn()
    assert snub.closed and not ok.closed
    s.peers.clear()
    crowd = [ChokePeer(0 if i < 5 else 5000) for i in range(MAX_PEERS)]
    s.peers.update(crowd)
    s.churn()
    assert len([p for p in crowd if p.closed]) == 0              # nobody waiting to replace them: leave them be
    s.candidates.add(("9.9.9.9", 1))
    s.churn()
    assert [i for i, p in enumerate(crowd) if p.closed] and len([p for p in crowd if p.closed]) == 2 \
        and all(i < 5 for i, p in enumerate(crowd) if p.closed)
    s.peers.clear()
    print("choking (merit slots, optimistic rotation, interest, upload limit, snub/useless peers): OK")


def main():
    test_schedule()
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "Show")
    os.makedirs(src)
    for name in ("ep1.bin", "ep2.bin"):
        with open(os.path.join(src, name), "wb") as f:
            f.write(os.urandom(4 * 32768))
    raw = create_torrent(src, piece_length=32768)
    mgr = Manager(0, dht=False)
    try:
        empty = os.path.join(tmp, "dl")
        os.makedirs(empty)
        s, _ = mgr.add(raw, empty, start=False)
        peer = FakePeer([True] * 8)
        # in-order download
        mgr.set_sequential([s.t.hex], True)
        assert picks(s, peer, 8) == [0, 1, 2, 3, 4, 5, 6, 7]
        s.partial.clear()
        # high priority: the second episode first, still in order inside it
        names = [f[0][-1] for f in s.t.files]
        mgr.set_file_prio(s.t.hex, [names.index("ep2.bin")], "high")
        assert s.high == {names.index("ep2.bin")}
        assert picks(s, peer, 8) == [4, 5, 6, 7, 0, 1, 2, 3]
        s.partial.clear()
        # skipping removes pieces from the wish list; the last file can't be skipped
        mgr.set_file_prio(s.t.hex, [names.index("ep1.bin")], "skip")
        assert picks(s, peer, 8) == [4, 5, 6, 7] and s.skip == {names.index("ep1.bin")}
        try:
            mgr.set_file_prio(s.t.hex, [names.index("ep2.bin")], "skip"); raise SystemExit("skipped every file")
        except ValueError:
            pass
        mgr.set_file_prio(s.t.hex, [names.index("ep1.bin")], "normal")
        assert not s.skip and s.high == {names.index("ep2.bin")}
        det = mgr.details(s.t.hex)
        assert all(len(f) == 5 for f in det["files"]) and det["files"][names.index("ep2.bin")][4] is True and det["sequential"]
        snap = [x for x in mgr.snapshot() if x["hash"] == s.t.hex][0]
        assert snap["sequential"] is True
        print("file priority + download in order (picker order, skip, details, snapshot): OK")

        test_choking(mgr, tmp)
        # starting state carries over (what the app restores)
        s2, _ = mgr.add(create_torrent(src, piece_length=65536), empty, start=False, high=[1], sequential=True)
        assert s2.high == {1} and s2.sequential and s2.hot.count(True) > 0
        print("restore high/sequential on add: OK")

        # banned peers: refused when they connect, never dialled
        seed, _ = mgr.add(create_torrent(src, piece_length=131072), tmp, start=True)
        assert wait(lambda: seed.complete and not seed.stopped, 20), seed.state
        mgr.ban("127.0.0.1")
        c = socket.create_connection(("127.0.0.1", mgr.port), 5)
        c.settimeout(5)
        c.sendall(b"\x13BitTorrent protocol" + bytes(8) + seed.t.info_hash + b"-XX0001-" + b"0" * 12)
        try:
            data = c.recv(100)
        except (ConnectionError, socket.timeout):
            data = b""
        c.close()
        assert data == b"", "banned peer got an answer"
        mgr.unban("127.0.0.1")
        assert "127.0.0.1" not in mgr.banned
        mgr.ban("10.9.8.7")
        before = dict(seed.tried)
        mgr._run(seed.connect(("10.9.8.7", 6881)))
        assert ("10.9.8.7", 6881) not in seed.tried and seed.tried == before
        mgr.unban()
        assert not mgr.banned
        print("banned peers (incoming refused, never dialled, unban): OK")

        # disk-full guard
        dl, _ = mgr.add(create_torrent(src, piece_length=16384), os.path.join(tmp, "full"), start=True)
        dl.free_space = lambda: 1024
        assert wait(lambda: dl.error and dl.stopped, 15), dl.state
        assert "Disk full" in dl.error
        print("low disk space stops the download with a clear error: OK")
    finally:
        mgr.shutdown()
    print("V1.4 ENGINE TESTS: ALL PASSED")


if __name__ == "__main__":
    main()
