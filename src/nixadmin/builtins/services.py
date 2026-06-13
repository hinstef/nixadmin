"""Built-in module: systemd services. Watches for any unit entering 'failed'."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module, Monitor


def _job_failed(*args: object) -> bool:
    # JobRemoved(id: u, job: o, unit: s, result: s) — fire on result == "failed".
    return len(args) >= 4 and args[3] == "failed"


manifest = Module(
    spec_version=SPEC_VERSION,
    name="services",
    description=(
        "running services, systemd, daemons, failed units, "
        "background processes, startup, something crashed"
    ),
    fetchers=[
        Fetcher(
            name="failed",
            cmd="systemctl --failed --no-pager",
            description="System services that have failed",
            expose_as_tool=True,
        ),
        Fetcher(
            name="failed_user",
            cmd="systemctl --user --failed --no-pager",
            description="User services that have failed",
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
