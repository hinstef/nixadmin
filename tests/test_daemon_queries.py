"""Daemon query routing, remote execution, and escalation behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from nixadmin import protocol as wire
from nixadmin import redact
from nixadmin.config import Config
from nixadmin.server import Daemon
from tests.daemon_support import FakeConn
from tests.daemon_support import read_until as _read_until


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

