"""A network link a live test can cut, for a pendant that goes off Wi-Fi.

``SwitchableLink`` is a plain TCP relay in front of the local stack. A client
pointed at ``link.url`` reaches Supabase through it, and ``link.cut()`` drops
every open connection and resets each new one, the way a pendant out of
Wi-Fi range sees the network. ``link.restore()`` brings it back. Nothing
above TCP is touched, so every request (auth, storage, PostgREST) fails or
succeeds exactly as it would on real hardware.
"""

import contextlib
import socket
import struct
import threading
from types import TracebackType
from urllib.parse import urlsplit


class SwitchableLink:
    def __init__(self, upstream_url: str) -> None:
        parts = urlsplit(upstream_url)
        assert parts.scheme == "http" and parts.hostname and parts.port
        self._upstream = (parts.hostname, parts.port)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self._lock = threading.Lock()
        self._up = True
        self._open: set[socket.socket] = set()
        self._closed = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def cut(self) -> None:
        with self._lock:
            self._up = False
            for sock in self._open:
                _reset(sock)
            self._open.clear()

    def is_up(self) -> bool:
        with self._lock:
            return self._up

    def restore(self) -> None:
        with self._lock:
            self._up = True

    def close(self) -> None:
        self.cut()
        self._closed = True
        self._listener.close()

    def __enter__(self) -> "SwitchableLink":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if not self.is_up():
                _reset(client)
                continue
            try:
                upstream = socket.create_connection(self._upstream)
            except OSError:
                _reset(client)
                continue
            with self._lock:
                if not self._up:
                    _reset(client)
                    _reset(upstream)
                    continue
                self._open.update((client, upstream))
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(
                    target=self._pump, args=(src, dst), daemon=True
                ).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            with self._lock:
                self._open.discard(src)
                self._open.discard(dst)
            for sock in (src, dst):
                with contextlib.suppress(OSError):
                    sock.close()


def _reset(sock: socket.socket) -> None:
    """Close with a TCP RST, as a dropped link looks to the other end."""
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
