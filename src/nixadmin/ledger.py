"""The kept-well ledger — silence made legible.

A system you never see act is one you stop trusting. Borrowed from ADAS ("400 km,
0 interventions"): the machine *unintrusively surfaces its own track record* so
the quiet is accounted for, never opaque. See ``docs/ux.md`` — pull-only, never a
popup, honest over flattering.

This module is **pure**: it turns the event store's audit trail (autofix restarts,
recoveries, installs, and the moments the system had to hand back to the human)
into a small :class:`Ledger` a glance-surface can render. No I/O, no clock of its
own — ``now`` and the live failure set are passed in, so it is exhaustively
testable and the daemon owns the wiring.

Two things it computes:

* the **streak** — days the machine has looked after itself, i.e. days since the
  last moment it *couldn't* (a failure it wouldn't/couldn't auto-fix, or one the
  person had to fix by hand). Honest: anything failing *right now* means the
  streak is zero, never a flattering number.
* the **quiet tally** — the autonomous upkeep it did without being asked (restarts
  that stuck) over a recent window. Only genuinely self-directed actions belong
  here; a user-requested install is not the machine looking after itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

DAY_S = 86400.0
DEFAULT_WINDOW_DAYS = 30

#: How many recent events the daemon scans to build the ledger. Generous enough to
#: cover the default window on a busy machine without loading the whole store.
LEDGER_SCAN_LIMIT = 1000

# Event kinds/shapes that mean "the machine needed the human" — they *break* the
# looked-after-itself streak. Everything else (a failure it silently healed, a
# question, an explanation) does not.
_MANUAL_RESTART_SOURCES = frozenset({"tray", "query"})


def _is_attention_moment(ev: dict[str, Any]) -> bool:
    """True if this event marks the machine handing a problem back to the person."""
    kind = ev.get("kind")
    meta = ev.get("meta") or {}
    if kind == "autofix":
        # Surfaced instead of fixing (loop guard / disabled), or tried and failed.
        return meta.get("action") == "inform" or meta.get("outcome") == "still_failing"
    if kind == "restart":
        # A restart the *person* triggered (tray/invoke) — the machine didn't
        # keep itself well on its own here. Autofix records its own restarts under
        # the ``autofix`` kind, so those never land here.
        return meta.get("source") in _MANUAL_RESTART_SOURCES
    return False


@dataclass(frozen=True, slots=True)
class Ledger:
    streak_days: int
    healthy_now: bool
    since_ts: float | None            # when the current streak began (None = no data yet)
    auto_restarts: int = 0            # autofix restarts that stuck, in the window
    recoveries: int = 0              # failures that cleared, in the window
    attention: int = 0               # times it handed back to the human, in the window
    headline: str = ""
    tally: list[str] = field(default_factory=list)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def summarize(
    events: Iterable[dict[str, Any]],
    *,
    now: float,
    current_failures: int = 0,
    earliest_ts: float | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Ledger:
    """Fold the event timeline into a :class:`Ledger`.

    ``events`` are store rows (any order); ``current_failures`` is how many units
    are failing *right now* (live, not reconstructed) so the streak can be honest.
    ``earliest_ts`` is the store's true first-event time (``MIN(ts)``) — passed in
    so that when the scanned events are capped/truncated, the "never needed the
    human" streak still counts from the real beginning, not a recent row.
    """
    evs = list(events)
    window_start = now - window_days * DAY_S

    last_attention: float | None = None
    earliest_seen: float | None = None
    auto_restarts = recoveries = attention = 0

    for ev in evs:
        ts = float(ev.get("ts", 0.0))
        is_attn = _is_attention_moment(ev)
        if earliest_seen is None or ts < earliest_seen:
            earliest_seen = ts
        if is_attn and (last_attention is None or ts > last_attention):
            last_attention = ts
        if ts < window_start:
            continue
        kind = ev.get("kind")
        meta = ev.get("meta") or {}
        healed = meta.get("action") == "restart" and meta.get("outcome") == "healthy"
        if kind == "autofix" and healed:
            auto_restarts += 1
        elif kind == "failure_cleared":
            recoveries += 1
        if is_attn:
            attention += 1

    # The streak floor: the store's authoritative MIN(ts) if given, else the
    # earliest row we actually scanned.
    floor = earliest_ts if earliest_ts is not None else earliest_seen
    healthy_now = current_failures == 0

    if not healthy_now:
        # Something is broken right now — never dress that up as a streak.
        since = now
        streak_days = 0
    elif last_attention is not None:
        since = last_attention
        streak_days = max(0, int((now - since) // DAY_S))
    elif floor is not None:
        # Never needed the human in what we can see → count from first observation.
        since = floor
        streak_days = max(0, int((now - since) // DAY_S))
    else:
        since = None
        streak_days = 0

    return Ledger(
        streak_days=streak_days,
        healthy_now=healthy_now,
        since_ts=since,
        auto_restarts=auto_restarts,
        recoveries=recoveries,
        attention=attention,
        headline=_headline(streak_days, healthy_now, since),
        tally=_tally(auto_restarts),
    )


def _headline(streak_days: int, healthy_now: bool, since: float | None) -> str:
    if since is None:
        return "Just getting started."
    if not healthy_now:
        return "Something needs a hand right now."
    if streak_days == 0:
        return "Looking after itself today."
    return f"Looked after itself for {_plural(streak_days, 'day')}."


def _tally(auto_restarts: int) -> list[str]:
    items: list[str] = []
    if auto_restarts:
        items.append(f"quietly restarted {_plural(auto_restarts, 'service')}")
    return items
