"""Module: performance — load and what's using CPU / memory."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module

manifest = Module(
    spec_version=SPEC_VERSION,
    name="performance",
    description=(
        "slow, sluggish, lagging, frozen, performance, why is it slow, "
        "what is using my CPU, what is using my memory, high usage, busy, load"
    ),
    fetchers=[
        Fetcher(
            name="load",
            cmd="uptime",
            description="System load average over 1, 5 and 15 minutes",
            expose_as_tool=True,
        ),
        Fetcher(
            name="top_cpu",
            cmd="ps -eo pid,comm,pcpu --sort=-pcpu | head -6",
            description="Processes using the most CPU",
            expose_as_tool=True,
        ),
        Fetcher(
            name="top_mem",
            cmd="ps -eo pid,comm,pmem --sort=-pmem | head -6",
            description="Processes using the most memory",
            expose_as_tool=True,
        ),
    ],
)
