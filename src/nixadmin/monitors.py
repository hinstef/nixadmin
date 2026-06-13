"""Monitors — reactive watches that emit events.

Two sources, both async:

* **poll** — run a command every ``interval`` seconds (floored to
  :data:`MIN_INTERVAL`), fire when the module's ``trigger`` returns True. A shared
  semaphore caps how many poll commands run at once so a misbehaving module can't
  saturate the machine.
* **dbus** — subscribe via ``dbus-fast`` and fire when a signal (optionally passing
  the module's ``filter``) arrives.

Events are delivered through an injected async ``emit(source, severity, text)``
callback; the daemon broadcasts them to clients / desktop notifications.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable

from nixadmin.log import get_logger
from nixadmin.sdk import Module, Monitor

log = get_logger(__name__)

Emit = Callable[[str, str, str], Awaitable[None]]

MIN_INTERVAL = 10          # seconds — floor for poll monitors
MAX_CONCURRENT_POLLS = 4   # cap on simultaneous poll-command executions


class MonitorRunner:
    """Owns all monitor tasks; start once, ``aclose`` on shutdown."""

    def __init__(self, modules: list[Module], emit: Emit) -> None:
        self._modules = modules
        self._emit = emit
        self._tasks: list[asyncio.Task[None]] = []
        self._poll_sem = asyncio.Semaphore(MAX_CONCURRENT_POLLS)
        self._dbus_objs: list[object] = []  # keep bus refs alive

    async def start(self) -> None:
        for module in self._modules:
            for monitor in module.monitors:
                if monitor.source == "poll":
                    self._tasks.append(asyncio.create_task(self._poll_loop(module, monitor)))
                elif monitor.source == "dbus":
                    self._tasks.append(asyncio.create_task(self._dbus_subscribe(module, monitor)))
        log.info("monitors started", count=len(self._tasks))

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ---- poll ------------------------------------------------------------- #

    async def _poll_loop(self, module: Module, monitor: Monitor) -> None:
        interval = max(monitor.interval, MIN_INTERVAL)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._poll_sem:
                    out = await asyncio.to_thread(_run, monitor.cmd)
                if monitor.trigger and monitor.trigger(out):
                    await self._fire(module, monitor, out)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("poll monitor error", monitor=monitor.name, error=str(e))

    # ---- dbus ------------------------------------------------------------- #

    async def _dbus_subscribe(self, module: Module, monitor: Monitor) -> None:
        try:
            from dbus_fast import BusType, Message, MessageType
            from dbus_fast.aio import MessageBus

            bus_type = BusType.SYSTEM if monitor.bus == "system" else BusType.SESSION
            bus = await MessageBus(bus_type=bus_type).connect()
            self._dbus_objs.append(bus)

            # Match rule limits traffic to the interface/signal we care about.
            await bus.call(Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[f"type='signal',interface='{monitor.interface}',member='{monitor.signal}'"],
            ))

            def handler(msg: Message) -> None:
                if msg.message_type is not MessageType.SIGNAL:
                    return
                if msg.interface != monitor.interface or msg.member != monitor.signal:
                    return
                if monitor.filter and not monitor.filter(*(msg.body or [])):
                    return
                asyncio.create_task(self._fire(module, monitor, _summarize(msg.body)))

            bus.add_message_handler(handler)
            log.info("dbus monitor subscribed", monitor=monitor.name, signal=monitor.signal)
            await asyncio.Event().wait()  # keep the task (and bus) alive
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — dbus unavailable shouldn't kill the daemon
            log.warning("dbus monitor failed", monitor=monitor.name, error=str(e))

    # ---- shared ----------------------------------------------------------- #

    async def _fire(self, module: Module, monitor: Monitor, detail: str) -> None:
        text = f"[{module.name}] {monitor.name}: {detail}".strip()
        await self._emit(f"monitor.{monitor.name}", monitor.severity, text)


def _run(cmd: str) -> str:
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT, timeout=10, text=True
        ).strip()
    except Exception as e:  # noqa: BLE001
        return f"(error: {e})"


def _summarize(body: list[object] | None) -> str:
    if not body:
        return "signal received"
    # For systemd JobRemoved the 3rd arg is the unit name — surface it if present.
    return " ".join(str(x) for x in body[:4])
