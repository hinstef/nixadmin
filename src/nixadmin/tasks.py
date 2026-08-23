"""Structured ownership for component-scoped asyncio tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from nixadmin.log import get_logger

log = get_logger(__name__)


class TaskSet:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        if self._closing:
            coroutine.close()
            raise RuntimeError(f"{self.name} task set is closing")
        task = asyncio.create_task(coroutine, name=self.name)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("owned task failed", component=self.name, error=str(error), exc_info=error)

    async def aclose(self, deadline_s: float = 5.0) -> None:
        self._closing = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=deadline_s)
            if pending:
                log.error(
                    "owned tasks ignored cancellation", component=self.name,
                    count=len(pending),
                )
