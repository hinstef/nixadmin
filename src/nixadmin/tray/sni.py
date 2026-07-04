"""StatusNotifierItem + DBusMenu, implemented with ``dbus-fast``.

This module intentionally does **not** use ``from __future__ import annotations``:
``dbus-fast`` reads the *literal* string annotations (``-> 's'``, ``x: 'i'``) as
D-Bus type signatures, and PEP 563 would turn them into their quoted source text
and break marshalling. mypy is disabled for this file for the same reason (the
annotations aren't Python types) — see the pyproject override.

Two interfaces:
* ``org.kde.StatusNotifierItem`` — the tray icon itself (icon, status, tooltip).
* ``com.canonical.dbusmenu`` — the popup menu, rendered from a live model so the
  daemon's current state drives what the menu shows.

Registration handles the host (re)appearing: a desktop-panel restart re-owns the
watcher name, and we re-register so the icon comes back on its own.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from dbus_fast import PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

from nixadmin.log import get_logger

log = get_logger(__name__)

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
NOTIFY_NAME = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


async def notify(bus: MessageBus, title: str, body: str, *, replaces: int = 0) -> int:
    """Send a desktop notification and return its id (pass it back as ``replaces``
    to update the same bubble in place). Best-effort: returns 0 if no server."""
    try:
        introspection = await bus.introspect(NOTIFY_NAME, NOTIFY_PATH)
        obj = bus.get_proxy_object(NOTIFY_NAME, NOTIFY_PATH, introspection)
        iface = obj.get_interface(NOTIFY_NAME)
        result = await iface.call_notify(
            "nixadmin", replaces, "dialog-information", title, body, [], {}, -1,
        )
        return int(result)
    except Exception as e:  # noqa: BLE001 — a missing notification server must not crash the tray
        log.warning("notify failed", error=str(e))
        return 0


@dataclass
class MenuEntry:
    """One row in the tray menu. When ``unit`` is set the row acts on that exact
    ``unit``/``scope``; ``action`` says how — ``"restart"`` (fix it) or
    ``"explain"`` (ask the local model why it failed)."""

    id: int
    label: str = ""
    enabled: bool = True
    separator: bool = False
    unit: str | None = field(default=None)
    scope: str | None = field(default=None)
    action: str | None = field(default=None)  # "restart" | "explain"


class StatusNotifierItem(ServiceInterface):
    """The tray icon. Pixmaps are supplied ready-made (green/amber/grey discs)."""

    def __init__(
        self,
        pixmaps: "list[list[object]]",
        on_activate: "Callable[[int, int], None] | None" = None,
    ) -> None:
        super().__init__("org.kde.StatusNotifierItem")
        self._pixmaps = pixmaps
        self._status = "Active"
        self._tooltip = "nixadmin — starting…"
        self._on_activate = on_activate or (lambda _x, _y: None)

    # --- properties ------------------------------------------------------- #
    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> 's':
        return "SystemServices"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> 's':
        return "nixadmin"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> 's':
        return "nixadmin"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> 's':
        return self._status

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> 'i':
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> 's':
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> 'a(iiay)':
        return self._pixmaps

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> 's':
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> 'a(iiay)':
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> 's':
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> 'a(iiay)':
        return self._pixmaps

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> 's':
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> '(sa(iiay)ss)':
        return ["", [], "nixadmin", self._tooltip]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> 'b':
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> 'o':
        return MENU_PATH

    # --- methods ---------------------------------------------------------- #
    @method()
    def ContextMenu(self, x: 'i', y: 'i'):
        pass

    @method()
    def Activate(self, x: 'i', y: 'i'):
        self._on_activate(x, y)

    @method()
    def SecondaryActivate(self, x: 'i', y: 'i'):
        pass

    @method()
    def Scroll(self, delta: 'i', orientation: 's'):
        pass

    # --- signals ---------------------------------------------------------- #
    @signal()
    def NewIcon(self):
        return None

    @signal()
    def NewAttentionIcon(self):
        return None

    @signal()
    def NewToolTip(self):
        return None

    @signal()
    def NewStatus(self, status) -> 's':
        return status

    # --- driven by the poll loop ------------------------------------------ #
    def update(self, pixmaps: "list[list[object]]", status: str, tooltip: str) -> None:
        self._pixmaps = pixmaps
        self._tooltip = tooltip
        if status != self._status:
            self._status = status
            self.NewStatus(status)
        self.NewIcon()
        self.NewAttentionIcon()
        self.NewToolTip()


class DBusMenu(ServiceInterface):
    """``com.canonical.dbusmenu`` rendered from a live model callable."""

    def __init__(
        self,
        model: "Callable[[], list[MenuEntry]]",
        on_activate: "Callable[[MenuEntry], None]",
    ) -> None:
        super().__init__("com.canonical.dbusmenu")
        self._model = model
        self._on_activate = on_activate
        self._revision = 1

    # --- pure logic (unit-tested; the D-Bus methods below are thin wrappers, as
    #     dbus-fast's @method turns them into dispatch stubs not callable in-proc) #
    def entry_props(self, e: MenuEntry) -> dict:
        if e.separator:
            return {"type": Variant('s', "separator")}
        return {"label": Variant('s', e.label), "enabled": Variant('b', e.enabled)}

    def build_layout(self) -> list:
        children = [
            Variant('(ia{sv}av)', [e.id, self.entry_props(e), []])
            for e in self._model()
        ]
        root = [0, {"children-display": Variant('s', "submenu")}, children]
        return [self._revision, root]

    def group_properties(self, ids: "list[int]") -> list:
        by_id = {e.id: e for e in self._model()}
        return [[i, self.entry_props(by_id[i])] for i in ids if i in by_id]

    def fire(self, entry_id: int) -> None:
        for e in self._model():
            if e.id == entry_id:
                self._on_activate(e)
                return

    # --- properties ------------------------------------------------------- #
    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> 'u':
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> 's':
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> 's':
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> 'as':
        return []

    # --- D-Bus surface (thin wrappers over the logic above) --------------- #
    @method()
    def GetLayout(self, parentId: 'i', recursionDepth: 'i',
                  propertyNames: 'as') -> 'u(ia{sv}av)':
        return self.build_layout()

    @method()
    def GetGroupProperties(self, ids: 'ai', propertyNames: 'as') -> 'a(ia{sv})':
        return self.group_properties(ids)

    @method()
    def GetProperty(self, id: 'i', name: 's') -> 'v':
        by_id = {e.id: e for e in self._model()}
        props = self.entry_props(by_id[id]) if id in by_id else {}
        return props.get(name, Variant('s', ""))

    @method()
    def Event(self, id: 'i', eventId: 's', data: 'v', timestamp: 'u'):
        if eventId == "clicked":
            self.fire(id)

    @method()
    def EventGroup(self, events: 'a(isvu)') -> 'ai':
        for eid, event_id, _data, _ts in events:
            if event_id == "clicked":
                self.fire(eid)
        return []

    @method()
    def AboutToShow(self, id: 'i') -> 'b':
        return True  # always let us re-render — failures may have changed

    @method()
    def AboutToShowGroup(self, ids: 'ai') -> 'aiai':
        return [[], []]

    @signal()
    def LayoutUpdated(self, revision, parent) -> 'ui':
        return [revision, parent]

    def bump(self) -> None:
        """Tell the host the menu changed so it refetches the layout."""
        self._revision += 1
        self.LayoutUpdated(self._revision, 0)


async def register(bus: MessageBus, item: StatusNotifierItem, menu: DBusMenu) -> "Registrar":
    """Export the objects and register with the tray host; keep re-registering if
    the host restarts. Returns the live :class:`Registrar`."""
    bus.export(ITEM_PATH, item)
    bus.export(MENU_PATH, menu)
    reg = Registrar(bus)
    await reg.start()
    return reg


class Registrar:
    """Owns registration with the StatusNotifierWatcher and re-does it whenever a
    new watcher owner appears (panel restart)."""

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self._well_known = f"org.kde.StatusNotifierItem-{_pid()}-1"

    async def start(self) -> None:
        await self.bus.request_name(self._well_known)
        await self._watch_owner_changes()
        await self.ensure_registered()

    async def ensure_registered(self) -> bool:
        try:
            introspection = await self.bus.introspect(WATCHER_NAME, WATCHER_PATH)
            obj = self.bus.get_proxy_object(WATCHER_NAME, WATCHER_PATH, introspection)
            iface = obj.get_interface(WATCHER_NAME)
            await iface.call_register_status_notifier_item(self._well_known)
            log.info("tray registered", name=self._well_known)
            return True
        except Exception as e:  # noqa: BLE001 — host may be absent; retry on owner change
            log.warning("tray registration failed", error=str(e))
            return False

    async def _watch_owner_changes(self) -> None:
        introspection = await self.bus.introspect(DBUS_NAME, DBUS_PATH)
        obj = self.bus.get_proxy_object(DBUS_NAME, DBUS_PATH, introspection)
        iface = obj.get_interface(DBUS_NAME)

        def on_change(name: str, old_owner: str, new_owner: str) -> None:
            if name == WATCHER_NAME and new_owner:
                import asyncio
                asyncio.create_task(self.ensure_registered())

        iface.on_name_owner_changed(on_change)


def _pid() -> int:
    import os
    return os.getpid()
