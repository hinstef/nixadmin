"""Module: updates — NixOS version and when the system was last built."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module

manifest = Module(
    spec_version=SPEC_VERSION,
    name="updates",
    description=(
        "updates, up to date, upgrade, new version, latest version, outdated, "
        "when did I last update, system version, NixOS generation"
    ),
    fetchers=[
        Fetcher(
            name="version",
            cmd="nixos-version",
            description="Current NixOS version string",
            expose_as_tool=True,
        ),
        Fetcher(
            name="last_built",
            cmd="stat -c 'Current system built: %y' /run/current-system",
            description="Timestamp of when the running system generation was built",
            expose_as_tool=True,
        ),
        Fetcher(
            name="generations",
            cmd="ls -d /nix/var/nix/profiles/system-*-link 2>/dev/null | wc -l "
                "| xargs -I{} echo '{} system generations available'",
            description="How many NixOS generations are available to roll back to",
            expose_as_tool=True,
        ),
    ],
)
