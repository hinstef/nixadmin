"""The core product promise as one executable architecture scenario."""

from __future__ import annotations

from nixadmin import autofix, remediation
from nixadmin.autofix_engine import AutofixEngine
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
    privileged_calls: list[str] = []
    notifications: list[tuple[str, str, str]] = []

    async def helper_restart(unit: str) -> str:
        privileged_calls.append(unit)
        return "helper accepted fixed restart action"

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
    monkeypatch.setattr(remediation.asyncio, "sleep", no_wait)
    engine = AutofixEngine(
        autofix.AutofixConfig(max_attempts=1), store, emit, helper_restart,
    )

    try:
        # Observe and persist the failure before policy acts.
        await timeline.record_failure_transitions(failures)

        # Policy crosses only the injected privileged restart boundary, then
        # remediation verifies live state before recording success.
        await engine.handle_unit("backup.service", "system")
        assert privileged_calls == ["backup.service"]

        # Recovery updates the timeline and the user-facing health summary.
        failures.clear()
        await timeline.record_failure_transitions(failures)
        ledger = await timeline.kept_well()
        assert ledger["healthy_now"] is True
        assert "quietly restarted 1 service" in ledger["tally"]

        # The persisted attempt exhausts the budget across episodes/restarts.
        await engine.handle_unit("backup.service", "system")
        assert privileged_calls == ["backup.service"]
        assert notifications[-1][1] == "warning"

        events = await store.recent(10)
        assert [event["kind"] for event in events] == [
            "autofix", "failure_cleared", "autofix", "failure_observed",
        ]
        assert events[2]["meta"] == {
            "action": "restart", "outcome": "healthy", "attempt": 1,
        }
        assert events[0]["meta"]["action"] == "inform"
    finally:
        await store.aclose()
