"""Conversation history — keyed by session.

v1 ships only :class:`NullHistory` (stateless). The :class:`HistoryBackend`
Protocol and the write points in the chains exist now so that adding a real
backend later (sqlite, vector) is a config change, not surgery.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nixadmin.errors import ConfigError

Message = dict[str, str]  # {"role": "user"|"assistant", "content": "..."}


@runtime_checkable
class HistoryBackend(Protocol):
    """Per-session conversation store. All methods are async and session-scoped."""

    async def append(self, session: str, role: str, content: str) -> None: ...

    async def recent(self, session: str, n: int) -> list[Message]: ...


class NullHistory:
    """Stateless no-op backend. Appends vanish; ``recent`` is always empty."""

    async def append(self, session: str, role: str, content: str) -> None:
        return None

    async def recent(self, session: str, n: int) -> list[Message]:
        return []


def make_history(kind: str) -> HistoryBackend:
    """Construct the configured history backend (``services.nixadmin.history``)."""
    if kind == "null":
        return NullHistory()
    # "sqlite" / "vector" land later; fail loud rather than silently degrade.
    raise ConfigError(f"unknown history backend: {kind!r}")
