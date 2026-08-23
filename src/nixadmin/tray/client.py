"""Async client for the nixadmin daemon socket — the tray's view of the system.

Speaks only :mod:`nixadmin.protocol`. Survives daemon restarts (a config switch
restarts the daemon): the run loop reconnects, and the tray simply shows grey
"unreachable" in the gap. Requests are correlated by id via one-shot futures.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from nixadmin import protocol as wire
from nixadmin.errors import ProtocolError
from nixadmin.log import get_logger
from nixadmin.transport import HANDSHAKE_TIMEOUT_S, negotiate_async

log = get_logger(__name__)

RECONNECT_DELAY_S = 3.0
REQUEST_TIMEOUT_S = 10.0
EXPLAIN_TIMEOUT_S = 90.0  # the local model may cold-start (~6s) or be slow to stream


def socket_path() -> str:
    """Same resolution the terminal client uses (``NIXADMIN_SOCKET`` overrides)."""
    env = os.environ.get("NIXADMIN_SOCKET")
    if env:
        return env
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")  # noqa: S108
    return str(Path(runtime) / "nixadmin.sock")


class DaemonClient:
    """Maintains a connection to the daemon and exposes the two calls the tray
    needs: :meth:`list_failures` (poll) and :meth:`run_action` (a fix-it)."""

    def __init__(
        self,
        path: str,
        *,
        on_state: Callable[[bool], None] | None = None,
        on_event: Callable[[wire.Event], None] | None = None,
    ) -> None:
        self.path = path
        self.on_state = on_state or (lambda _connected: None)
        self.on_event = on_event or (lambda _ev: None)
        self.connected = False
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[wire.Message]] = {}
        self._collect: dict[str, list[str]] = {}  # accumulates Delta text per request

    async def run(self) -> None:
        """Connect-read-reconnect forever. Launch as a background task."""
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.path)
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                self._set_connected(False)
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue
            try:
                await negotiate_async(reader, HANDSHAKE_TIMEOUT_S)
            except (TimeoutError, ProtocolError, OSError) as error:
                log.warning("daemon handshake failed", error=str(error))
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                self._set_connected(False)
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue

            self._writer = writer
            self._set_connected(True)
            try:
                async for raw in reader:
                    line = raw.decode(errors="replace").strip()
                    if line:
                        self._dispatch(line)
            except (ConnectionResetError, OSError):
                pass
            finally:
                self._set_connected(False)
                self._fail_pending()
                writer.close()
                self._writer = None
            await asyncio.sleep(RECONNECT_DELAY_S)

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            self.on_state(value)

    def _dispatch(self, line: str) -> None:
        try:
            msg = wire.decode(line)
        except Exception:  # noqa: BLE001 — ignore junk, keep the loop alive
            return
        if isinstance(msg, wire.Event):
            self.on_event(msg)
        elif isinstance(msg, wire.Confirm):
            # A fix-it action asked to confirm; the click *was* the confirmation.
            self._send(wire.Respond(id=msg.id, confirmed=True))
        elif isinstance(msg, wire.Delta):
            buf = self._collect.get(msg.id)
            if buf is not None:
                buf.append(msg.text)
        elif isinstance(msg, (wire.Failures, wire.Done, wire.Error)):
            fut = self._pending.pop(msg.id, None)
            if fut and not fut.done():
                fut.set_result(msg)

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("daemon disconnected"))
        self._pending.clear()

    def _send(self, msg: wire.Message) -> bool:
        if self._writer is None:
            return False
        try:
            self._writer.write(wire.encode(msg).encode())
        except OSError:
            return False
        return True

    async def _request(
        self, msg: wire.Message, req_id: str, *,
        collect: bool = False, timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> wire.Message | None:
        if not self.connected:
            return None
        fut: asyncio.Future[wire.Message] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        if collect:
            self._collect[req_id] = []
        if not self._send(msg):
            self._pending.pop(req_id, None)
            self._collect.pop(req_id, None)
            return None
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except (TimeoutError, ConnectionError):
            self._pending.pop(req_id, None)
            return None

    async def list_failures(self) -> list[dict[str, str]] | None:
        """Current failed units, or ``None`` if the daemon didn't answer."""
        req_id = uuid.uuid4().hex[:8]
        reply = await self._request(wire.ListFailures(id=req_id), req_id)
        return reply.units if isinstance(reply, wire.Failures) else None

    async def restart_unit(self, unit: str, scope: str) -> str | None:
        """Ask the daemon to restart a specific failed unit (a fix-it click) and
        wait for it to finish. Returns an error string on failure, else ``None``.

        This is the deterministic path: the exact unit and scope come from a prior
        :class:`~nixadmin.protocol.Failures`, so no natural-language matching runs."""
        req_id = uuid.uuid4().hex[:8]
        reply = await self._request(wire.RestartUnit(id=req_id, unit=unit, scope=scope), req_id)
        if isinstance(reply, wire.Error):
            return reply.text
        return None

    async def explain_unit(self, unit: str, scope: str) -> str | None:
        """Ask the daemon for a plain-words explanation of why a unit failed and
        collect the streamed answer. The local model may warm up first, so this
        allows a long wait. Returns the text, or ``None`` if it couldn't answer."""
        req_id = uuid.uuid4().hex[:8]
        reply = await self._request(
            wire.ExplainUnit(id=req_id, unit=unit, scope=scope),
            req_id, collect=True, timeout_s=EXPLAIN_TIMEOUT_S,
        )
        text = "".join(self._collect.pop(req_id, []))
        if isinstance(reply, wire.Error):
            return reply.text
        return text or None
