"""
nixadmin daemon — ambient system intelligence layer.

Listens on a Unix socket. Multiple clients connect (terminal, GTK app, future UIs).
Reactive monitors push events to all connected clients.
Two call chains: local (classify+prefetch+summarize) and remote (full agent).
"""

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from pathlib import Path

from daemon.backends import local as local_backend
from daemon.backends import remote as remote_backend
from daemon.module_base import load_modules
from daemon.modules.builtin import MODULES as BUILTIN_MODULES
from daemon.protocol import (
    DeltaMsg, DoneMsg, ErrorMsg, EventMsg, ConfirmMsg, encode, decode
)

log = logging.getLogger("nixadmin")


class Config:
    socket_path: str = "/run/user/1000/nixadmin.sock"
    local_model:  str = "qwen2.5:3b"
    local_url:    str = "http://localhost:11434"
    remote_model: str = "claude-sonnet-4-5"
    remote_base:  str | None = None  # None = LiteLLM default; set to Hermes URL
    default_chain: str = "remote"    # "local" | "remote"


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.pending_confirm: asyncio.Future | None = None

    async def send(self, msg):
        self.writer.write(encode(msg).encode())
        await self.writer.drain()

    async def confirm(self, text: str) -> bool:
        future = asyncio.get_event_loop().create_future()
        self.pending_confirm = future
        await self.send(ConfirmMsg(id=str(uuid.uuid4()), text=text))
        try:
            return await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending_confirm = None


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.clients: set[Client] = set()
        self.modules = BUILTIN_MODULES + load_modules()
        log.info(f"loaded {len(self.modules)} modules: {[m.name for m in self.modules]}")

    async def broadcast_event(self, msg: EventMsg):
        for client in list(self.clients):
            try:
                await client.send(msg)
            except Exception:
                self.clients.discard(client)

    def _route(self, query: str) -> str:
        """Decide local or remote. Override with explicit prefix."""
        if query.startswith("/local "):
            return "local"
        if query.startswith("/remote "):
            return "remote"
        return self.config.default_chain

    def _strip_prefix(self, query: str) -> str:
        for prefix in ("/local ", "/remote "):
            if query.startswith(prefix):
                return query[len(prefix):]
        return query

    async def handle_query(self, client: Client, query_id: str, text: str):
        chain = self._route(text)
        text = self._strip_prefix(text)

        try:
            if chain == "local":
                gen = local_backend.call(
                    text, self.config.local_model,
                    self.config.local_url, self.modules,
                )
            else:
                gen = remote_backend.call(
                    text, self.config.remote_model,
                    self.config.remote_base,
                    confirm_fn=lambda msg: asyncio.ensure_future(client.confirm(msg)),
                )

            async for delta in gen:
                await client.send(DeltaMsg(id=query_id, text=delta))

            await client.send(DoneMsg(id=query_id))

        except Exception as e:
            log.exception(f"query {query_id} failed")
            await client.send(ErrorMsg(id=query_id, text=str(e)))

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client = Client(reader, writer)
        self.clients.add(client)
        log.info(f"client connected ({len(self.clients)} total)")

        try:
            async for raw in reader:
                line = raw.decode().strip()
                if not line:
                    continue
                msg = decode(line)
                t = msg.get("type")

                if t == "query":
                    asyncio.create_task(
                        self.handle_query(client, msg["id"], msg["text"])
                    )

                elif t == "cancel":
                    pass  # TODO: cancel in-flight task by id

                elif t == "respond":
                    if client.pending_confirm and not client.pending_confirm.done():
                        client.pending_confirm.set_result(msg.get("value", False))

        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            log.exception(f"client error: {e}")
        finally:
            self.clients.discard(client)
            writer.close()
            log.info(f"client disconnected ({len(self.clients)} total)")

    async def start_monitors(self):
        """Start all reactive monitors from loaded modules."""
        for module in self.modules:
            for monitor in module.monitors:
                if monitor.source == "poll":
                    asyncio.create_task(self._poll_monitor(module, monitor))
                elif monitor.source == "dbus":
                    asyncio.create_task(self._dbus_monitor(module, monitor))

    async def _poll_monitor(self, module, monitor):
        import asyncio, subprocess
        while True:
            await asyncio.sleep(monitor.interval)
            try:
                out = subprocess.check_output(
                    monitor.cmd, shell=True, text=True, timeout=10
                ).strip()
                if monitor.trigger and monitor.trigger(out):
                    await self.broadcast_event(EventMsg(
                        source=f"monitor.{monitor.name}",
                        severity=monitor.severity,
                        text=f"[{module.name}] {monitor.name} triggered: {out}",
                    ))
            except Exception as e:
                log.warning(f"poll monitor {monitor.name}: {e}")

    async def _dbus_monitor(self, module, monitor):
        # D-Bus subscription runs in a thread (dbus-python is sync)
        loop = asyncio.get_event_loop()
        def _subscribe():
            try:
                import dbus
                from dbus.mainloop.glib import DBusGMainLoop
                from gi.repository import GLib
                DBusGMainLoop(set_as_default=True)
                bus = dbus.SystemBus()

                def on_signal(*args):
                    if monitor.filter and not monitor.filter(*args):
                        return
                    unit_name = args[2] if len(args) > 2 else monitor.name
                    loop.call_soon_threadsafe(
                        asyncio.ensure_future,
                        self.broadcast_event(EventMsg(
                            source=f"monitor.{monitor.name}",
                            severity=monitor.severity,
                            text=f"{unit_name} — check `journalctl -u {unit_name} -n 20`",
                        ))
                    )

                bus.add_signal_receiver(
                    on_signal,
                    signal_name=monitor.signal,
                    dbus_interface=monitor.interface,
                )
                GLib.MainLoop().run()
            except Exception as e:
                log.warning(f"dbus monitor {monitor.name}: {e}")

        await loop.run_in_executor(None, _subscribe)

    async def run(self):
        sock = self.config.socket_path
        Path(sock).unlink(missing_ok=True)

        server = await asyncio.start_unix_server(self.handle_client, path=sock)
        os.chmod(sock, 0o660)
        log.info(f"listening on {sock}")

        await self.start_monitors()

        async with server:
            await server.serve_forever()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = Config()

    # Override from environment (set by NixOS module)
    config.local_model  = os.environ.get("NIXADMIN_LOCAL_MODEL",  config.local_model)
    config.local_url    = os.environ.get("NIXADMIN_LOCAL_URL",    config.local_url)
    config.remote_model = os.environ.get("NIXADMIN_REMOTE_MODEL", config.remote_model)
    config.remote_base  = os.environ.get("NIXADMIN_REMOTE_BASE",  config.remote_base)
    config.default_chain = os.environ.get("NIXADMIN_CHAIN",       config.default_chain)
    config.socket_path  = os.environ.get("NIXADMIN_SOCKET",       config.socket_path)

    daemon = Daemon(config)

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGTERM, loop.stop)
    loop.add_signal_handler(signal.SIGINT,  loop.stop)

    try:
        loop.run_until_complete(daemon.run())
    finally:
        Path(config.socket_path).unlink(missing_ok=True)
        loop.close()


if __name__ == "__main__":
    main()
