"""Daemon kept-well ledger integration."""

from __future__ import annotations

import asyncio

from nixadmin import protocol as wire
from nixadmin.config import Config
from nixadmin.server import Daemon
from tests.daemon_support import read_until as _read_until


async def test_get_ledger_over_socket(daemon_socket, tmp_path, monkeypatch):
    """A client reads the kept-well ledger back over the wire; a silent self-heal
    on record shows up in the quiet tally, and nothing failing → healthy."""
    async def no_failures():
        return []
    monkeypatch.setattr("nixadmin.server.remediation.failed_units", no_failures)

    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    await daemon.store.append("autofix", unit="a.service", scope="user",
                              text="restarted a — healthy again",
                              meta={"action": "restart", "outcome": "healthy"})
    server_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.2)
    try:
        reader, writer = await asyncio.open_unix_connection(daemon_socket)
        await _read_until(reader, "hello")
        writer.write(wire.encode(wire.GetLedger(id="l1")).encode())
        await writer.drain()
        msg = await _read_until(reader, "ledger")
        assert isinstance(msg, wire.Ledger)
        assert msg.data["healthy_now"] is True
        assert "quietly restarted 1 service" in msg.data["tally"]
        writer.close()
    finally:
        server_task.cancel()
        await daemon.aclose()

