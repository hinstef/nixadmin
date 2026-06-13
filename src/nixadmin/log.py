"""Logging convention — structured logs via structlog.

**Libraries never configure logging.** Every module gets its logger with::

    from nixadmin.log import get_logger
    log = get_logger(__name__)
    log.info("module loaded", module="network", fetchers=3)

Only the daemon entrypoint calls :func:`configure` once, choosing the renderer:

* ``"json"``    — one JSON object per line. Default for the service; journald
                  stores it and ``journalctl -o cat | jq`` stays queryable.
* ``"console"`` — colourised human output for development.

Per-query context is bound with :func:`bind` / :func:`clear` (backed by
contextvars), so a ``query_id`` bound at dispatch appears on every downstream log
line — router, chain, tools — without being passed through call signatures::

    bind(query_id="q1", session="s1", chain="local")
    ...                       # every log.* below carries those keys
    clear()
"""

from __future__ import annotations

from typing import Any, Literal

import structlog

Renderer = Literal["json", "console"]


def configure(renderer: Renderer = "json", level: str = "INFO") -> None:
    """Set up structlog. Call once, from the daemon entrypoint only."""
    last_processor = (
        structlog.processors.JSONRenderer()
        if renderer == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # inject bound per-query context
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            last_processor,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_to_int(level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger for a module. Safe to call before :func:`configure`."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind(**kwargs: Any) -> None:
    """Bind key/values onto the current context (e.g. per-query at dispatch)."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear() -> None:
    """Clear all bound context (e.g. when a query finishes)."""
    structlog.contextvars.clear_contextvars()


def _level_to_int(level: str) -> int:
    import logging

    return getattr(logging, level.upper(), logging.INFO)
