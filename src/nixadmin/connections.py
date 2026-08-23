"""Unix-socket client framing, lifecycle ownership, and broadcasts."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from nixadmin import protocol as wire
from nixadmin.errors import ProtocolError
from nixadmin.log import get_logger
from nixadmin.observability import OperationalState

log = get_logger(__name__)

MAX_WIRE_MESSAGE_BYTES = 64 * 1024
CLIENT_SEND_TIMEOUT_S = 10.0

MessageHandler = Callable[["ClientConn", str], Awaitable[None]]
HelloFactory = Callable[[], wire.Hello]


class ClientConn:
    """One connected client and the work/futures scoped to its lifetime."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: dict[str, asyncio.Future[wire.Respond]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.owned_tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        self.closed = False

    async def send(self, msg: wire.Message) -> None:
        payload = wire.encode(msg).encode()
        if len(payload) > MAX_WIRE_MESSAGE_BYTES:
            raise ProtocolError("outgoing message exceeds wire limit")
        async with self._send_lock:
            self.writer.write(payload)
            async with asyncio.timeout(CLIENT_SEND_TIMEOUT_S):
                await self.writer.drain()

    async def confirm(self, qid: str, text: str) -> bool:
        await self.send(wire.Confirm(id=qid, text=text))
        response = await self._await_response(qid)
        return bool(response and response.confirmed)

    async def _await_response(self, qid: str) -> wire.Respond | None:
        future: asyncio.Future[wire.Respond] = asyncio.get_event_loop().create_future()
        self.pending[qid] = future
        try:
            return await future
        except asyncio.CancelledError:
            return None
        finally:
            self.pending.pop(qid, None)

    def deliver_response(self, response: wire.Respond) -> None:
        future = self.pending.get(response.id)
        if future and not future.done():
            future.set_result(response)


class ConnectionManager:
    """Own active transports; delegate decoded request lines to the daemon."""

    def __init__(
        self, on_message: MessageHandler, hello: HelloFactory, operations: OperationalState,
    ) -> None:
        self._on_message = on_message
        self._hello = hello
        self._operations = operations
        self.clients: set[ClientConn] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = ClientConn(reader, writer)
        self.clients.add(connection)
        try:
            await connection.send(self._hello())
            async for raw in reader:
                try:
                    line = raw.decode().strip()
                except UnicodeDecodeError as error:
                    self._operations.increment("invalid_encoding")
                    if self._operations.should_log("invalid_client_encoding"):
                        log.warning("invalid client encoding", error=str(error))
                    break
                if line:
                    await self._on_message(connection, line)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            ValueError,
            TimeoutError,
        ) as error:
            log.warning("client connection closed", error=str(error))
        finally:
            await self.close(connection)

    async def close(self, connection: ClientConn) -> None:
        if connection.closed:
            return
        connection.closed = True
        self.clients.discard(connection)
        tasks = tuple(connection.owned_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
        for pending in connection.pending.values():
            pending.cancel()
        connection.writer.close()
        with contextlib.suppress(Exception):
            async with asyncio.timeout(1.0):
                await connection.writer.wait_closed()

    async def broadcast(self, message: wire.Message) -> None:
        async def send(connection: ClientConn) -> None:
            try:
                await connection.send(message)
            except Exception:  # noqa: BLE001 — any transport failure retires the peer
                self._operations.increment("send_failures")
                await self.close(connection)

        await asyncio.gather(*(send(connection) for connection in tuple(self.clients)))
