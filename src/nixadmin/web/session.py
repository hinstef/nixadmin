"""A streaming query session for the web hub's invoke bar.

The one-shot :class:`nixadmin.web.dclient.Daemon` roundtrip can't handle an
interactive query: installing an app pauses mid-flight for a ``Confirm`` (show the
diff → proceed?), and the answer arrives on a *different* HTTP request than the one
streaming the reply. So a query needs a daemon connection that stays open across
that pause.

:class:`QuerySession` owns one daemon socket for the life of a single query. The
SSE handler thread drives :meth:`messages` (a generator that streams decoded
daemon messages and, when it hits a ``Confirm``/``Input``, blocks until the browser
answers via a separate ``POST /api/respond`` that calls :meth:`answer`). The
daemon query id is set to the browser-supplied ``qid`` so confirm replies correlate
without extra bookkeeping.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
from collections.abc import Iterator

from nixadmin import protocol as wire
from nixadmin.errors import ProtocolError

CONNECT_TIMEOUT = 5.0
# Long: an install validates in a worktree and may wait on the user's confirm; a
# cold local model can take a while. The read blocks between daemon messages.
READ_TIMEOUT = 600.0
# How long to wait for the browser to answer a Confirm/Input before giving up. A
# bound is essential: if the browser navigates away while a confirm is pending, the
# handler thread is parked on the answer queue (not in a write it could fail), so
# without this it — and the daemon socket — would leak forever.
CONFIRM_WAIT_TIMEOUT = 300.0


class QuerySession:
    """One in-flight query over a dedicated daemon socket."""

    def __init__(self, path: str, qid: str, text: str, session_id: str) -> None:
        self.qid = qid
        self._path = path
        self._text = text
        self._session_id = session_id
        # A queued ``Respond`` is the browser's answer; ``None`` is a cancel signal
        # (abandoned wait / explicit cancel) — distinct so a cancel is never
        # mistaken for a real "decline" of a confirm the user never saw.
        self._answers: queue.Queue[wire.Respond | None] = queue.Queue()
        self._sock: socket.socket | None = None
        self._file: object | None = None
        self._send_lock = threading.Lock()
        self._closed = False

    # ---- lifecycle -------------------------------------------------------- #

    def open(self) -> None:
        """Connect, consume the daemon Hello, and send the Query."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(self._path)
        sock.settimeout(READ_TIMEOUT)
        self._sock = sock
        self._file = sock.makefile("r")
        hello = wire.decode(self._file.readline())
        if not isinstance(hello, wire.Hello):
            self.close()
            raise ProtocolError("daemon did not send Hello first")
        try:
            wire.require_compatible(hello)
        except ProtocolError:
            self.close()
            raise
        self._write(wire.Query(id=self.qid, text=self._text, session=self._session_id))

    def messages(self) -> Iterator[wire.Message]:
        """Yield decoded daemon messages until a terminal one.

        On a ``Confirm``/``Input`` the generator blocks (after yielding, when the
        consumer asks for the next item) until :meth:`answer` supplies the reply,
        which is then written back on the same socket.
        """
        assert self._file is not None
        try:
            for raw in self._file:  # type: ignore[attr-defined]
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = wire.decode(line)
                except ProtocolError:
                    continue
                yield msg
                if isinstance(msg, wire.Confirm | wire.Input):
                    try:
                        resp = self._answers.get(timeout=CONFIRM_WAIT_TIMEOUT)
                    except queue.Empty:
                        resp = None  # nobody answered in time — abandon, don't hang
                    if resp is None:  # cancel / abandoned → tell the daemon and stop
                        self._cancel_daemon()
                        break
                    self._write(resp)
                elif isinstance(msg, wire.Done | wire.Error):
                    break
        finally:
            self.close()

    # ---- cross-thread control (called from the /api/respond, /api/cancel threads) #

    def answer(self, *, confirmed: bool | None = None, value: str | None = None) -> None:
        self._answers.put(wire.Respond(id=self.qid, confirmed=confirmed, value=value))

    def cancel(self) -> None:
        """Ask the daemon to abort, and unblock any pending confirm wait.

        The queued sentinel is ``None`` (a cancel), never a ``Respond`` — so if the
        stream hasn't reached a Confirm yet, a later Confirm is *cancelled*, not
        silently auto-declined with an answer the user never saw."""
        self._cancel_daemon()
        self._answers.put(None)

    def _cancel_daemon(self) -> None:
        with self._send_lock:
            if self._sock is not None and not self._closed:
                try:
                    self._sock.sendall(wire.encode(wire.Cancel(id=self.qid)).encode())
                except OSError:
                    pass

    def close(self) -> None:
        with self._send_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._file is not None:
                    self._file.close()  # type: ignore[attr-defined]
            except OSError:
                pass
            try:
                if self._sock is not None:
                    self._sock.close()
            except OSError:
                pass

    # ---- internals -------------------------------------------------------- #

    def _write(self, msg: wire.Message) -> None:
        with self._send_lock:
            if self._sock is not None and not self._closed:
                self._sock.sendall(wire.encode(msg).encode())


def sse(event: str, payload: dict[str, object]) -> bytes:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
