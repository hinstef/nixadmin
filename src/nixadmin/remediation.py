"""Remediation tier — safe, reversible runtime fixes (no config change).

Diagnosis says *what's wrong*; this acts on it. The canonical first remediation is
restarting a failed unit: deterministic, reversible, and the fix for a large share
of real problems ("have you turned it off and on again"). As everywhere, the model
only phrases things — the action set and execution are deterministic, every action
is confirmed, and the result is **verified** (we re-check the unit and report the
real state, never a fake "Done!").

First slice: restart a failed USER unit (no privilege). System units (via the root
helper) and session relogin / reboot come later.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nixadmin.log import get_logger
from nixadmin.util import run as _run

log = get_logger(__name__)

ConfirmFn = Callable[[str], Awaitable[bool]]
StatusFn = Callable[[str], Awaitable[None]]

# "restart the backup service", "relaunch nixadmin-backup", "reload X" — an
# imperative naming a thing to restart. Skip how-to questions ("how do I restart…")
# but allow polite requests ("can you restart…"), which confirm catches anyway.
_RESTART_RE = re.compile(r"\b(?:restart|relaunch|reload)\b\s+(.+)", re.IGNORECASE)
_HOWTO_RE = re.compile(r"^\s*(?:how|what|why|when|where|who)\b", re.IGNORECASE)
_FILLER_RE = re.compile(
    r"\b(?:the|a|an|please|my|service|unit|daemon|app|application|"
    r"can|could|would|you|for|me)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Remediation:
    kind: str  # "restart_unit"
    target: str


def parse(text: str) -> Remediation | None:
    """Map an imperative to a remediation, or None if it isn't one."""
    if _HOWTO_RE.search(text):
        return None
    if m := _RESTART_RE.search(text):
        return Remediation("restart_unit", _normalize(m.group(1)))
    return None


def _normalize(phrase: str) -> str:
    s = phrase.strip().rstrip("?.!").lower()
    s = _FILLER_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# Pure matching (unit-tested)
# --------------------------------------------------------------------------- #


def match_unit(target: str, units: list[tuple[str, str, str]]) -> list[str]:
    """Names of units whose stem or description matches ``target``. ``units`` are
    ``(unit, active_state, description)`` triples from the live system."""
    t = target.lower().strip()
    if not t:
        return []
    hits = []
    for unit, _active, desc in units:
        stem = unit[:-8] if unit.endswith(".service") else unit
        hay = f"{stem} {desc}".lower()
        if t in hay or all(tok in hay for tok in t.split()):
            hits.append(unit)
    return hits


def _choose(matches: list[str], units: list[tuple[str, str, str]]) -> list[str]:
    """Prefer failed units — remediation usually follows a failure diagnosis."""
    failed = {u for u, active, _ in units if active == "failed"}
    failed_matches = [u for u in matches if u in failed]
    return failed_matches or matches


# --------------------------------------------------------------------------- #
# Executor (integration-tested live)
# --------------------------------------------------------------------------- #


async def run(rem: Remediation, *, confirm: ConfirmFn, status: StatusFn) -> str:
    """Restart a matched user unit, confirm first, then verify and report."""
    units = await _list_user_units()
    cand = _choose(match_unit(rem.target, units), units)

    if not cand:
        return f"I couldn't find a service matching '{rem.target}'."
    if len(cand) > 1:
        return "Several services match that — which one? " + ", ".join(cand)

    unit = cand[0]
    if not await confirm(f"Restart {unit}?"):
        log.info("remediation", kind=rem.kind, unit=unit, outcome="cancelled")
        return "Cancelled — nothing changed."

    await status(f"Restarting {unit}…")
    await _run("systemctl", "--user", "restart", unit)
    await asyncio.sleep(1.0)  # let it settle before checking

    if not await _is_failed(unit):
        log.info("remediation", kind=rem.kind, unit=unit, outcome="restarted")
        return f"Restarted {unit} — it's healthy again."

    # Restart didn't help — be honest and show the real reason rather than claim a fix.
    reason = await _unit_tail(unit)
    log.warning("remediation", kind=rem.kind, unit=unit, outcome="still_failing")
    return (
        f"I restarted {unit}, but it's still failing — this needs a real fix, "
        f"not a restart:\n{reason}"
    )


async def _list_user_units() -> list[tuple[str, str, str]]:
    _rc, out = await _run(
        "systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--no-pager"
    )
    units: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[0].endswith(".service"):
            desc = parts[4] if len(parts) > 4 else ""
            units.append((parts[0], parts[2], desc))  # (unit, active, description)
    return units


async def _is_failed(unit: str) -> bool:
    _rc, out = await _run("systemctl", "--user", "is-failed", unit)
    return out.strip() == "failed"


async def _unit_tail(unit: str) -> str:
    _rc, out = await _run(
        "journalctl", "--user", "-u", unit, "-b", "--no-pager", "-n", "12", "-o", "cat"
    )
    return out.strip()
