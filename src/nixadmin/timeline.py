"""Timeline, failure-transition, and kept-well ledger service."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from nixadmin import ledger
from nixadmin.store import Store

FailuresFn = Callable[[], Awaitable[list[dict[str, str]]]]


class TimelineService:
    def __init__(self, store: Store, failures: FailuresFn) -> None:
        self.store = store
        self._failures = failures
        self._seen_failures: set[tuple[str, str]] = set()

    async def record_failure_transitions(self, units: list[dict[str, str]]) -> None:
        current = {(unit["unit"], unit["scope"]): unit for unit in units}
        keys = set(current)
        for unit, scope in keys - self._seen_failures:
            await self.store.append(
                "failure_observed", unit=unit, scope=scope, severity="warning",
                text=current[(unit, scope)].get("description") or f"{unit} failed",
            )
        for unit, scope in self._seen_failures - keys:
            await self.store.append(
                "failure_cleared", unit=unit, scope=scope, severity="info",
                text=f"{unit} recovered",
            )
        self._seen_failures = keys

    async def page(
        self, limit: int, *, unit: str | None = None, before_id: int | None = None,
    ) -> tuple[list[dict[str, object]], int | None]:
        bounded = max(1, min(limit, 1000))
        rows = await self.store.recent(bounded + 1, unit=unit, before_id=before_id)
        events = rows[:bounded]
        cursor = int(events[-1]["id"]) if len(rows) > bounded and events else None
        return events, cursor

    async def kept_well(self) -> dict[str, object]:
        now = time.time()
        window_start = now - ledger.DEFAULT_WINDOW_DAYS * ledger.DAY_S
        limit = ledger.LEDGER_SCAN_LIMIT
        autofix_events, restarts, cleared, earliest, failures = await asyncio.gather(
            self.store.recent(limit, kind="autofix"),
            self.store.recent(limit, kind="restart"),
            self.store.recent(limit, kind="failure_cleared", since=window_start),
            self.store.earliest(),
            self._failures(),
        )
        summary = ledger.summarize(
            [*autofix_events, *restarts, *cleared], now=now,
            current_failures=len(failures), earliest_ts=earliest,
        )
        return asdict(summary)
