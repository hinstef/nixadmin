"""Routing — decide which chain answers a query, and detect mutation intent.

Two pure, dependency-free pieces (easy to unit-test):

* :func:`detect_mutation` — a deterministic imperative-verb matcher. Must NOT use
  the LLM: classify can time out or miss, and a missed mutation lets the read-only
  local model fake a "Done!". This always runs.
* :func:`resolve` — the two-stage router. Given the desired chain (explicit /
  module-hint / default) and current availability, it returns a :class:`Decision`
  describing what to do, including when a `confirm` is required before going remote.

The classifier (local LLM) is *not* called here — the dispatcher runs classify and
passes the matched modules in, so routing stays pure and chain-independent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from nixadmin.sdk import Module

Chain = Literal["local", "remote"]
Action = Literal["proceed", "confirm_remote", "wait_local", "unavailable"]

# Imperative verbs/phrases that indicate the user wants a *change*, not an answer.
# Deliberately conservative and word-boundaried to avoid false positives like
# "what's installed" (no verb) vs "install firefox".
_MUTATION_PATTERNS = [
    r"\binstall\b", r"\buninstall\b", r"\bremove\b", r"\badd\b", r"\bdelete\b",
    r"\benable\b", r"\bdisable\b", r"\bturn (on|off)\b", r"\bset\b", r"\bchange\b",
    r"\bupdate\b", r"\bupgrade\b", r"\bfix\b", r"\brestart\b", r"\bstop\b",
    r"\bstart\b", r"\brebuild\b", r"\brollback\b", r"\broll back\b",
]
_MUTATION_RE = re.compile("|".join(_MUTATION_PATTERNS), re.IGNORECASE)


def detect_mutation(text: str) -> bool:
    """True if the query expresses intent to change the system (deterministic)."""
    return _MUTATION_RE.search(text) is not None


def resolve_desired_chain(
    *,
    explicit: Chain | None,
    matched: list[Module],
    default_chain: Chain,
) -> tuple[Chain, bool]:
    """Stage 1 — pick the desired chain and whether it is *pinned local*.

    Pinned local = the user explicitly asked for local, or a matched module is
    privacy-flagged (``routing="local"``). Pinned-local queries may only reach
    remote via explicit consent.

    Module-hint collisions resolve ``local > auto > remote``.
    """
    if explicit is not None:
        return explicit, explicit == "local"

    hints = {m.routing for m in matched}
    if "local" in hints:
        return "local", True  # privacy hint pins local
    if "remote" in hints:
        return "remote", False
    return default_chain, False


@dataclass(frozen=True, slots=True)
class Decision:
    chain: Chain
    action: Action
    pinned_local: bool
    #: Human-facing text for a confirm/status, when the action needs one.
    message: str = ""


def resolve(
    *,
    desired: Chain,
    pinned_local: bool,
    local_ready: bool,
    remote_ready: bool,
) -> Decision:
    """Stage 2 — reconcile the desired chain with availability.

    Core principle: the daemon never silently changes where a query runs. Any
    deviation toward remote surfaces as a `confirm` (``action="confirm_remote"``).
    """
    if desired == "remote":
        if remote_ready:
            return Decision("remote", "proceed", pinned_local)
        if local_ready:
            return Decision(
                "local", "confirm_remote", pinned_local,
                "The remote assistant is unavailable. Use the on-device model instead?",
            )
        return Decision("remote", "unavailable", pinned_local,
                        "No assistant is available right now.")

    # desired == "local"
    if local_ready:
        return Decision("local", "proceed", pinned_local)

    # local configured but warming up / down
    if remote_ready:
        msg = (
            "The on-device model is starting. Use the remote assistant instead "
            "(your request would leave this device)?"
            if pinned_local
            else "The on-device model is starting. Use the remote assistant instead, or wait?"
        )
        return Decision("remote", "confirm_remote", pinned_local, msg)

    return Decision("local", "wait_local", pinned_local,
                    "The on-device model is still starting…")
