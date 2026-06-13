"""Module: battery / power. Includes a poll monitor for low battery."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module, Monitor


def _battery_low(output: str) -> bool:
    try:
        return int(output.strip()) < 20
    except ValueError:
        return False


manifest = Module(
    spec_version=SPEC_VERSION,
    name="power",
    description=(
        "battery, charge, charging, plugged in, power, battery health, "
        "how much battery left, AC adapter, on battery"
    ),
    fetchers=[
        Fetcher(
            name="battery",
            # /sys paths have no spaces, so unquoted globbing is safe and keeps the
            # command free of nested quotes.
            cmd=(
                "for b in /sys/class/power_supply/BAT*; do "
                "[ -e $b/capacity ] && "
                "echo $(basename $b): $(cat $b/capacity)% $(cat $b/status); "
                "done 2>/dev/null; "
                "for a in /sys/class/power_supply/A*/online; do "
                "[ -e $a ] && echo AC online: $(cat $a); done 2>/dev/null"
            ),
            description="Battery charge level, charging state, and AC adapter status",
            expose_as_tool=True,
        ),
    ],
    monitors=[
        Monitor(
            name="battery-low",
            source="poll",
            cmd="cat /sys/class/power_supply/BAT*/capacity 2>/dev/null | head -1 || echo 100",
            interval=120,
            trigger=_battery_low,
            severity="warning",
        ),
    ],
)
