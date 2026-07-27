"""Smoke tests for the kept-well ledger — the pure fold over the event store.

Pins the honesty rules: a live failure zeroes the streak (never flatter), the
streak counts from the last time the machine handed a problem back to the human,
and the quiet tally counts only the good it did on its own.
"""

from __future__ import annotations

from nixadmin import ledger

DAY = ledger.DAY_S
NOW = 1_000_000.0


def _ev(ev_kind, ts, **meta):
    return {"kind": ev_kind, "ts": ts, "meta": meta}


def test_no_events_is_just_getting_started():
    lg = ledger.summarize([], now=NOW)
    assert lg.since_ts is None
    assert lg.streak_days == 0
    assert lg.headline == "Just getting started."
    assert lg.tally == []


def test_streak_counts_from_last_attention_moment():
    events = [
        # 10 days ago the machine had to hand something back (autofix informed).
        _ev("autofix", NOW - 10 * DAY, action="inform"),
        # since then, only silent self-care.
        _ev("autofix", NOW - 2 * DAY, action="restart", outcome="healthy"),
    ]
    lg = ledger.summarize(events, now=NOW)
    assert lg.streak_days == 10
    assert lg.healthy_now is True
    assert lg.headline == "Looked after itself for 10 days."
    assert lg.attention == 1
    assert lg.auto_restarts == 1
    assert "quietly restarted 1 service" in lg.tally


def test_live_failure_zeroes_the_streak_even_with_clean_history():
    events = [_ev("autofix", NOW - 30 * DAY, action="restart", outcome="healthy")]
    lg = ledger.summarize(events, now=NOW, current_failures=1)
    assert lg.healthy_now is False
    assert lg.streak_days == 0
    assert lg.headline == "Something needs a hand right now."


def test_manual_restart_breaks_streak_but_autofix_restart_does_not():
    # A person-triggered restart (tray) is an intervention; an autofix restart is
    # the machine keeping itself well — only the former resets the streak.
    manual = ledger.summarize(
        [_ev("restart", NOW - 3 * DAY, source="tray")], now=NOW)
    assert manual.streak_days == 3
    auto = ledger.summarize(
        [_ev("autofix", NOW - 3 * DAY, action="restart", outcome="healthy"),
         _ev("failure_observed", NOW - 20 * DAY)], now=NOW)
    # no attention moment → streak counts from the earliest thing we saw (20d)
    assert auto.streak_days == 20


def test_still_failing_autofix_is_an_attention_moment():
    lg = ledger.summarize(
        [_ev("autofix", NOW - 1 * DAY, action="restart", outcome="still_failing")],
        now=NOW)
    assert lg.streak_days == 1
    assert lg.attention == 1


def test_tally_counts_only_autonomous_upkeep_within_window():
    events = [
        _ev("autofix", NOW - 1 * DAY, action="restart", outcome="healthy"),
        _ev("autofix", NOW - 2 * DAY, action="restart", outcome="healthy"),
        # outside the 30-day window → not tallied
        _ev("autofix", NOW - 40 * DAY, action="restart", outcome="healthy"),
        # user-requested install is NOT the machine looking after itself → excluded
        _ev("action", NOW - 3 * DAY, kind="install_app"),
    ]
    lg = ledger.summarize(events, now=NOW, window_days=30)
    assert lg.auto_restarts == 2
    assert lg.tally == ["quietly restarted 2 services"]


def test_earliest_ts_overrides_truncated_scan_for_streak_floor():
    # The scanned events start only 3 days ago (a truncated window), but the store
    # says the first event was 90 days ago and nothing ever needed the human.
    events = [_ev("failure_cleared", NOW - 3 * DAY)]
    lg = ledger.summarize(events, now=NOW, earliest_ts=NOW - 90 * DAY)
    assert lg.streak_days == 90


def test_healthy_today_when_streak_under_a_day():
    lg = ledger.summarize(
        [_ev("restart", NOW - 3600, source="query")], now=NOW)
    assert lg.streak_days == 0
    assert lg.headline == "Looking after itself today."
