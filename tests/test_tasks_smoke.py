from __future__ import annotations

import asyncio

import pytest

from nixadmin.tasks import TaskSet


async def test_task_set_cancels_and_awaits_owned_work():
    tasks = TaskSet("test")
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        await asyncio.Event().wait()

    task = tasks.spawn(work())
    await started.wait()
    await tasks.aclose()
    assert task.cancelled()
    assert not tasks.tasks


async def test_task_set_rejects_work_after_close():
    tasks = TaskSet("test")
    await tasks.aclose()

    async def work() -> None:
        pass

    with pytest.raises(RuntimeError, match="closing"):
        tasks.spawn(work())


async def test_task_set_shutdown_is_bounded_when_task_ignores_cancel():
    tasks = TaskSet("stubborn")
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = tasks.spawn(stubborn())
    await asyncio.sleep(0)
    await tasks.aclose(deadline_s=0.01)
    assert not task.done()
    release.set()
    await task
