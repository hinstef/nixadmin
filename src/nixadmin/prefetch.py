"""Prefetch — run matched modules' fetcher commands and format the result.

Used by the **local chain only**. Fetchers run in parallel off the event loop
(``asyncio.to_thread``) so a slow command can't block the daemon. The formatted
output is injected into the local model's prompt as grounded context.

The *grounding guard* lives with the caller: if prefetch returns no usable data,
the local chain must say "I couldn't check" rather than answer ungrounded.
"""

from __future__ import annotations

import asyncio
import subprocess

from nixadmin.log import get_logger
from nixadmin.sdk import Fetcher, Module

log = get_logger(__name__)


async def prefetch(modules: list[Module]) -> str:
    """Run every fetcher of every matched module in parallel; format the output.

    Returns an empty string if there are no fetchers, so the caller can apply the
    grounding guard uniformly.
    """
    fetchers = [f for m in modules for f in m.fetchers]
    if not fetchers:
        return ""

    results = await asyncio.gather(*(_run(f) for f in fetchers))
    return "\n\n".join(f"$ {f.cmd}\n{out}" for f, out in zip(fetchers, results, strict=True))


async def _run(fetcher: Fetcher) -> str:
    """Run one fetcher command, returning stdout or a short error marker."""
    try:
        return await asyncio.to_thread(_run_blocking, fetcher.cmd, fetcher.timeout)
    except Exception as e:  # noqa: BLE001 — surface as data, never crash the chain
        log.warning("fetcher failed", cmd=fetcher.cmd, error=str(e))
        return f"(error: {e})"


def _run_blocking(cmd: str, timeout: int) -> str:
    # shell=True is deliberate and safe here: `cmd` is a static, module-authored
    # fetcher command (trusted single-author code — ADR 0001). The user's query is
    # NEVER interpolated into it, so there is no injection surface; shell features
    # (pipes, redirection) are what fetchers rely on.
    try:
        out = subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT, timeout=timeout, text=True
        )
        return out.strip()
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip() or f"(exit {e.returncode})"
    except subprocess.TimeoutExpired:
        return "(timed out)"
