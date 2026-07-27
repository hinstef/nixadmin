"""Smoke test for the daemon — real socket, fake LLM backends.

Proves the end-to-end wiring: connect → hello → query → streamed delta → done,
without needing Ollama or a remote provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nixadmin import protocol as wire
from nixadmin import redact
from nixadmin.config import Config
from nixadmin.server import Daemon


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
