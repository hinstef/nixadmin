from __future__ import annotations

import asyncio
import io

import pytest

from nixadmin import protocol as wire
from nixadmin.errors import ProtocolError
from nixadmin.transport import MAX_LINE_BYTES, decode_line, negotiate_async, negotiate_sync


def test_sync_negotiation_validates_hello():
    hello = wire.Hello(chains=[], ready={}, default_chain="remote", modules=[])
    assert negotiate_sync(io.StringIO(wire.encode(hello))) == hello
    with pytest.raises(ProtocolError, match="Hello first"):
        negotiate_sync(io.StringIO(wire.encode(wire.Ready(chain="local"))))


async def test_async_negotiation_has_a_deadline():
    reader = asyncio.StreamReader()
    with pytest.raises(TimeoutError):
        await negotiate_async(reader, 0.01)


def test_framing_rejects_oversize_invalid_utf8_and_eof():
    with pytest.raises(ProtocolError, match="size limit"):
        decode_line(b"x" * (MAX_LINE_BYTES + 1))
    with pytest.raises(ProtocolError, match="UTF-8"):
        decode_line(b"\xff\n")
    with pytest.raises(ProtocolError, match="disconnected"):
        decode_line(b"")
