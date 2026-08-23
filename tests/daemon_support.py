from __future__ import annotations

import asyncio

from nixadmin import protocol as wire


class FakeConn:
    """Record daemon messages and answer confirmations deterministically."""

    def __init__(self, confirm_answer: bool) -> None:
        self.sent: list[wire.Message] = []
        self.confirms: list[str] = []
        self._answer = confirm_answer

    async def send(self, message: wire.Message) -> None:
        self.sent.append(message)

    async def confirm(self, _qid: str, text: str) -> bool:
        self.confirms.append(text)
        return self._answer

    def deltas(self) -> str:
        return "".join(message.text for message in self.sent if isinstance(message, wire.Delta))


async def read_until(
    reader: asyncio.StreamReader, type_: str, wait: float = 2.0,
) -> wire.Message:
    async with asyncio.timeout(wait):
        async for raw in reader:
            line = raw.decode().strip()
            if not line:
                continue
            message = wire.decode(line)
            if message.TYPE == type_:
                return message
    raise AssertionError(f"never saw {type_}")
