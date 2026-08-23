"""Smoke test for the daemon — real socket, fake LLM backends.

Proves the end-to-end wiring: connect → hello → query → streamed delta → done,
without needing Ollama or a remote provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nixadmin import protocol as wire
from nixadmin import redact, remediation
from nixadmin.config import Config
from nixadmin.errors import ProtocolError
from nixadmin.server import ClientConn, Daemon, _serve


class FakeConn:
    """Records sent messages and answers confirms with a fixed reply."""

    def __init__(self, confirm_answer: bool) -> None:
        self.sent: list[wire.Message] = []
        self.confirms: list[str] = []
        self._answer = confirm_answer

    async def send(self, msg: wire.Message) -> None:
        self.sent.append(msg)

    async def confirm(self, qid: str, text: str) -> bool:
        self.confirms.append(text)
        return self._answer

    def deltas(self) -> str:
        return "".join(m.text for m in self.sent if isinstance(m, wire.Delta))


async def test_client_send_is_bounded(monkeypatch):
    class StalledWriter:
        def write(self, _payload):
            pass

        async def drain(self):
            await asyncio.Event().wait()

    monkeypatch.setattr("nixadmin.server.CLIENT_SEND_TIMEOUT_S", 0.01)
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
    # No remote credentials → remote_usable False. With no frontier to escalate to,
    # an open-ended change gets an immediate honest limitation — no redaction pass,
    # no prompt to send data to a cloud that doesn't exist.
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
        writer.write(wire.encode(wire.Query(id="q1", text="fix my wifi please")).encode())
        await writer.drain()
        text = ""
        saw_confirm = False
        async with asyncio.timeout(2.0):
            async for raw in reader:
                msg = wire.decode(raw.decode().strip())
                if isinstance(msg, wire.Confirm):
                    saw_confirm = True
                elif isinstance(msg, wire.Delta):
                    text += msg.text
                elif isinstance(msg, wire.Done):
                    break
        assert not saw_confirm  # nothing to escalate to → never prompt to send
        assert "isn't set up on this machine" in text
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


async def _escalation_daemon(daemon_socket, *, remote_ready: bool):
    """A daemon with a local model configured, remote readiness forced, and its
    redaction stubbed so no Ollama is needed."""
    cfg = Config(socket_path=daemon_socket, events="null", local_model="m")
    daemon = Daemon(cfg)
    daemon.remote_ready = remote_ready

    async def fake_redact(text: str) -> redact.Redaction:
        return redact.Redaction(original=text, redacted=f"REDACTED[{text}]", removed=["[email]"])

    daemon._redact_query = fake_redact  # type: ignore[method-assign]
    return daemon


async def test_offer_escalation_shows_redacted_payload_then_declines(daemon_socket):
    daemon = await _escalation_daemon(daemon_socket, remote_ready=True)
    conn = FakeConn(confirm_answer=False)  # user says "keep on device"
    q = wire.Query(id="q1", text="reconfigure my whole firewall")
    await daemon._offer_escalation(conn, q, [], reason="R", local_fallback=False)
    # The confirm shows exactly the redacted payload, and names what was removed.
    assert any("REDACTED[reconfigure my whole firewall]" in c for c in conn.confirms)
    assert any("email removed" in c for c in conn.confirms)
    assert "keeping this on your device" in conn.deltas().lower()
    assert any(isinstance(m, wire.Done) for m in conn.sent)
    await daemon.aclose()


async def test_offer_escalation_accepted_sends_redacted_text(daemon_socket):
    daemon = await _escalation_daemon(daemon_socket, remote_ready=True)
    sent: dict[str, object] = {}

    async def fake_run_remote(conn, query, *, text=None):  # noqa: ANN001
        sent["text"] = text

    daemon._run_remote = fake_run_remote  # type: ignore[method-assign]
    conn = FakeConn(confirm_answer=True)  # user says "send it"
    q = wire.Query(id="q2", text="tell me about photo editors")
    await daemon._offer_escalation(conn, q, [], reason="R", local_fallback=True)
    # What leaves is the redacted payload, never the raw query text.
    assert sent["text"] == "REDACTED[tell me about photo editors]"
    await daemon.aclose()


async def test_offer_escalation_without_remote_never_prompts(daemon_socket):
    # No frontier configured → don't redact or prompt; answer honestly at once.
    daemon = await _escalation_daemon(daemon_socket, remote_ready=False)
    conn = FakeConn(confirm_answer=True)  # would say yes, but must not be asked
    q = wire.Query(id="q3", text="set up a brand new firewall")
    await daemon._offer_escalation(conn, q, [], reason="R", local_fallback=False)
    assert conn.confirms == []  # never prompted to send data nowhere
    assert "isn't set up on this machine" in conn.deltas()
    await daemon.aclose()


async def test_escalated_remote_sends_only_reviewed_query_and_scrubbed_tools(
    daemon_socket, monkeypatch
):
    """On an escalated query, only what the person reviewed leaves the device: the
    redacted query, plus tool results the assistant pulls here (deterministically
    scrubbed). The un-reviewed grounding context and prior turns are NOT sent —
    shipping them (even scrubbed) would break the 'exactly what I'd send' promise
    (bv1)."""
    daemon = await _escalation_daemon(daemon_socket, remote_ready=True)

    class FakeCtx:
        async def assemble(self):
            return "grounding for user@example.com at 10.0.0.5"

    class FakeHist:
        async def recent(self, session, n):
            return [{"role": "user", "content": "my key sk-ABCDEFGHIJKLMNOP1234"}]
        async def append(self, *a, **k):
            return None

    async def fake_call_tool(name, args, conn, query, state):  # noqa: ANN001
        return "journal: token ghp_ABCDEFGHIJKLMNOP1234 at /home/alice/x"

    daemon.context = FakeCtx()  # type: ignore[assignment]
    daemon.history = FakeHist()  # type: ignore[assignment]
    daemon._call_tool = fake_call_tool  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    async def fake_run(sent, *, model, api_base, tools, run_tool, history, system_extra):  # noqa: ANN001
        captured["sent"] = sent
        captured["system_extra"] = system_extra
        captured["history"] = history
        captured["tool"] = await run_tool("some_tool", {})
        if False:  # make this an async generator that yields nothing
            yield ""

    monkeypatch.setattr("nixadmin.server.remote_llm.run", fake_run)
    conn = FakeConn(confirm_answer=True)
    q = wire.Query(id="q1", text="ignored — escalated payload is passed explicitly")
    await daemon._run_remote(conn, q, text="REDACTED[send this]")

    # Only the reviewed (redacted) query goes; no grounding context, no prior turns.
    assert captured["sent"] == "REDACTED[send this]"
    assert captured["system_extra"] == ""
    assert captured["history"] == []
    # Tool output that ran on-device is scrubbed before it crosses the boundary.
    tool = captured["tool"]
    assert "ghp_ABCDEFGHIJKLMNOP1234" not in tool and "[token]" in tool
    assert "/home/alice" not in tool and "/home/[user]" in tool
    await daemon.aclose()


async def test_unescalated_remote_sends_context_and_tools_verbatim(daemon_socket, monkeypatch):
    """The scrub is gated to *escalated* queries. A remote-by-default query (no
    escalation promise) sends real context/tool output so the frontier can help —
    the opt-in-to-cloud case, out of bv1's scope."""
    daemon = await _escalation_daemon(daemon_socket, remote_ready=True)

    class FakeCtx:
        async def assemble(self):
            return "grounding for user@example.com"

    class FakeHist:
        async def recent(self, session, n):
            return []
        async def append(self, *a, **k):
            return None

    async def fake_call_tool(name, args, conn, query, state):  # noqa: ANN001
        return "token ghp_ABCDEFGHIJKLMNOP1234"

    daemon.context = FakeCtx()  # type: ignore[assignment]
    daemon.history = FakeHist()  # type: ignore[assignment]
    daemon._call_tool = fake_call_tool  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    async def fake_run(sent, *, model, api_base, tools, run_tool, history, system_extra):  # noqa: ANN001
        captured["system_extra"] = system_extra
        captured["tool"] = await run_tool("some_tool", {})
        if False:
            yield ""

    monkeypatch.setattr("nixadmin.server.remote_llm.run", fake_run)
    conn = FakeConn(confirm_answer=True)
    # text=None → not escalated
    await daemon._run_remote(conn, wire.Query(id="q1", text="what's up?"))
    assert "user@example.com" in captured["system_extra"]
    assert "ghp_ABCDEFGHIJKLMNOP1234" in captured["tool"]
    await daemon.aclose()


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


async def test_autofix_unit_restarts_and_records_healthy(daemon_socket, tmp_path, monkeypatch):
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    calls: list[tuple[str, str]] = []

    async def fake_restart(unit, scope, *, status, restart_system):  # noqa: ANN001
        calls.append((unit, scope))
        return remediation.RestartOutcome(True, f"Restarted {unit} — healthy again.")

    monkeypatch.setattr("nixadmin.server.remediation.restart_resolved", fake_restart)

    await daemon._autofix_unit("foo.service", "user")
    assert calls == [("foo.service", "user")]
    evs = await daemon.store.recent(10, kind="autofix")
    assert evs[0]["meta"]["action"] == "restart"
    assert evs[0]["meta"]["outcome"] == "healthy"
    await daemon.aclose()


async def test_autofix_loop_guard_informs_without_restarting(daemon_socket, tmp_path, monkeypatch):
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)  # max_attempts defaults to 1
    # One prior restart attempt already on record, within the window.
    await daemon.store.append("autofix", unit="foo.service", scope="user",
                              text="prior", meta={"action": "restart"})
    restarted = False

    async def fake_restart(*a, **k):
        nonlocal restarted
        restarted = True
        return "x"

    monkeypatch.setattr("nixadmin.server.remediation.restart_resolved", fake_restart)
    await daemon._autofix_unit("foo.service", "user")
    assert restarted is False  # budget spent → don't loop
    evs = await daemon.store.recent(10, kind="autofix")
    assert evs[0]["meta"]["action"] == "inform"
    await daemon.aclose()


async def test_run_autofix_once_per_episode_and_rearms_on_recovery(
    daemon_socket, tmp_path, monkeypatch
):
    cfg = Config(socket_path=daemon_socket, events="sqlite", state_dir=str(tmp_path))
    daemon = Daemon(cfg)
    calls: list[str] = []

    async def fake_restart(unit, scope, *, status, restart_system):  # noqa: ANN001
        calls.append(unit)
        return remediation.RestartOutcome(False, "restarted, still failing")

    failing = [{"unit": "foo.service", "scope": "user", "description": "x"}]

    async def fake_failed():
        return failing

    monkeypatch.setattr("nixadmin.server.remediation.restart_resolved", fake_restart)
    monkeypatch.setattr("nixadmin.server.remediation.failed_units", fake_failed)

    await daemon._run_autofix()   # foo is new → act once
    await daemon._run_autofix()   # same episode → no second action
    assert calls == ["foo.service"]
    assert ("foo.service", "user") in daemon._autofix_seen

    failing.clear()               # unit recovered
    await daemon._run_autofix()
    assert ("foo.service", "user") not in daemon._autofix_seen  # episode forgotten
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
