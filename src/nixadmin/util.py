"""Small shared helpers."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from nixadmin.errors import ExternalProcessError


async def run(*cmd: str, deadline_s: float | None = None) -> tuple[int, str]:
    """Run a command with no shell, returning ``(returncode, merged stdout+stderr)``.

    No shell means arguments (e.g. a resolved unit or package name) are passed
    literally — there is no injection surface even for user-derived values.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise ExternalProcessError("unavailable", cmd) from error
    except PermissionError as error:
        raise ExternalProcessError("permission_denied", cmd) from error
    try:
        async with asyncio.timeout(deadline_s):
            out, _ = await proc.communicate()
    except TimeoutError as error:
        await asyncio.shield(_terminate_group(proc))
        raise ExternalProcessError("timeout", cmd) from error
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_group(proc))
        raise
    return proc.returncode or 0, out.decode(errors="replace")


async def run_checked(*cmd: str, deadline_s: float | None = None) -> str:
    rc, output = await run(*cmd, deadline_s=deadline_s)
    if rc != 0:
        detail = output.strip()[-1000:]
        raise ExternalProcessError(
            "command_failed", cmd, exit_code=rc, detail=detail,
        )
    return output


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and descendants, then reap without an unbounded wait."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError, ProcessLookupError):
        async with asyncio.timeout(2.0):
            await proc.communicate()
