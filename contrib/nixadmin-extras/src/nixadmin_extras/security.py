"""Module: security — firewall, listening ports, logins.

Marked ``routing="local"`` because the answers (open ports, who is logged in,
login history) are sensitive and should be handled on-device by default. This is
the reference example of a privacy-pinned module.
"""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module

manifest = Module(
    spec_version=SPEC_VERSION,
    name="security",
    description=(
        "security, firewall, open ports, listening ports, who is logged in, "
        "login history, is my computer secure, has anyone logged in, intrusion"
    ),
    routing="local",  # privacy-sensitive — keep on device unless the user overrides
    fetchers=[
        Fetcher(
            name="firewall",
            cmd="systemctl is-active firewall.service nftables.service 2>/dev/null "
                "| paste -sd' ' || echo unknown",
            description="Whether the firewall is active",
            expose_as_tool=True,
        ),
        Fetcher(
            name="ports",
            cmd="ss -tln 2>/dev/null | head -15",
            description="TCP ports the machine is listening on",
            expose_as_tool=True,
        ),
        Fetcher(
            name="logins",
            cmd="who; echo '--- recent ---'; last -n 5 2>/dev/null | head -5",
            description="Who is logged in now and recent login history",
            expose_as_tool=True,
        ),
    ],
)
