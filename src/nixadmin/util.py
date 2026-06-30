"""Small shared helpers."""

from __future__ import annotations

import asyncio


async def run(*cmd: str) -> tuple[int, str]:
    """Run a command with no shell, returning ``(returncode, merged stdout+stderr)``.

    No shell means arguments (e.g. a resolved unit or package name) are passed
    literally — there is no injection surface even for user-derived values.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")
