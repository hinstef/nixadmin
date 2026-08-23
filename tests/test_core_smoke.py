"""Smoke tests for history, session, and prefetch."""

from __future__ import annotations

import pytest

from nixadmin.history import NullHistory, make_history
from nixadmin.prefetch import MAX_FETCHER_OUTPUT_CHARS, MAX_PREFETCH_CHARS, prefetch
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
    assert "## t/a" in out
    assert "echo hello" not in out  # raw commands are implementation detail, not context


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


async def test_prefetch_hides_multiline_command_and_bounds_output(monkeypatch):
    command = "printf 'secret implementation detail\\nsecond line'"
    mod = Module(
        spec_version=SPEC_VERSION, name="system", description="system",
        fetchers=[Fetcher(name="large", cmd=command, description="System summary")],
    )

    async def huge(_fetcher):
        return "\x1b[31m" + "x" * (MAX_FETCHER_OUTPUT_CHARS + 100) + "\x00"

    monkeypatch.setattr("nixadmin.prefetch._run", huge)
    out = await prefetch([mod])
    assert command not in out and "second line" not in out
    assert "## system/large — System summary" in out
    assert "output truncated" in out
    assert "\x1b" not in out and "\x00" not in out


async def test_prefetch_has_a_total_context_bound(monkeypatch):
    modules = [
        Module(
            spec_version=SPEC_VERSION, name=f"m{i}", description="test",
            fetchers=[Fetcher(name="data", cmd="ignored")],
        )
        for i in range(4)
    ]

    async def large(_fetcher):
        return "y" * MAX_FETCHER_OUTPUT_CHARS

    monkeypatch.setattr("nixadmin.prefetch._run", large)
    out = await prefetch(modules)
    assert out.startswith("## m0/data")
    assert "output truncated: prefetch context" in out
    assert len(out) <= MAX_PREFETCH_CHARS
