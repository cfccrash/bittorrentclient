"""Keep Bit Torrent Client to one running window; later launches forward their arguments to it."""
import json
import os
import socket
import threading

PORT = 47653
_listener = None     # our listening socket while we are the running instance


def acquire(args, handler, port=PORT):
    """True  -> we are the first instance; handler(list_of_args) is called for later launches.
    False -> another instance is running and has been sent `args`; the caller should exit."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":  # lets a quick restart rebind past TIME_WAIT (on Windows it would allow port stealing)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
        srv.listen(5)
    except OSError:
        srv.close()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as c:
                c.sendall(json.dumps(list(args)).encode() + b"\n")
            return False
        except OSError:
            return True  # something else owns the port; just run normally
    global _listener
    _listener = srv
    threading.Thread(target=_serve, args=(srv, handler), daemon=True).start()
    return True


def release():
    """Give up the instance port (a restart starts the new copy before this process has fully exited)."""
    global _listener
    if _listener is not None:
        try:
            _listener.close()
        except OSError:
            pass
        _listener = None


def _serve(srv, handler):
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.settimeout(2)
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            args = json.loads(buf.decode())
            if isinstance(args, list):
                handler([a for a in args if isinstance(a, str)])
        except (OSError, ValueError):
            pass
        finally:
            conn.close()
