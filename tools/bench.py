"""Loopback benchmark: one seeder and one downloader on this machine.

    python tools/bench.py [size_MiB=256] [piece_KiB=256] [--profile=down|up]

Prints the transfer speed and, with --profile, the functions that used the most CPU on one side.
There is no network in the way, so the number is the most this program can move on this computer (the real
speed is whatever the swarm and your line allow)."""
import cProfile
import os
import pstats
import io
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from torrent_core import Manager, create_torrent


def profile_loop(mgr):
    """Start cProfile inside a manager's event-loop thread; returns a function that stops it and returns the report."""
    prof = cProfile.Profile()
    started = threading.Event()

    def on():
        prof.enable()
        started.set()
    mgr.loop.call_soon_threadsafe(on)
    started.wait(5)

    def stop(top=14):
        done = threading.Event()

        def off():
            prof.disable()
            done.set()
        mgr.loop.call_soon_threadsafe(off)
        done.wait(5)
        out = io.StringIO()
        pstats.Stats(prof, stream=out).strip_dirs().sort_stats("tottime").print_stats(top)
        return out.getvalue()
    return stop


def run(size_mib=256, piece_kib=256, do_profile=None, quiet=False):
    """do_profile: None, "down" or "up" (Python 3.12+ allows one profiler at a time, so one side per run)."""
    tmp = tempfile.mkdtemp()
    try:
        seed_dir, dl_dir = os.path.join(tmp, "seed"), os.path.join(tmp, "dl")
        os.makedirs(seed_dir)
        os.makedirs(dl_dir)
        path = os.path.join(seed_dir, "data.bin")
        chunk = os.urandom(1 << 20)
        with open(path, "wb") as f:
            for _ in range(size_mib):
                f.write(chunk)                # repeated data is fine: pieces still hash, nothing is compressed
        raw = create_torrent(path, piece_length=piece_kib * 1024)
        a = Manager(0, dht=False, encryption="off", utp="off")
        b = Manager(0, dht=False, encryption="off", utp="off")
        try:
            sa, _ = a.add(raw, seed_dir)
            deadline = time.time() + 120
            while time.time() < deadline and not (sa.complete and not sa.stopped):
                time.sleep(0.1)
            assert sa.complete, "seeder did not finish its hash check"
            sb, _ = b.add(raw, dl_dir)
            stop = profile_loop(b if do_profile == "down" else a) if do_profile else None
            t0 = time.time()
            b.add_peer(sb.t.hex, "127.0.0.1", a.port)
            while time.time() - t0 < 300 and not sb.complete:
                time.sleep(0.05)
            dt = time.time() - t0
            assert sb.complete, "download did not finish"
            rate = size_mib / dt
            if not quiet:
                print("%d MiB, %d KiB pieces: %.1f s  ->  %.1f MiB/s" % (size_mib, piece_kib, dt, rate))
                if do_profile:
                    print("\n--- %s (top by own time; epoll 'poll' is idle waiting) ---\n%s"
                          % ("downloader" if do_profile == "down" else "seeder", stop()))
            return rate
        finally:
            a.shutdown()
            b.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    prof = next((a.split("=")[1] for a in sys.argv if a.startswith("--profile=")), None)
    run(int(args[0]) if args else 256, int(args[1]) if len(args) > 1 else 256, prof)
