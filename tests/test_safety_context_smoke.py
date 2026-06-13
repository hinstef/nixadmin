"""Smoke tests for the safety gate and context cache (no real helper / network)."""

from __future__ import annotations

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
