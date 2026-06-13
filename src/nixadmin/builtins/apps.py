"""Built-in module: installed applications."""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module

manifest = Module(
    spec_version=SPEC_VERSION,
    name="apps",
    description=(
        "installed applications, packages, software, programs, tools, "
        "what is installed, do I have"
    ),
    fetchers=[
        Fetcher(
            name="list",
            cmd="nixadmin-apps",
            description="List of installed Nix packages and Flatpak apps",
            expose_as_tool=True,
        ),
    ],
)
