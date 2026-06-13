"""Module: general system info (machine, kernel, memory, uptime, time)."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module

manifest = Module(
    spec_version=SPEC_VERSION,
    name="system",
    description=(
        "system info, what computer is this, machine model, kernel, operating system, "
        "how much memory, RAM, uptime, how long has it been running, hostname, "
        "what time is it, date, timezone"
    ),
    fetchers=[
        Fetcher(
            name="info",
            cmd="hostnamectl",
            description="Machine model, operating system, kernel and hostname",
            expose_as_tool=True,
        ),
        Fetcher(
            name="memory",
            cmd="free -h",
            description="Total, used and available memory (RAM)",
            expose_as_tool=True,
        ),
        Fetcher(
            name="uptime",
            cmd="uptime -p",
            description="How long the machine has been running",
            expose_as_tool=True,
        ),
        Fetcher(
            name="datetime",
            cmd="timedatectl",
            description="Current time, date and timezone",
            expose_as_tool=True,
        ),
    ],
)
