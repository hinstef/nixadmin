"""Smoke tests for the persistent event store.

The store is the observability substrate (and the record autofix will read), so
the contract worth pinning: appends land, ``recent`` returns newest-first, the
unit/kind/since filters narrow correctly, ``NullStore`` is inert, and a fresh
path bootstraps its own schema + parent directory.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from nixadmin.config import Config
from nixadmin.errors import ConfigError
from nixadmin.store import EventStore, NullStore, make_store


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "sub" / "events.db")  # 'sub' must be created for us
    yield s
    s.close()


async def test_append_and_recent_newest_first(store):
    await store.append("failure_observed", unit="a.service", scope="system", text="a failed")
    await store.append("explanation", unit="a.service", scope="system", text="because reasons",
                       meta={"model": "qwen2.5:3b"})
    await store.append("restart", unit="a.service", scope="system", text="healthy again")

    events = await store.recent(10)
    assert [e["kind"] for e in events] == ["restart", "explanation", "failure_observed"]
    # ids assigned, meta decoded back to a dict, ts present
    assert events[1]["meta"] == {"model": "qwen2.5:3b"}
    assert events[0]["meta"] == {}          # no meta → {}
    assert all(isinstance(e["ts"], float) for e in events)
    assert events[0]["id"] > events[2]["id"]


async def test_filters_by_unit_and_kind(store):
    await store.append("failure_observed", unit="a.service", scope="system", text="a")
    await store.append("failure_observed", unit="b.service", scope="system", text="b")
    await store.append("explanation", unit="a.service", scope="system", text="why a")

    by_unit = await store.recent(10, unit="a.service")
    assert {e["unit"] for e in by_unit} == {"a.service"}
    assert len(by_unit) == 2

    by_kind = await store.recent(10, kind="failure_observed")
    assert {e["kind"] for e in by_kind} == {"failure_observed"}
    assert len(by_kind) == 2


async def test_since_filter(store):
    first = await store.append("monitor_event", text="old")
    assert first > 0
    events_all = await store.recent(10)
    cutoff = events_all[0]["ts"]
    await store.append("monitor_event", text="new")
    recent = await store.recent(10, since=cutoff)
    assert [e["text"] for e in recent] == ["new", "old"] or [e["text"] for e in recent] == ["new"]
    # at minimum the newest is present and nothing older than cutoff slips the bound
    assert all(e["ts"] >= cutoff for e in recent)


async def test_limit_is_clamped(store):
    for i in range(5):
        await store.append("monitor_event", text=str(i))
    assert len(await store.recent(2)) == 2
    assert len(await store.recent(0)) == 1     # clamped up to >= 1
    assert len(await store.recent(9999)) == 5  # clamped down, but all 5 fit


async def test_before_id_is_a_stable_pagination_cursor(store):
    for i in range(5):
        await store.append("monitor_event", text=str(i))
    first = await store.recent(2)
    second = await store.recent(2, before_id=first[-1]["id"])
    assert [e["text"] for e in first] == ["4", "3"]
    assert [e["text"] for e in second] == ["2", "1"]
    assert not ({e["id"] for e in first} & {e["id"] for e in second})


async def test_persists_across_reopen(tmp_path):
    path = tmp_path / "events.db"
    s1 = EventStore(path)
    await s1.append("explanation", unit="x.service", text="stored")
    s1.close()

    s2 = EventStore(path)
    events = await s2.recent(10)
    s2.close()
    assert [e["text"] for e in events] == ["stored"]


async def test_prune_removes_only_events_older_than_cutoff(store, monkeypatch):
    monkeypatch.setattr("nixadmin.store.time.time", lambda: 100.0)
    await store.append("monitor_event", text="old")
    monkeypatch.setattr("nixadmin.store.time.time", lambda: 200.0)
    await store.append("monitor_event", text="new")

    assert await store.prune(150.0) == 1
    assert [event["text"] for event in await store.recent()] == ["new"]


async def test_async_close_waits_for_inflight_store_operation(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "events.db")
    started = threading.Event()
    release = threading.Event()

    def blocked_query(*args):
        started.set()
        release.wait(timeout=2)
        return []

    monkeypatch.setattr(store, "_query", blocked_query)
    query = asyncio.create_task(store.recent())
    assert await asyncio.to_thread(started.wait, 1)
    close = asyncio.create_task(store.aclose())
    await asyncio.sleep(0)
    assert not close.done()

    release.set()
    await query
    await close


async def test_null_store_is_inert():
    s = NullStore()
    assert await s.append("explanation", unit="x", text="y") == 0
    assert await s.recent(10) == []
    assert await s.prune(1.0) == 0
    await s.aclose()


def test_make_store_factory(tmp_path):
    assert isinstance(make_store("null", tmp_path), NullStore)
    s = make_store("sqlite", tmp_path)
    assert isinstance(s, EventStore)
    s.close()
    with pytest.raises(ConfigError):
        make_store("bogus", tmp_path)


def test_event_retention_config_is_non_negative():
    assert Config.from_env({}).event_retention_days == 90
    assert Config.from_env({"NIXADMIN_EVENT_RETENTION_DAYS": "0"}).event_retention_days == 0
    with pytest.raises(ConfigError, match="EVENT_RETENTION_DAYS"):
        Config.from_env({"NIXADMIN_EVENT_RETENTION_DAYS": "forever"})
    with pytest.raises(ConfigError, match="EVENT_RETENTION_DAYS"):
        Config.from_env({"NIXADMIN_EVENT_RETENTION_DAYS": "-1"})
