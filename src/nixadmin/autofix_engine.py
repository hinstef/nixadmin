"""Stateful execution engine for the pure autofix policy."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from nixadmin import autofix, remediation
from nixadmin.errors import NixadminError
from nixadmin.log import get_logger
from nixadmin.store import Store

log = get_logger(__name__)

Emit = Callable[[str, str, str], Awaitable[None]]
RestartSystem = Callable[[str], Awaitable[str]]


async def _silent_status(_text: str) -> None:
    return None


class AutofixEngine:
    """Own failure episodes and turn policy decisions into verified outcomes."""

    def __init__(
        self, config: autofix.AutofixConfig, store: Store, emit: Emit,
        restart_system: RestartSystem,
    ) -> None:
        self.config = config
        self.store = store
        self._emit = emit
        self._restart_system = restart_system
        self.seen: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    async def seed(self) -> None:
        self.seen = {
            (unit["unit"], unit["scope"]) for unit in await remediation.failed_units()
        }

    async def run_once(self) -> None:
        """Handle each newly failed unit once and re-arm recovered units."""
        async with self._lock:
            units = await remediation.failed_units()
            failed = {(unit["unit"], unit["scope"]): unit for unit in units}
            self.seen &= set(failed)
            for key, unit in failed.items():
                if key in self.seen:
                    continue
                self.seen.add(key)
                try:
                    await self.handle_unit(unit["unit"], unit["scope"])
                except NixadminError as error:
                    log.warning("autofix failed", unit=unit["unit"], error=str(error))

    async def handle_unit(self, unit: str, scope: str) -> None:
        since = time.time() - self.config.window_s
        prior = await self.store.recent(50, unit=unit, kind="autofix", since=since)
        recorded_attempts = sum(
            1 for event in prior if event.get("meta", {}).get("action") == "restart"
        )
        systemd_attempts = await remediation.restart_count(unit, scope)
        attempts = max(recorded_attempts, systemd_attempts)
        decision = autofix.decide(
            scope=scope, prior_attempts=attempts, cfg=self.config,
        )
        if decision == "skip":
            return
        if decision == "inform":
            text = (
                f"{unit} keeps failing and needs a real fix."
                if attempts else f"{unit} stopped and needs attention."
            )
            await self.store.append(
                "autofix", unit=unit, scope=scope, severity="warning", text=text,
                meta={
                    "action": "inform", "recorded_attempts": recorded_attempts,
                    "systemd_restarts": systemd_attempts,
                },
            )
            await self._emit("autofix", "warning", text)
            return

        log.info("autofix restart", unit=unit, scope=scope, attempt=attempts + 1)
        outcome = await remediation.restart_resolved(
            unit, scope, status=_silent_status, restart_system=self._restart_system,
        )
        result = "healthy" if outcome.ok else "still_failing"
        await self.store.append(
            "autofix", unit=unit, scope=scope,
            severity="info" if outcome.ok else "warning", text=outcome.message,
            meta={"action": "restart", "outcome": result, "attempt": attempts + 1},
        )
        if outcome.ok:
            await self._emit(
                "autofix", "info",
                f"{unit} stopped, so I restarted it — it's healthy again.",
            )
        else:
            await self._emit(
                "autofix", "warning",
                f"{unit} stopped and a restart didn't fix it — it needs attention.",
            )
