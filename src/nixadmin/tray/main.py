"""nixadmin-tray entry point — wire the daemon client to the tray icon.

The loop is deliberately quiet: poll the daemon for failed units on a slow timer,
refresh immediately on a daemon event, and otherwise sit green and silent. Colour
is the whole message — green fine, amber something failed, grey can't reach the
daemon. The menu (gav.5) turns those failures into one-click fix-its.
"""

from __future__ import annotations

import asyncio
import contextlib

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from nixadmin.log import get_logger
from nixadmin.tray import icons
from nixadmin.tray.client import DaemonClient, socket_path
from nixadmin.tray.sni import DBusMenu, MenuEntry, StatusNotifierItem, register

log = get_logger(__name__)

POLL_INTERVAL_S = 20.0
QUIT_ID = 9000


def _menu_model(connected: bool, units: list[dict[str, str]] | None) -> list[MenuEntry]:
    """Status header, one clickable fix-it per failed unit, then Quit.

    Ids: 1 = header (disabled), 100+ = fix-it rows, QUIT_ID = quit. Clicking a
    fix-it row restarts exactly that unit (see :meth:`Tray._on_menu`)."""
    rows: list[MenuEntry] = []
    if not connected:
        rows.append(MenuEntry(1, "⚠ daemon unreachable", enabled=False))
    elif units:
        rows.append(MenuEntry(1, f"⚠ {len(units)} service(s) failed", enabled=False))
        for i, u in enumerate(units):
            rows.append(MenuEntry(
                100 + i, f"Restart {u['unit']}", unit=u["unit"], scope=u["scope"],
            ))
    else:
        rows.append(MenuEntry(1, "✓ all services healthy", enabled=False))
    rows.append(MenuEntry(2, separator=True))
    rows.append(MenuEntry(QUIT_ID, "Quit nixadmin tray"))
    return rows


def _tooltip(connected: bool, units: list[dict[str, str]] | None) -> str:
    if not connected:
        return "nixadmin — daemon unreachable"
    if units:
        names = ", ".join(u["unit"] for u in units[:3])
        more = f" (+{len(units) - 3} more)" if len(units) > 3 else ""
        return f"nixadmin — failed: {names}{more}"
    return "nixadmin — all services healthy"


class Tray:
    def __init__(self) -> None:
        self._units: list[dict[str, str]] | None = None
        self._connected = False
        self._stop = asyncio.Event()
        self.item = StatusNotifierItem(icons.pixmaps(icons.UNKNOWN))
        self.menu = DBusMenu(self._model, self._on_menu)
        self.client = DaemonClient(
            socket_path(),
            on_state=self._on_state,
            on_event=lambda _ev: self._schedule_refresh(),
        )

    def _model(self) -> list[MenuEntry]:
        return _menu_model(self._connected, self._units)

    def _on_menu(self, entry: MenuEntry) -> None:
        if entry.id == QUIT_ID:
            self._stop.set()
        elif entry.unit and entry.scope:
            asyncio.create_task(self._fix(entry.unit, entry.scope))

    async def _fix(self, unit: str, scope: str) -> None:
        """Restart a failed unit, then refresh so the icon reflects the outcome.
        A restart that didn't stick simply stays amber — the honest signal."""
        await self.client.restart_unit(unit, scope)
        await self._refresh()

    def _on_state(self, _connected: bool) -> None:
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        asyncio.create_task(self._refresh())

    async def _refresh(self) -> None:
        units = await self.client.list_failures()
        self._connected = self.client.connected and units is not None
        self._units = units
        count = len(units) if units else 0
        rgb = icons.health_color(self._connected, count)
        self.item.update(
            icons.pixmaps(rgb),
            icons.status_word(self._connected, count),
            _tooltip(self._connected, units),
        )
        self.menu.bump()

    async def run(self) -> None:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        await register(bus, self.item, self.menu)
        asyncio.create_task(self.client.run())
        poll = asyncio.create_task(self._poll())
        await self._stop.wait()
        poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll
        bus.disconnect()

    async def _poll(self) -> None:
        while True:
            await self._refresh()
            await asyncio.sleep(POLL_INTERVAL_S)


def main() -> None:
    try:
        asyncio.run(Tray().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
