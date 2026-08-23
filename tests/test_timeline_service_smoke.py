from __future__ import annotations

from nixadmin.store import EventStore
from nixadmin.timeline import TimelineService


async def test_timeline_service_records_transitions_and_pages(tmp_path):
    store = EventStore(tmp_path / "events.db")

    async def no_failures():
        return []

    service = TimelineService(store, no_failures)
    unit = {"unit": "a.service", "scope": "system", "description": "A"}
    await service.record_failure_transitions([unit])
    await service.record_failure_transitions([unit])
    await service.record_failure_transitions([])
    events, cursor = await service.page(10)
    await store.aclose()
    assert [event["kind"] for event in events] == ["failure_cleared", "failure_observed"]
    assert cursor is None


async def test_timeline_page_has_stable_older_cursor(tmp_path):
    store = EventStore(tmp_path / "events.db")

    async def no_failures():
        return []

    service = TimelineService(store, no_failures)
    for index in range(3):
        await store.append("monitor_event", text=str(index))
    events, cursor = await service.page(2)
    await store.aclose()
    assert [event["text"] for event in events] == ["2", "1"]
    assert cursor == events[-1]["id"]
