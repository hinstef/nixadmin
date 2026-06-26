"""Built-in module: systemd services.

Watches for any unit entering 'failed', and — when asked — reports failed units
*with the reason each failed* (the tail of its journal), not just the name. Listing
only names lets the model invent plausible-but-wrong advice; the live log gives it
the real cause to diagnose from. This is core systemd territory, so it lives in the
builtin where error questions ("are there any errors?") actually classify.
"""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module, Monitor


def _job_failed(*args: object) -> bool:
    # JobRemoved(id: u, job: o, unit: s, result: s) — fire on result == "failed".
    return len(args) >= 4 and args[3] == "failed"


# Failed units WITH the reason each failed. Discovers whatever is actually failing
# at runtime (system + user) and gives the model each unit's raw journal tail.
# Derive-don't-hardcode: we do NOT grep for "error-looking" lines — a keyword
# filter throws away the real cause (e.g. "fatal: disk quota exceeded") while
# keeping systemd boilerplate. We supply the live log; the model reads it and
# names the cause. Shared with the extras `health` module.
FAILED_UNITS_CMD = r"""
sys=$(systemctl --failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}')
usr=$(systemctl --user --failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}')
if [ -z "$sys$usr" ]; then echo "No failed units (system or user)."; fi
for u in $sys; do
  echo "### FAILED (system): $u"
  journalctl -u "$u" -b --no-pager -n 15 -o cat 2>/dev/null || echo "(no log access)"
  echo
done
for u in $usr; do
  echo "### FAILED (user): $u"
  journalctl --user -u "$u" -b --no-pager -n 15 -o cat 2>/dev/null
  echo
done
"""


manifest = Module(
    spec_version=SPEC_VERSION,
    name="services",
    description=(
        "running services, systemd, daemons, failed units, errors, "
        "is anything wrong, something crashed, background processes, startup"
    ),
    fetchers=[
        Fetcher(
            name="failed",
            cmd=FAILED_UNITS_CMD,
            description="Failed systemd services (system and user), each with the "
                        "tail of its journal explaining why it failed",
            expose_as_tool=True,
        ),
    ],
    monitors=[
        Monitor(
            name="service-failed",
            source="dbus",
            bus="system",
            interface="org.freedesktop.systemd1.Manager",
            signal="JobRemoved",
            filter=_job_failed,
            severity="error",
        ),
    ],
)
