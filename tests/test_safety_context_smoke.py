"""Smoke tests for the safety gate and context cache (no real helper / network)."""

from __future__ import annotations

import asyncio
import json

import pytest

from nixadmin.context import ContextCache
from nixadmin.errors import SafetyError
from nixadmin.safety import SafetyGate
from nixadmin.sdk import ContextProvider
from nixadmin.session import SessionState


async def _yes(_msg: str) -> bool:
    return True


async def _no(_msg: str) -> bool:
    return False


async def test_switch_refused_without_prior_test():
    gate = SafetyGate("/nonexistent.sock")
    out = await gate.rebuild("switch", state=SessionState(), confirm=_yes)
    assert "test" in out.lower()  # refused, never reached the helper


async def test_switch_cancelled_when_user_declines():
    state = SessionState()
    state.record_test(True)  # prerequisite satisfied
    gate = SafetyGate("/nonexistent.sock")
    out = await gate.rebuild("switch", state=state, confirm=_no)
    assert "cancelled" in out.lower()


async def test_unknown_action_raises():
    gate = SafetyGate("/nonexistent.sock")
    with pytest.raises(SafetyError):
        await gate.rebuild("nuke", state=SessionState(), confirm=_yes)


# --- gate protocol against a fake helper socket (real _run_helper path) ----- #


class FakeHelper:
    """Stand-in for the root nixadmin-helper: speaks the same newline-JSON
    protocol and returns a configurable ``(stream, exit_code)``."""

    def __init__(self, sock_path: str, *, output: str = "", exit_code: int = 0) -> None:
        self.sock_path = sock_path
        self.output = output
        self.exit_code = exit_code
        self.requests: list[dict[str, str]] = []
        self._server: asyncio.AbstractServer | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        self.requests.append(json.loads(raw.decode()))
        if self.output:
            writer.write((json.dumps({"stream": self.output}) + "\n").encode())
        writer.write((json.dumps({"exit": self.exit_code}) + "\n").encode())
        await writer.drain()
        writer.close()

    async def __aenter__(self) -> FakeHelper:
        self._server = await asyncio.start_unix_server(self._handle, path=self.sock_path)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
def sock(tmp_path) -> str:
    return str(tmp_path / "helper.sock")


async def test_successful_test_enables_switch(sock):
    state = SessionState()
    async with FakeHelper(sock, output="building...\nok", exit_code=0):
        await SafetyGate(sock).rebuild("test", state=state, confirm=_yes)
    assert state.last_test_ok is True


async def test_failed_test_blocks_switch_even_without_the_word_failed(sock):
    """The regression #1 fixes: a nonzero test whose output never says 'failed'
    must still be recorded as a failure and block a subsequent switch."""
    state = SessionState()
    async with FakeHelper(sock, output="error: build broke", exit_code=1):
        out = await SafetyGate(sock).rebuild("test", state=state, confirm=_yes)
    assert state.last_test_ok is False
    assert "exit 1" in out
    # switch is refused before reaching the helper, because the test failed
    refused = await SafetyGate(sock).rebuild("switch", state=state, confirm=_yes)
    assert "test" in refused.lower()


async def test_passing_test_with_word_failed_in_output_still_passes(sock):
    """Inverse: exit 0 with '0 failed' in the output must NOT be misread as a
    failure (the old substring heuristic got this exactly wrong)."""
    state = SessionState()
    async with FakeHelper(sock, output="0 packages failed", exit_code=0):
        await SafetyGate(sock).rebuild("test", state=state, confirm=_yes)
    assert state.last_test_ok is True


async def test_apply_switch_raises_on_nonzero(sock):
    async with FakeHelper(sock, output="boom", exit_code=2):
        with pytest.raises(SafetyError, match="exit 2"):
            await SafetyGate(sock).apply_switch()


async def test_apply_switch_returns_output_on_success(sock):
    async with FakeHelper(sock, output="activated", exit_code=0):
        out = await SafetyGate(sock).apply_switch()
    assert "activated" in out


async def test_apply_restart_sends_action_and_unit(sock):
    async with FakeHelper(sock, output="", exit_code=0) as h:
        out = await SafetyGate(sock).apply_restart("bluetooth.service")
    assert h.requests[0] == {"action": "restart", "unit": "bluetooth.service"}
    assert out  # non-empty result on success


async def test_apply_restart_raises_on_failure(sock):
    async with FakeHelper(sock, output="nope", exit_code=1):
        with pytest.raises(SafetyError, match="restart"):
            await SafetyGate(sock).apply_restart("bluetooth.service")


async def test_context_cache_assembles_and_caches():
    calls = {"n": 0}

    async def get() -> str:
        calls["n"] += 1
        return "machine profile text"

    cache = ContextCache([ContextProvider(name="p", get=get)])  # no refresh → cache forever
    first = await cache.assemble()
    second = await cache.assemble()
    assert "machine profile text" in first == second
    assert calls["n"] == 1  # second call served from cache


async def test_context_cache_survives_provider_error():
    async def boom() -> str:
        raise RuntimeError("nope")

    cache = ContextCache([ContextProvider(name="p", get=boom)])
    assert await cache.assemble() == ""  # error swallowed, empty context
