"""Run every test script and print a summary.   python run_tests.py [name ...]   (names without .py / test_ prefix)

Tests that need Tk run against a real display; on Linux start one with `Xvfb :99 &` and `export DISPLAY=:99`.
Set BTC_CHROME=1 to exercise the custom window frames (on by default only on Windows); test_chrome sets it itself.

A failing test's whole output is saved to test_logs/<test>.log and its last lines are printed here.
Each test gets its own TEMP folder, deleted afterwards, so test data does not pile up in %TEMP%.
On Windows, network filters such as AdGuard break the loopback tests at random (see CLAUDE.md)."""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

here = os.path.dirname(os.path.abspath(__file__))
logs = os.path.join(here, "test_logs")
names = sys.argv[1:] or sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(here, "test_*.py")))
env = dict(os.environ, BTC_TRACEBACKS="1")      # crashlog also prints uncaught errors, so they are not hidden
NOISE = ("WinError 10022", "_call_connection_lost", "Exception in callback _ProactorBasePipeTransport")   # asyncio shutdown chatter
failed = []
for n in names:
    n = n if n.startswith("test_") else "test_" + n
    t0 = time.time()
    out = ""
    tmp = tempfile.mkdtemp(prefix="btc-test-")     # the test's tempfile.mkdtemp() folders land in here
    try:
        r = subprocess.run([sys.executable, os.path.join(here, n + ".py")], cwd=here, capture_output=True, text=True,
                           timeout=600, env=dict(env, TEMP=tmp, TMP=tmp, TMPDIR=tmp), errors="replace")
        out = r.stdout + ("\n----- stderr -----\n" + r.stderr if r.stderr.strip() else "")
        ok = r.returncode == 0 and "Traceback" not in r.stdout[-400:] + r.stderr[-400:].replace("RuntimeError: boom", "")
        tail = (r.stdout.strip().splitlines() or [""])[-1]
    except subprocess.TimeoutExpired as e:
        ok, tail = False, "timed out"
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("%-14s %s  (%.0fs)  %s" % (n, "ok  " if ok else "FAIL", time.time() - t0, tail[:80]))
    if not ok:
        failed.append(n)
        os.makedirs(logs, exist_ok=True)
        path = os.path.join(logs, n + ".log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        useful = [ln for ln in out.splitlines() if ln.strip() and not any(x in ln for x in NOISE)]
        for ln in useful[-12:]:
            print("    | " + ln)
        print("    full output: " + os.path.relpath(path, here))
print("\nAll passed" if not failed else "\nFAILED: " + ", ".join(failed))
sys.exit(1 if failed else 0)
