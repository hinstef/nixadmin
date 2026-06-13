"""Smoke tests for history, session, and prefetch."""

from __future__ import annotations

import pytest

from nixadmin.history import NullHistory, make_history
from nixadmin.prefetch import prefetch
from nixadmin.sdk import SPEC_VERSION, Fetcher, Module
from nixadmin.session import SessionRegistry


async def test_null_history_is_empty():
    h = NullHistory()
    await h.append("s1", "user", "hi")
    assert await h.recent("s1", 10) == []


def test_make_history_unknown_raises():
    from nixadmin.errors import ConfigError

    with pytest.raises(ConfigError):
        make_history("redis")


def test_session_state_is_stable_and_records_test():
    reg = SessionRegistry()
    st = reg.state("s1")
    assert st.last_test_ok is False
    st.record_test(True)
    assert reg.state("s1").last_test_ok is True  # same object returned
    assert reg.lock("s1") is reg.lock("s1")  # stable lock per session


async def test_prefetch_runs_fetchers_in_parallel():
    mod = Module(
        spec_version=SPEC_VERSION, name="t", description="t",
        fetchers=[
            Fetcher(name="a", cmd="echo hello"),
            Fetcher(name="b", cmd="echo world"),
        ],
    )
    out = await prefetch([mod])
    assert "hello" in out and "world" in out
    assert "$ echo hello" in out


async def test_prefetch_empty_when_no_fetchers():
    mod = Module(spec_version=SPEC_VERSION, name="t", description="t")
    assert await prefetch([mod]) == ""


async def test_prefetch_captures_failures_as_data():
    mod = Module(
        spec_version=SPEC_VERSION, name="t", description="t",
        fetchers=[Fetcher(name="x", cmd="exit 3")],
    )
    out = await prefetch([mod])
    assert "exit 3" in out  # failure surfaced, not raised
