"""Remediation tier — safe, reversible runtime fixes (no config change).

Diagnosis says *what's wrong*; this acts on it. The canonical first remediation is
restarting a failed unit: deterministic, reversible, and the fix for a large share
of real problems ("have you turned it off and on again"). As everywhere, the model
only phrases things — the action set and execution are deterministic, every action
is confirmed, and the result is **verified** (we re-check the unit and report the
real state, never a fake "Done!").

Scope-aware: a failed **user** unit is restarted directly (`systemctl --user`, no
privilege); a failed **system** unit is restarted through the root helper (injected
as ``restart_system``) — the tray's "fix it" for the 80% (systemd unit failures).
Session relogin / reboot come later.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nixadmin.errors import NixadminError
from nixadmin.log import get_logger
from nixadmin.util import run as _run

log = get_logger(__name__)

ConfirmFn = Callable[[str], Awaitable[bool]]
StatusFn = Callable[[str], Awaitable[None]]
# Privileged restart of a system unit (via the helper); raises on failure.
RestartFn = Callable[[str], Awaitable[str]]

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


async def run(
    rem: Remediation, *, confirm: ConfirmFn, status: StatusFn,
    restart_system: RestartFn | None = None,
) -> str:
    """Restart a matched unit (system or user), confirm first, then verify + report.

    System units go through ``restart_system`` (the privileged helper); user units
    are restarted directly. ``restart_system`` is required to fix a system unit.
    """
    system = await _list_units("system")
    user = await _list_units("user")
    scope_of = {u: "user" for u, _a, _d in user}
    scope_of.update({u: "system" for u, _a, _d in system})  # system wins on a name clash

    cand = list(dict.fromkeys(_choose(match_unit(rem.target, system + user), system + user)))
    if not cand:
        return f"I couldn't find a service matching '{rem.target}'."
    if len(cand) > 1:
        return "Several services match that — which one? " + ", ".join(cand)

    unit = cand[0]
    scope = scope_of.get(unit, "system")
    if not await confirm(f"Restart {unit}?"):
        log.info("remediation", kind=rem.kind, unit=unit, scope=scope, outcome="cancelled")
        return "Cancelled — nothing changed."

    await status(f"Restarting {unit}…")
    try:
        if scope == "system":
            if restart_system is None:
                return "I can't restart a system service in this context."
            await restart_system(unit)  # via the root helper; raises on failure
        else:
            await _run("systemctl", "--user", "restart", unit)
    except NixadminError as e:
        log.warning("remediation", kind=rem.kind, unit=unit, scope=scope,
                    outcome="failed", error=str(e))
        return f"I couldn't restart {unit} ({e})."

    await asyncio.sleep(1.0)  # let it settle before checking
    if not await _is_failed(unit, scope):
        log.info("remediation", kind=rem.kind, unit=unit, scope=scope, outcome="restarted")
        return f"Restarted {unit} — it's healthy again."

    # Restart didn't help — be honest and show the real reason rather than claim a fix.
    reason = await _unit_tail(unit, scope)
    log.warning("remediation", kind=rem.kind, unit=unit, scope=scope, outcome="still_failing")
    return (
        f"I restarted {unit}, but it's still failing — this needs a real fix, "
        f"not a restart:\n{reason}"
    )


async def failed_units() -> list[dict[str, str]]:
    """Currently-failed service units across both scopes, as structured data for a
    client (the tray) to render per-unit actions: ``{unit, scope, description}``."""
    out: list[dict[str, str]] = []
    for scope in ("system", "user"):
        for unit, active, desc in await _list_units(scope):
            if active == "failed":
                out.append({"unit": unit, "scope": scope, "description": desc})
    return out


def _scoped(scope: str, *args: str) -> tuple[str, ...]:
    """Prefix systemctl/journalctl args with --user for the user scope."""
    head, tail = args[0], args[1:]
    return (head, *(("--user",) if scope == "user" else ()), *tail)


async def _list_units(scope: str) -> list[tuple[str, str, str]]:
    _rc, out = await _run(*_scoped(
        scope, "systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager"))
    units: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[0].endswith(".service"):
            desc = parts[4] if len(parts) > 4 else ""
            units.append((parts[0], parts[2], desc))  # (unit, active, description)
    return units


async def _is_failed(unit: str, scope: str) -> bool:
    _rc, out = await _run(*_scoped(scope, "systemctl", "is-failed", unit))
    return out.strip() == "failed"


async def _unit_tail(unit: str, scope: str) -> str:
    _rc, out = await _run(*_scoped(
        scope, "journalctl", "-u", unit, "-b", "--no-pager", "-n", "12", "-o", "cat"))
    return out.strip()
