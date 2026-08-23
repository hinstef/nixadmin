"""Small shared helpers."""

from __future__ import annotations

import asyncio

from nixadmin.errors import ExternalProcessError


async def run(*cmd: str, deadline_s: float | None = None) -> tuple[int, str]:
    """Run a command with no shell, returning ``(returncode, merged stdout+stderr)``.

    No shell means arguments (e.g. a resolved unit or package name) are passed
    literally — there is no injection surface even for user-derived values.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError as error:
        raise ExternalProcessError("unavailable", cmd) from error
    except PermissionError as error:
        raise ExternalProcessError("permission_denied", cmd) from error
    try:
        async with asyncio.timeout(deadline_s):
            out, _ = await proc.communicate()
    except TimeoutError as error:
        proc.kill()
        await proc.communicate()
        raise ExternalProcessError("timeout", cmd) from error
    except asyncio.CancelledError:
        proc.kill()
        await proc.communicate()
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
