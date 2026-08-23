"""Synchronous daemon client for the (threaded) web server.

One short-lived socket per call keeps the HTTP handlers simple and thread-safe —
no shared connection, no async loop. Speaks only :mod:`nixadmin.protocol`.
"""

from __future__ import annotations

import json
import socket
import uuid

from nixadmin import protocol as wire
from nixadmin.errors import ProtocolError
from nixadmin.transport import negotiate_sync

DEFAULT_TIMEOUT = 30.0
EXPLAIN_TIMEOUT = 90.0  # local model may cold-start


def _id() -> str:
    return uuid.uuid4().hex[:8]


class Daemon:
    """Blocking request/response against the daemon's Unix socket."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _roundtrip(
        self, req: dict[str, object], *, terminal: tuple[type, ...],
        collect: bool = False, timeout: float = DEFAULT_TIMEOUT,
    ) -> tuple[wire.Message | None, str]:
        deltas: list[str] = []
        result: wire.Message | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self.path)
            with sock, sock.makefile("r") as f:
                negotiate_sync(f)
                sock.sendall((json.dumps(req) + "\n").encode())
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = wire.decode(line)
                    except ProtocolError:
                        continue
                    if collect and isinstance(msg, wire.Delta):
                        deltas.append(msg.text)
                    if isinstance(msg, terminal):
                        result = msg
                        break
        except (OSError, ProtocolError):
            return None, ""
        return result, "".join(deltas)

    def list_failures(self) -> list[dict[str, str]] | None:
        """Failed units, or ``None`` if the daemon is unreachable."""
        msg, _ = self._roundtrip(
            {"type": "list_failures", "id": _id()}, terminal=(wire.Failures,))
        return msg.units if isinstance(msg, wire.Failures) else None

    def journal(self, unit: str, scope: str) -> str | None:
        msg, _ = self._roundtrip(
            {"type": "unit_journal", "id": _id(), "unit": unit, "scope": scope},
            terminal=(wire.Journal,))
        return msg.text if isinstance(msg, wire.Journal) else None

    def timeline(
        self, limit: int = 10, unit: str | None = None, before_id: int | None = None,
    ) -> tuple[list[dict[str, object]], int | None]:
        """One stable timeline page (newest first) and its older-page cursor."""
        req: dict[str, object] = {"type": "get_timeline", "id": _id(), "limit": limit}
        if unit:
            req["unit"] = unit
        if before_id is not None:
            req["before_id"] = before_id
        msg, _ = self._roundtrip(req, terminal=(wire.Timeline,))
        if isinstance(msg, wire.Timeline):
            return msg.events, msg.next_cursor
        return [], None

    def ledger(self) -> dict[str, object] | None:
        """The kept-well ledger summary, or ``None`` if the daemon is unreachable."""
        msg, _ = self._roundtrip(
            {"type": "get_ledger", "id": _id()}, terminal=(wire.Ledger,))
        return msg.data if isinstance(msg, wire.Ledger) else None

    def restart(self, unit: str, scope: str) -> tuple[str, bool]:
        msg, deltas = self._roundtrip(
            {"type": "restart_unit", "id": _id(), "unit": unit, "scope": scope},
            terminal=(wire.Done, wire.Error), collect=True)
        if isinstance(msg, wire.Error):
            return msg.text, False
        if isinstance(msg, wire.Done):
            return deltas or "done", True
        return "daemon unreachable", False

    def explain(self, unit: str, scope: str) -> tuple[str, bool]:
        msg, deltas = self._roundtrip(
            {"type": "explain_unit", "id": _id(), "unit": unit, "scope": scope},
            terminal=(wire.Done, wire.Error), collect=True, timeout=EXPLAIN_TIMEOUT)
        if isinstance(msg, wire.Error):
            return msg.text, False
        if isinstance(msg, wire.Done):
            return deltas or "(no explanation available)", True
        return "daemon unreachable", False
