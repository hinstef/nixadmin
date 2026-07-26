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
                 socket_path=daemon_socket, events="null")
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
    cfg = Config(remote_model="fake", default_chain="remote", socket_path=daemon_socket,
                 events="null")
    daemon = Daemon(cfg)
    assert daemon.remote_ready is False  # no key/base → not usable
    server_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.2)

    try:
        reader, writer = await asyncio.open_unix_connection(daemon_socket)
        await _read_until(reader, "hello")
        # An open-ended change (not a known app action) → needs the full assistant.
        writer.write(wire.encode(wire.Query(id="q1", text="fix my wifi please")).encode())
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


async def test_failure_transitions_recorded(daemon_socket, tmp_path):
    """Failed units appearing / clearing between polls land on the timeline as
    failure_observed / failure_cleared (once each, not per poll)."""
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    try:
        a = {"unit": "a.service", "scope": "system", "description": "A"}
        await daemon._record_failure_transitions([a])
        await daemon._record_failure_transitions([a])          # still failing → no dup
        await daemon._record_failure_transitions([])           # cleared

        events = await daemon.store.recent(10)
        kinds = [(e["kind"], e["unit"]) for e in events]
        assert kinds == [("failure_cleared", "a.service"), ("failure_observed", "a.service")]
    finally:
        await daemon.aclose()


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
