"""Built-in modules — shipped with the daemon, always available."""

from daemon.module_base import Module, Fetcher, Monitor

MODULES = [
    Module(
        name="apps",
        description="installed applications, packages, software, programs, what is installed",
        fetchers=[Fetcher(cmd="nixadmin-apps")],
    ),
    Module(
        name="network",
        description="wifi, wireless, network, internet, connectivity, IP address, ping, DNS",
        fetchers=[
            Fetcher(cmd="nmcli -f active,ssid,signal,state dev wifi"),
            Fetcher(cmd="ping -c 2 8.8.8.8"),
        ],
        monitors=[
            Monitor(
                name="network-down",
                source="dbus",
                interface="org.freedesktop.NetworkManager",
                signal="StateChanged",
                severity="warning",
            ),
        ],
    ),
    Module(
        name="disk",
        description="disk space, storage, free space, full, filesystem, drive, partition",
        fetchers=[
            Fetcher(cmd="df -h"),
            Fetcher(cmd="lsblk"),
        ],
        monitors=[
            Monitor(
                name="disk-full",
                source="poll",
                cmd="df / --output=pcent | tail -1 | tr -d '% '",
                interval=300,
                trigger=lambda out: int(out.strip() or 0) > 90,
                severity="warning",
            ),
        ],
    ),
    Module(
        name="services",
        description="running services, systemd, daemons, failed units, background processes",
        fetchers=[
            Fetcher(cmd="systemctl --failed --no-pager"),
            Fetcher(cmd="systemctl --user --failed --no-pager"),
        ],
        monitors=[
            Monitor(
                name="service-failed",
                source="dbus",
                interface="org.freedesktop.systemd1.Manager",
                signal="JobRemoved",
                filter=lambda _id, _path, _unit, result: result == "failed",
                severity="error",
            ),
        ],
    ),
]
