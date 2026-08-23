"""Prefetch — run matched modules' fetcher commands and format the result.

Used by the **local chain only**. Fetchers run in parallel off the event loop
(``asyncio.to_thread``) so a slow command can't block the daemon. The formatted
output is injected into the local model's prompt as grounded context.

The *grounding guard* lives with the caller: if prefetch returns no usable data,
the local chain must say "I couldn't check" rather than answer ungrounded.
"""

from __future__ import annotations

import asyncio
import re
import subprocess

from nixadmin.log import get_logger
from nixadmin.sdk import Fetcher, Module

log = get_logger(__name__)

MAX_FETCHER_OUTPUT_CHARS = 6_000
MAX_PREFETCH_CHARS = 16_000
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


async def prefetch(modules: list[Module]) -> str:
    """Run every fetcher of every matched module in parallel; format the output.

    Returns an empty string if there are no fetchers, so the caller can apply the
    grounding guard uniformly.
    """
    selected = [(module, fetcher) for module in modules for fetcher in module.fetchers]
    if not selected:
        return ""

    results = await asyncio.gather(*(_run(fetcher) for _, fetcher in selected))
    sections = [
        _format_result(module, fetcher, output)
        for (module, fetcher), output in zip(selected, results, strict=True)
    ]
    return _truncate("\n\n".join(sections), MAX_PREFETCH_CHARS, "prefetch context")


def _format_result(module: Module, fetcher: Fetcher, output: str) -> str:
    """Format bounded context without exposing the module-authored shell command."""
    label = f"{module.name}/{fetcher.name}"
    if fetcher.description:
        label += f" — {fetcher.description}"
    cleaned = _ANSI_ESCAPE.sub("", output).replace("\x00", "")
    bounded = _truncate(cleaned, MAX_FETCHER_OUTPUT_CHARS, label)
    return f"## {label}\n{bounded}"


def _truncate(text: str, limit: int, source: str) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n[… {omitted} characters omitted from {source} …]"


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
