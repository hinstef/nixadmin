"""Daemon connection, lifecycle, and health behavior."""

from __future__ import annotations

import asyncio

import pytest

from nixadmin import protocol as wire
from nixadmin.config import Config
from nixadmin.errors import ProtocolError
from nixadmin.server import ClientConn, Daemon, _serve
from tests.daemon_support import FakeConn
from tests.daemon_support import read_until as _read_until


async def test_client_send_is_bounded(monkeypatch):
    class StalledWriter:
        def write(self, _payload):
            pass

        async def drain(self):
            await asyncio.Event().wait()

    monkeypatch.setattr("nixadmin.connections.CLIENT_SEND_TIMEOUT_S", 0.01)
    conn = ClientConn(asyncio.StreamReader(), StalledWriter())  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        await conn.send(wire.Ready(chain="local"))
    with pytest.raises(ProtocolError, match="wire limit"):
        await conn.send(wire.Delta(id="x", text="x" * 70_000))


async def test_broadcast_does_not_serialize_clients(daemon_socket):
    daemon = Daemon(Config(socket_path=daemon_socket, events="null"))
    slow_release = asyncio.Event()
    fast_received = asyncio.Event()

    class SlowConn:
        async def send(self, _msg):
            await slow_release.wait()

    class FastConn:
        async def send(self, _msg):
            fast_received.set()

    daemon.conns.update((SlowConn(), FastConn()))  # type: ignore[arg-type]
    broadcast = asyncio.create_task(daemon._send_all(wire.Ready(chain="local")))
    try:
        async with asyncio.timeout(0.2):
            await fast_received.wait()
    finally:
        slow_release.set()
        await broadcast
        await daemon.aclose()


async def test_daemon_cancels_owned_background_tasks(daemon_socket):
    daemon = Daemon(Config(socket_path=daemon_socket, events="null"))
    started = asyncio.Event()

    async def waits_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = daemon._spawn(waits_forever())
    await started.wait()
    assert task in daemon._background_tasks

    await daemon.aclose()

    assert task.cancelled()
    assert not daemon._background_tasks


async def test_worktree_cleanup_failure_does_not_block_readiness(
    daemon_socket, tmp_path, monkeypatch,
):
    async def cleanup_fails(_flake_dir):
        raise RuntimeError("git unavailable")

    notifications: list[str] = []
    ready = asyncio.Event()

    def record_notification(message: str) -> None:
        notifications.append(message)
        if message.startswith("READY=1"):
            ready.set()

    monkeypatch.setattr("nixadmin.server.actions.prune_abandoned_worktrees", cleanup_fails)
    monkeypatch.setattr("nixadmin.server.notify", record_notification)
    daemon = Daemon(Config(
        socket_path=daemon_socket, events="null", flake_dir=str(tmp_path), autofix=False,
    ))
    task = asyncio.create_task(daemon.run())
    try:
        async with asyncio.timeout(1):
            await ready.wait()
        assert task.done() is False
    finally:
        task.cancel()
        await daemon.aclose()


async def test_serve_always_closes_daemon():
    closed = False

    class FakeDaemon:
        async def run(self) -> None:
            raise RuntimeError("stop")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    with pytest.raises(RuntimeError, match="stop"):
        await _serve(FakeDaemon())  # type: ignore[arg-type]
    assert closed


async def test_background_task_failure_is_observed(daemon_socket, monkeypatch):
    daemon = Daemon(Config(socket_path=daemon_socket, events="null"))
    errors: list[str] = []
    monkeypatch.setattr("nixadmin.tasks.log.error", lambda message, **kw: errors.append(message))

    async def fails() -> None:
        raise RuntimeError("broken task")

    task = daemon._spawn(fails())
    with pytest.raises(RuntimeError, match="broken task"):
        await task
    await asyncio.sleep(0)
    assert errors == ["owned task failed"]
    await daemon.aclose()


async def test_disconnect_cancels_non_query_request(daemon_socket, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_failures():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("nixadmin.server.remediation.failed_units", slow_failures)
    daemon = Daemon(Config(socket_path=daemon_socket, events="null"))
    server_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    try:
        reader, writer = await asyncio.open_unix_connection(daemon_socket)
        await _read_until(reader, "hello")
        writer.write(wire.encode(wire.ListFailures(id="list-1")).encode())
        await writer.drain()
        await started.wait()
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(1.0):
            await cancelled.wait()
    finally:
        server_task.cancel()
        await daemon.aclose()


async def test_invalid_utf8_closes_client_cleanly(daemon_socket):
    daemon = Daemon(Config(socket_path=daemon_socket, events="null"))
    server_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    try:
        reader, writer = await asyncio.open_unix_connection(daemon_socket)
        await _read_until(reader, "hello")
        writer.write(b"\xff\n")
        await writer.drain()
        async with asyncio.timeout(1.0):
            assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    finally:
        server_task.cancel()
        await daemon.aclose()


async def test_health_reports_lifecycle_and_malformed_counter(daemon_socket):
    daemon = Daemon(Config(socket_path=daemon_socket, events="null"))
    conn = FakeConn(confirm_answer=False)
    await daemon._on_message(conn, "not json")  # type: ignore[arg-type]
    await daemon._get_health(conn, wire.GetHealth(id="health-1"))  # type: ignore[arg-type]

    health = next(msg for msg in conn.sent if isinstance(msg, wire.Health))
    assert health.data["store"] == {"backend": "NullStore", "enabled": False}
    assert health.data["counters"] == {"malformed_messages": 1}
    assert isinstance(health.data["uptime_s"], float)
    assert health.data["ready"] == {"local": False, "remote": daemon.remote_ready}
    await daemon.aclose()

