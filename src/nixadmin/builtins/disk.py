"""Built-in module: disk / storage. Polls for a near-full root filesystem."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module, Monitor


def _root_over_90(output: str) -> bool:
    try:
        return int(output.strip()) > 90
    except ValueError:
        return False


manifest = Module(
    spec_version=SPEC_VERSION,
    name="disk",
    description=(
        "disk space, storage, free space, full, filesystem, drive, "
        "partition, mount, how much space"
    ),
    fetchers=[
        Fetcher(
            name="usage",
            cmd="df -h",
            description="Disk usage per filesystem",
            expose_as_tool=True,
        ),
        Fetcher(
            name="layout",
            cmd="lsblk",
            description="Block devices and partition layout",
            expose_as_tool=True,
        ),
    ],
    monitors=[
        Monitor(
            name="disk-full",
            source="poll",
            cmd="df / --output=pcent | tail -1 | tr -d '% '",
            interval=300,
            trigger=_root_over_90,
            severity="warning",
        ),
    ],
)
