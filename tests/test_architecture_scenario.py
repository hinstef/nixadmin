"""The core product promise as one executable architecture scenario."""

from __future__ import annotations

import asyncio
import json

from nixadmin import autofix, remediation
from nixadmin.autofix_engine import AutofixEngine
from nixadmin.safety import SafetyGate
from nixadmin.store import EventStore
from nixadmin.timeline import TimelineService


async def test_failure_is_observed_fixed_verified_and_not_restarted_twice(
    tmp_path, monkeypatch,
):
    """Models are absent: deterministic state and policy own the complete loop."""
    failures = [{
        "unit": "backup.service", "scope": "system", "description": "Backup",
    }]

    async def current_failures():
        return failures

    store = EventStore(tmp_path / "events.db")
    timeline = TimelineService(store, current_failures)
    helper_requests: list[dict[str, str]] = []
    notifications: list[tuple[str, str, str]] = []

    helper_socket = str(tmp_path / "helper.sock")

    async def fake_helper(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        helper_requests.append(json.loads((await reader.readline()).decode()))
        writer.write(b'{"exit": 0}\n')
        await writer.drain()
        writer.close()

    async def emit(source: str, severity: str, text: str) -> None:
        notifications.append((source, severity, text))

    async def no_prior_systemd_restarts(_unit: str, _scope: str) -> int:
        return 0

    async def verified_healthy(_unit: str, _scope: str) -> bool:
        return False

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(remediation, "restart_count", no_prior_systemd_restarts)
    monkeypatch.setattr(remediation, "_is_failed", verified_healthy)
    monkeypatch.setattr(remediation, "failed_units", current_failures)
    monkeypatch.setattr(remediation.asyncio, "sleep", no_wait)
    helper_server = await asyncio.start_unix_server(fake_helper, path=helper_socket)
    engine = AutofixEngine(
        autofix.AutofixConfig(max_attempts=1), store, emit,
        SafetyGate(helper_socket).apply_restart,
    )

    try:
        # Observe and persist the failure before policy acts.
        await timeline.record_failure_transitions(failures)

        # The engine discovers the episode, crosses the real typed helper protocol,
        # then remediation verifies live state before recording success.
        await engine.run_once()
        assert helper_requests == [{"action": "restart", "unit": "backup.service"}]

        # Recovery updates the timeline and the user-facing health summary.
        failures.clear()
        await timeline.record_failure_transitions(failures)
        await engine.run_once()  # recovery re-arms the episode detector
        ledger = await timeline.kept_well()
        assert ledger["healthy_now"] is True
        assert "quietly restarted 1 service" in ledger["tally"]

        # A fresh episode is discovered automatically, but the persisted attempt
        # exhausts the restart budget across episodes.
        failures.append({
            "unit": "backup.service", "scope": "system", "description": "Backup",
        })
        await timeline.record_failure_transitions(failures)
        await engine.run_once()
        assert helper_requests == [{"action": "restart", "unit": "backup.service"}]
        assert notifications[-1][1] == "warning"

        events = await store.recent(10)
        assert [event["kind"] for event in events] == [
            "autofix", "failure_observed", "failure_cleared", "autofix",
            "failure_observed",
        ]
        assert events[3]["meta"] == {
            "action": "restart", "outcome": "healthy", "attempt": 1,
        }
        assert events[0]["meta"]["action"] == "inform"
    finally:
        helper_server.close()
        await helper_server.wait_closed()
        await store.aclose()
