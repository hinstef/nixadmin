"""Daemon timeline socket integration."""

from __future__ import annotations

import asyncio

from nixadmin import protocol as wire
from nixadmin.config import Config
from nixadmin.server import Daemon
from tests.daemon_support import read_until as _read_until


async def test_get_timeline_over_socket(daemon_socket, tmp_path):
    """A client can read the persisted timeline back over the wire."""
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    await daemon.store.append("explanation", unit="a.service", scope="system", text="why")
    server_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.2)
    try:
        reader, writer = await asyncio.open_unix_connection(daemon_socket)
        await _read_until(reader, "hello")
        writer.write(wire.encode(wire.GetTimeline(id="t1")).encode())
        await writer.drain()
        msg = await _read_until(reader, "timeline")
        assert isinstance(msg, wire.Timeline)
        assert [e["kind"] for e in msg.events] == ["explanation"]
        assert msg.events[0]["meta"] == {}
        writer.close()
    finally:
        server_task.cancel()
        await daemon.aclose()
