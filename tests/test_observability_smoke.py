"""Bounded operational counters and warning suppression."""

from __future__ import annotations

from nixadmin.observability import MAX_COUNTER, MAX_RATE_KEYS, OperationalState


def test_counters_are_snapshotted_and_saturate():
    state = OperationalState()
    state._counters["failures"] = MAX_COUNTER  # exercise saturation without a huge loop
    state.increment("failures")
    snapshot = state.counters()
    assert snapshot == {"failures": MAX_COUNTER}
    snapshot["failures"] = 0
    assert state.counters()["failures"] == MAX_COUNTER


def test_warning_suppression_is_bounded(monkeypatch):
    monkeypatch.setattr("nixadmin.observability.time.monotonic", lambda: 10.0)
    state = OperationalState()
    assert state.should_log("same") is True
    assert state.should_log("same") is False
    for index in range(MAX_RATE_KEYS + 1):
        assert state.should_log(f"key-{index}") is True
    assert len(state._last_log) == MAX_RATE_KEYS
