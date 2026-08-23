"""Terminal client for the nixadmin daemon.

Connects to the daemon's Unix socket and runs a simple REPL: a spinner while the
daemon works, streamed answer text, inline confirm prompts, and monitor events
printed as they arrive. Depends only on :mod:`nixadmin.protocol` — no daemon deps.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import uuid
from pathlib import Path

from nixadmin import protocol as wire
from nixadmin.errors import ProtocolError
from nixadmin.tasks import TaskSet
from nixadmin.transport import negotiate_async

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _socket_path() -> str:
    env = os.environ.get("NIXADMIN_SOCKET")
    if env:
        return env
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")  # noqa: S108
    return str(Path(runtime) / "nixadmin.sock")


class Spinner:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._spin())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    async def _spin(self) -> None:
        for frame in itertools.cycle(SPINNER):
            sys.stdout.write(f"\r{frame} thinking…")
            sys.stdout.flush()
            await asyncio.sleep(0.08)


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.spinner = Spinner()
        self.session = uuid.uuid4().hex[:8]
        self._turn_done = asyncio.Event()
        self._streaming = False

    async def send(self, msg: wire.Message) -> None:
        self.writer.write(wire.encode(msg).encode())
        await self.writer.drain()

    async def reader_loop(self) -> None:
        async for raw in self.reader:
            line = raw.decode().strip()
            if not line:
                continue
            try:
                msg = wire.decode(line)
            except Exception:  # noqa: BLE001 — ignore junk
                continue
            await self._handle(msg)

    async def _handle(self, msg: wire.Message) -> None:
        if isinstance(msg, wire.Hello):
            ready = ", ".join(c for c, r in msg.ready.items() if r) or "none ready"
            print(f"nixadmin connected · chains: {ready} · modules: {len(msg.modules)}")
        elif isinstance(msg, wire.Delta):
            if not self._streaming:
                await self.spinner.stop()
                self._streaming = True
            sys.stdout.write(msg.text)
            sys.stdout.flush()
        elif isinstance(msg, wire.Status):
            await self.spinner.stop()
            print(f"\033[2m… {msg.text}\033[0m")
            self.spinner.start()
        elif isinstance(msg, wire.Confirm):
            await self.spinner.stop()
            ans = await _ainput(f"\n{msg.text} [y/N] ")
            await self.send(wire.Respond(id=msg.id, confirmed=ans.strip().lower() in ("y", "yes")))
            self.spinner.start()
        elif isinstance(msg, wire.Input):
            await self.spinner.stop()
            val = await _ainput(f"\n{msg.prompt} ")
            await self.send(wire.Respond(id=msg.id, value=val))
            self.spinner.start()
        elif isinstance(msg, wire.Done):
            await self.spinner.stop()
            if self._streaming:
                sys.stdout.write("\n")
            self._streaming = False
            self._turn_done.set()
        elif isinstance(msg, wire.Error):
            await self.spinner.stop()
            print(f"\n\033[31merror:\033[0m {msg.text}")
            self._turn_done.set()
        elif isinstance(msg, wire.Ready):
            print(f"\033[2m({msg.chain} chain is now ready)\033[0m")
        elif isinstance(msg, wire.Event):
            mark = {"info": "ℹ", "warning": "⚠", "error": "✗"}.get(msg.severity, "•")
            print(f"\n{mark} {msg.text}")

    async def repl(self) -> None:
        while True:
            try:
                text = await _ainput("\nnixadmin> ")
            except (EOFError, KeyboardInterrupt):
                return
            text = text.strip()
            if not text:
                continue
            if text in ("exit", "quit"):
                return
            self._turn_done.clear()
            self._streaming = False
            self.spinner.start()
            await self.send(wire.Query(id=uuid.uuid4().hex[:8], text=text, session=self.session))
            await self._turn_done.wait()


async def _ainput(prompt: str) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, lambda: input(prompt))


async def _run() -> int:
    sock = _socket_path()
    try:
        reader, writer = await asyncio.open_unix_connection(sock)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"nixadmin: daemon not running (socket {sock})", file=sys.stderr)
        return 1

    client = Client(reader, writer)
    try:
        hello = await negotiate_async(reader)
    except ProtocolError as error:
        print(f"nixadmin: {error}", file=sys.stderr)
        writer.close()
        await writer.wait_closed()
        return 1
    await client._handle(hello)
    tasks = TaskSet("cli")
    tasks.spawn(client.reader_loop())
    try:
        await client.repl()
    finally:
        await tasks.aclose()
        writer.close()
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
