"""Run every test script and print a summary.   python run_tests.py [name ...]   (names without .py / test_ prefix)

Tests that need Tk run against a real display; on Linux start one with `Xvfb :99 &` and `export DISPLAY=:99`.
Set BTC_CHROME=1 to exercise the custom window frames (on by default only on Windows); test_chrome sets it itself."""
import glob
import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
names = sys.argv[1:] or sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(here, "test_*.py")))
failed = []
for n in names:
    n = n if n.startswith("test_") else "test_" + n
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, os.path.join(here, n + ".py")], cwd=here, capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0 and "Traceback" not in r.stdout[-400:] + r.stderr[-400:].replace("RuntimeError: boom", "")
        tail = (r.stdout.strip().splitlines() or [""])[-1]
    except subprocess.TimeoutExpired:
        ok, tail = False, "timed out"
    print("%-14s %s  (%.0fs)  %s" % (n, "ok  " if ok else "FAIL", time.time() - t0, tail[:80]))
    if not ok:
        failed.append(n)
print("\nAll passed" if not failed else "\nFAILED: " + ", ".join(failed))
sys.exit(1 if failed else 0)
