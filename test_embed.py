"""embed.py logic with a fake Win32 layer (the real calls only exist on Windows)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import tkinter as tk
except ImportError:
    print("embed test: SKIPPED (no tkinter)"); sys.exit(0)
import embed


class FakeWin:
    def __init__(self): self.calls, self.visible_after, self.n = [], 3, 0; self.styles = {}
    def find(self, pid):
        self.n += 1
        return 4242 if self.n >= self.visible_after else None
    def style(self, h): return 0x00CF0000 | 0x10000          # caption + frame + buttons
    def set_style(self, h, v): self.calls.append(("style", v))
    def parent(self, h, p): self.calls.append(("parent", p))
    def move(self, h, x, y, w, ht): self.calls.append(("move", x, y, w, ht))
    def rect(self, h): return (100, 100, 800, 600)
    def alive(self, h): return True
    def keys(self, h, vks): self.calls.append(("keys",) + tuple(vks))


class Proc:
    pid = 1; code = None
    def poll(self): return self.code
    def terminate(self): self.code = 0


try:
    root = tk.Tk()
except tk.TclError:
    print("embed test: SKIPPED (no display)"); sys.exit(0)
host = tk.Frame(root, width=500, height=300); host.pack(fill="both", expand=True); root.update()
w, p, ended, failed = FakeWin(), Proc(), [], []
e = embed.Embedded(root, host, p, on_end=lambda: ended.append(1), on_fail=failed.append, win=w,
                   on_attached=lambda: e.keys(embed.VK_CONTROL, embed.VK_1))
import time
for _ in range(30):
    root.update(); time.sleep(0.1)
kinds = [c[0] for c in w.calls]
assert "parent" in kinds and ("parent", host.winfo_id()) in w.calls, w.calls
st = [c for c in w.calls if c[0] == "style"][0][1]
assert st & embed.WS_CHILD and not st & embed.WS_CAPTION and not st & embed.WS_THICKFRAME, hex(st)
assert any(c[0] == "move" and c[3] > 100 for c in w.calls), "sized to the frame"
assert ("keys", embed.VK_CONTROL, embed.VK_1) in w.calls, "minimal view requested after attaching"
w.calls.clear(); host.configure(width=700); root.update(); time.sleep(0.2); root.update()
assert any(c[0] == "move" for c in w.calls), "follows the frame size"
w.calls.clear(); e.release()
assert ("parent", None) in w.calls and ("style", 0x00CF0000 | 0x10000) in w.calls, w.calls
# process ends -> callback
w2, p2 = FakeWin(), Proc(); e2 = embed.Embedded(root, host, p2, on_end=lambda: ended.append(2), on_fail=failed.append, win=w2)
for _ in range(10): root.update(); time.sleep(0.1)
p2.code = 0
for _ in range(10): root.update(); time.sleep(0.1)
assert 2 in ended, ended
# never shows a window -> fails cleanly
w3, p3 = FakeWin(), Proc(); w3.visible_after = 10 ** 9
e3 = embed.Embedded(root, host, p3, on_fail=failed.append, win=w3, tries=3)
for _ in range(15): root.update(); time.sleep(0.1)
assert failed and "window" in failed[-1], failed
assert embed.launch_args(r"C:\\x\\mpc-hc64.exe", "u")[-1] == "/new"
assert "--qt-minimal-view" in embed.launch_args("vlc.exe", "u")
assert "--wid=77" in embed.launch_args("mpv.exe", "u", 77) and embed.launch_args("foo.exe", "u") == ["foo.exe", "u"]
# attach=False (mpv draws into the frame itself): only the exit is watched
w4, p4 = FakeWin(), Proc(); got = []
e4 = embed.Embedded(root, host, p4, on_end=lambda: got.append(1), win=w4, attach=False)
for _ in range(6): root.update(); time.sleep(0.1)
assert not [c for c in w4.calls if c[0] == "parent"]; p4.code = 0
for _ in range(8): root.update(); time.sleep(0.1)
assert got, "end of an mpv process is noticed"
print("EMBED TEST: ALL PASSED")
