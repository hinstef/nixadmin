"""Built-in module: network / Wi-Fi / connectivity.

Marked ``routing="local"`` is deliberately NOT done here — network status is not
privacy-sensitive. Includes a D-Bus monitor for NetworkManager state changes.
"""

from __future__ import annotations

from nixadmin.sdk import SPEC_VERSION, Fetcher, Module, Monitor

manifest = Module(
    spec_version=SPEC_VERSION,
    name="network",
    description=(
        "wifi, wireless, network, internet, connectivity, online, "
        "IP address, ping, DNS, ethernet, connection"
    ),
    fetchers=[
        Fetcher(
            name="wifi",
            cmd="nmcli -f active,ssid,signal,state dev wifi",
            description="Current Wi-Fi connection, signal strength and state",
            expose_as_tool=True,
        ),
        Fetcher(
            name="ping",
            cmd="ping -c 2 8.8.8.8",
            description="Basic internet reachability test",
            expose_as_tool=True,
        ),
    ],
    monitors=[
        Monitor(
            name="network-state-changed",
            source="dbus",
            bus="system",
            interface="org.freedesktop.NetworkManager",
            signal="StateChanged",
            severity="warning",
        ),
    ],
)
