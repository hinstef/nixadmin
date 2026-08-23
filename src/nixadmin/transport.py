"""Shared bounded framing and Hello negotiation for daemon clients."""

from __future__ import annotations

import asyncio
from typing import Protocol

from nixadmin import protocol as wire
from nixadmin.errors import ProtocolError

MAX_LINE_BYTES = 64 * 1024
HANDSHAKE_TIMEOUT_S = 5.0


class LineReader(Protocol):
    def readline(self, size: int = -1) -> str | bytes: ...


def decode_line(raw: str | bytes) -> wire.Message:
    size = len(raw.encode() if isinstance(raw, str) else raw)
    if size > MAX_LINE_BYTES:
        raise ProtocolError("wire message exceeds size limit")
    if isinstance(raw, bytes):
        try:
            raw = raw.decode()
        except UnicodeDecodeError as error:
            raise ProtocolError("wire message is not valid UTF-8") from error
    if not raw.strip():
        raise ProtocolError("daemon disconnected before sending a message")
    return wire.decode(raw.strip())


def negotiate_sync(reader: LineReader) -> wire.Hello:
    hello = decode_line(reader.readline(MAX_LINE_BYTES + 1))
    return _validate_hello(hello)


async def negotiate_async(
    reader: asyncio.StreamReader, deadline_s: float = HANDSHAKE_TIMEOUT_S,
) -> wire.Hello:
    try:
        raw = await asyncio.wait_for(reader.readline(), deadline_s)
    except ValueError as error:
        raise ProtocolError("wire message exceeds size limit") from error
    return _validate_hello(decode_line(raw))


def _validate_hello(message: wire.Message) -> wire.Hello:
    if not isinstance(message, wire.Hello):
        raise ProtocolError("daemon did not send Hello first")
    wire.require_compatible(message)
    return message
