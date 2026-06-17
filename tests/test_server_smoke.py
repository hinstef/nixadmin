"""Smoke test for the daemon — real socket, fake LLM backends.

Proves the end-to-end wiring: connect → hello → query → streamed delta → done,
without needing Ollama or a remote provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nixadmin import protocol as wire
from nixadmin.config import Config
from nixadmin.server import Daemon


@pytest.fixture
def daemon_socket(tmp_path):
    return str(tmp_path / "nixadmin.sock")


async def _read_until(reader: asyncio.StreamReader, type_: str, wait=2.0) -> wire.Message:
    """Read messages until one of the given wire type arrives."""
    async with asyncio.timeout(wait):
        async for raw in reader:
            line = raw.decode().strip()
            if not line:
                continue
            msg = wire.decode(line)
            if msg.TYPE == type_:
                return msg
    raise AssertionError(f"never saw {type_}")


async def test_remote_query_round_trip(daemon_socket, monkeypatch):
    # Fake the remote chain so no provider is needed.
    async def fake_run(query, **kwargs) -> AsyncIterator[str]:
        for chunk in ("Yes, ", "all good."):
            yield chunk

    monkeypatch.setattr("nixadmin.server.remote_llm.run", fake_run)

    # remote_base set → remote_usable is True without needing an API key env.
    cfg = Config(remote_model="fake-model", remote_base="http://fake", default_chain="remote",
                 socket_path=daemon_socket)
    daemon = Daemon(cfg)
    server_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.2)  # let it bind

    try:
        reader, writer = await asyncio.open_unix_connection(daemon_socket)

        hello = await _read_until(reader, "hello")
        assert "remote" in hello.chains

        writer.write(wire.encode(wire.Query(id="q1", text="is everything ok?")).encode())
        await writer.drain()

        # collect deltas until done
        text = ""
        async with asyncio.timeout(2.0):
            async for raw in reader:
                msg = wire.decode(raw.decode().strip())
                if isinstance(msg, wire.Delta):
                    text += msg.text
                elif isinstance(msg, wire.Done):
                    assert msg.chain == "remote"
                    break
        assert text == "Yes, all good."
        writer.close()
    finally:
        server_task.cancel()
        await daemon.aclose()


async def test_mutation_without_remote_says_it_cannot(daemon_socket, monkeypatch):
    # No remote credentials → remote_usable False → writes get a plain-language
    # limitation (a Delta), not an auth error.
    for k in Config._REMOTE_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = Config(remote_model="fake", default_chain="remote", socket_path=daemon_socket)
    daemon = Daemon(cfg)
    assert daemon.remote_ready is False  # no key/base → not usable
    server_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.2)

    try:
        reader, writer = await asyncio.open_unix_connection(daemon_socket)
        await _read_until(reader, "hello")
        writer.write(wire.encode(wire.Query(id="q1", text="install firefox")).encode())
        await writer.drain()
        text = ""
        async with asyncio.timeout(2.0):
            async for raw in reader:
                msg = wire.decode(raw.decode().strip())
                if isinstance(msg, wire.Delta):
                    text += msg.text
                elif isinstance(msg, wire.Done):
                    break
        assert "can't make changes" in text.lower()
        writer.close()
    finally:
        server_task.cancel()
        await daemon.aclose()
