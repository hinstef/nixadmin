"""Module: bluetooth — adapter state and connected devices."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module

manifest = Module(
    spec_version=SPEC_VERSION,
    name="bluetooth",
    description=(
        "bluetooth, headphones, earbuds, speaker, wireless mouse, wireless keyboard, "
        "paired device, connected device, is bluetooth on"
    ),
    fetchers=[
        Fetcher(
            name="adapter",
            cmd="bluetoothctl show 2>/dev/null | grep -E 'Name|Powered|Discovering' "
                "|| rfkill list bluetooth",
            description="Bluetooth adapter name and power state",
            expose_as_tool=True,
        ),
        Fetcher(
            name="connected",
            cmd="bluetoothctl devices Connected 2>/dev/null",
            description="Currently connected bluetooth devices",
            expose_as_tool=True,
        ),
    ],
)
