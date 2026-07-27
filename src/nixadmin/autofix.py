"""Autofix policy — decide what to do about a failed unit.

The daemon already *notices* systemd unit failures (the ``services`` monitor's
D-Bus ``JobRemoved`` event). This is the missing **policy**: given a failed unit
and how many times we've already tried to restart it recently, decide whether to
auto-restart, to merely inform, or to do nothing.

Per ``docs/ux.md``'s act/ask matrix: a *failed* unit is already broken, so a
restart is the reversible, low-consequence, "act silently + record" case. A
restart that keeps not sticking is the "inform" case — we never silently loop.

This module is **pure** (no I/O, no daemon deps) so the policy is exhaustively
unit-testable; the engine that gathers state and acts lives in the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Decision = Literal["restart", "inform", "skip"]


@dataclass(frozen=True, slots=True)
class AutofixConfig:
    """Tunables from ``services.nixadmin.autofix.*``."""

    enable: bool = True
    #: Auto-restart system-scope units too (via the root helper), not just user units.
    system: bool = True
    #: Auto-restarts allowed per unit within ``window_s`` before we stop and inform.
    max_attempts: int = 1
    window_s: float = 3600.0


def decide(*, scope: str, prior_attempts: int, cfg: AutofixConfig) -> Decision:
    """Choose the action for a failed unit.

    ``prior_attempts`` is how many times autofix has already restarted this unit
    within the loop-guard window (from the event store).
    """
    if not cfg.enable:
        return "skip"
    if scope == "system" and not cfg.system:
        return "inform"  # system autonomy disabled → surface it, don't act
    if prior_attempts >= cfg.max_attempts:
        return "inform"  # restarting isn't fixing it → stop looping, ask for help
    return "restart"
