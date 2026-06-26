"""Module: system health / "is anything wrong?" — diagnosis over systemd + journald.

Seeded by a real incident (the COSMIC bar + dock vanishing): the decisive signal
was a *missing* process, not a *failed* unit — `systemctl --failed` showed nothing.

Design principle (see vision §"Derive, don't hardcode"): this module does NOT
encode "a healthy session looks like X". It gathers the **real, live state**
dynamically and lets the model infer the diagnosis from that plus the user's
symptom ("my bar disappeared" → the bar is the panel → it's absent from the
running list → and here's the EGL error). Knowledge of what COSMIC *is* comes from
the model; this module only supplies accurate current state.

Scoped to the locked stack (NixOS + systemd + COSMIC); see ADR 0002.
"""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module

# Pure live state — running cosmic processes, cosmic units, and the session's own
# declared dependency list. No hardcoded "expected" set: the model compares the
# user's symptom against what's actually here.
_COSMIC_STATE = r"""
echo "# Running COSMIC processes:"
pgrep -fa 'cosmic-' 2>/dev/null | grep -v pgrep || echo "(none)"
echo
echo "# COSMIC systemd units:"
systemctl --user list-units 'cosmic*' --all --no-pager 2>/dev/null | head -30
echo
echo "# Declared cosmic-session components:"
systemctl --user list-dependencies cosmic-session.target --no-pager 2>/dev/null | head -30
"""

# Failed units WITH the reason each failed. Just listing names ("nixos-upgrade
# failed") gives the model the symptom but no cause, so it can only parrot it.
# This discovers the failed units at runtime (system + user) and pulls the tail of
# each one's journal — the error lines if any match, else a plain tail as fallback.
# Still derive-don't-hardcode: no baked-in unit names or expected states; it reads
# whatever is actually failing and supplies the live log so the model can diagnose.
_FAILED_UNITS = r"""
p='error|fail|cannot|expect|reason|refus|timeout|denied'
sys=$(systemctl --failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}')
usr=$(systemctl --user --failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}')
if [ -z "$sys$usr" ]; then echo "No failed units (system or user)."; fi
for u in $sys; do
  echo "### FAILED (system): $u"
  d=$(journalctl -u "$u" -b --no-pager -n 30 -o cat 2>/dev/null | grep -iE "$p" | tail -10)
  [ -z "$d" ] && d=$(journalctl -u "$u" -b --no-pager -n 12 -o cat 2>/dev/null)
  echo "${d:-(no log access)}"
  echo
done
for u in $usr; do
  echo "### FAILED (user): $u"
  d=$(journalctl --user -u "$u" -b --no-pager -n 30 -o cat 2>/dev/null | grep -iE "$p" | tail -10)
  [ -z "$d" ] && d=$(journalctl --user -u "$u" -b --no-pager -n 12 -o cat 2>/dev/null)
  echo "$d"
  echo
done
"""

# Error-level logs this boot, both scopes. The previous version was --user only,
# so it missed every system-service failure (e.g. nixos-upgrade.service).
_RECENT_ERRORS = r"""
echo "# System error-level logs this boot:"
journalctl -p 3 -b --no-pager -o cat 2>/dev/null | tail -15 || echo "(no access)"
echo
echo "# User error-level logs this boot:"
journalctl --user -p 3 -b --no-pager -o cat 2>/dev/null | tail -15
"""

# Derived heuristic (not hardcoded knowledge): booted != current means a rebuild
# happened this boot — the precondition for GL clients failing to init EGL until
# relogin (the panel incident's root cause).
_REBUILD_SKEW = r"""
if [ "$(readlink /run/booted-system)" != "$(readlink /run/current-system)" ]; then
  echo "REBUILT SINCE BOOT: the system was rebuilt while running. Apps that restart"
  echo "may fail to render (EGL) until you log out and back in, or reboot."
else
  echo "No rebuild since boot."
fi
"""

manifest = Module(
    spec_version=SPEC_VERSION,
    name="health",
    description=(
        "is anything wrong, something broke, is everything ok, what's wrong, "
        "diagnose, health check, my bar disappeared, dock gone, panel missing, "
        "desktop broken, something crashed, why did X vanish, problems, errors"
    ),
    fetchers=[
        Fetcher(
            name="cosmic_state",
            cmd=_COSMIC_STATE,
            description="Live state of the COSMIC desktop: running processes, units, "
                        "and the session's declared components",
            expose_as_tool=True,
        ),
        Fetcher(
            name="failed_units",
            cmd=_FAILED_UNITS,
            description="Failed systemd services (system and user), each with the "
                        "tail of its journal explaining why it failed",
            expose_as_tool=True,
        ),
        Fetcher(
            name="recent_errors",
            cmd=_RECENT_ERRORS,
            description="Recent error-level log messages this boot (system and user)",
            expose_as_tool=True,
        ),
        Fetcher(
            name="coredumps",
            cmd="coredumpctl list --since -2h --no-pager 2>/dev/null | tail -10 "
                "|| echo 'none'",
            description="Programs that crashed in the last 2 hours",
            expose_as_tool=True,
        ),
        Fetcher(
            name="rebuild_skew",
            cmd=_REBUILD_SKEW,
            description="Whether a rebuild since boot may have broken graphics rendering",
            expose_as_tool=True,
        ),
    ],
)
