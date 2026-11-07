"""Write problems to error.log in the app's data folder, so a crash or a silent failure leaves something to look at.

Covers: Python exceptions anywhere (main thread, worker threads, Tk callbacks) and hard crashes of the interpreter
(faulthandler writes the stack of every thread)."""
import faulthandler
import os
import sys
import threading
import time
import traceback

path = None
_file = None


def install(data_dir, version, root=None):
    global path, _file
    path = os.path.join(data_dir, "error.log")
    try:
        if os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".old")
    except OSError:
        pass
    try:
        _file = open(path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return None

    def write(kind, et, ev, tb):
        try:
            _file.write("\n[%s] v%s %s\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), version, kind,
                                               "".join(traceback.format_exception(et, ev, tb))))
        except Exception:
            pass

    try:
        faulthandler.enable(_file, all_threads=True)
    except Exception:
        pass
    def uncaught(et, ev, tb):
        write("uncaught", et, ev, tb)
        if os.environ.get("BTC_TRACEBACKS"):    # set by run_tests.py: a failing test must show why
            traceback.print_exception(et, ev, tb, file=sys.__stderr__)
    sys.excepthook = uncaught
    threading.excepthook = lambda a: write("thread %s" % getattr(a.thread, "name", "?"), a.exc_type, a.exc_value, a.exc_traceback)
    if root is not None:
        root.report_callback_exception = lambda et, ev, tb: write("window callback", et, ev, tb)
    return path
