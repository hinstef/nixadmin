"""Context assembly — build the remote chain's extra system-prompt text.

Context providers (declared by modules) are called lazily, cached, and re-fetched
when their ``refresh_interval`` elapses. Local chain never uses these — it runs on
the minimal hardcoded prompt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from nixadmin.log import get_logger
from nixadmin.sdk import ContextProvider

log = get_logger(__name__)


@dataclass
class _Cached:
    text: str
    at: float


class ContextCache:
    """Lazily evaluates and caches context providers with per-provider TTL."""

    def __init__(self, providers: list[ContextProvider]) -> None:
        self._providers = providers
        self._cache: dict[str, _Cached] = {}

    async def assemble(self) -> str:
        """Return the concatenated, freshness-respecting context block."""
        parts: list[str] = []
        for provider in self._providers:
            text = await self._get(provider)
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    async def _get(self, provider: ContextProvider) -> str:
        cached = self._cache.get(provider.name)
        if cached is not None and not self._stale(provider, cached):
            return cached.text
        try:
            text = await provider.get()
        except Exception as e:  # noqa: BLE001 — a bad provider must not break the chain
            log.warning("context provider failed", provider=provider.name, error=str(e))
            return cached.text if cached else ""
        self._cache[provider.name] = _Cached(text=text, at=time.monotonic())
        return text

    @staticmethod
    def _stale(provider: ContextProvider, cached: _Cached) -> bool:
        if provider.refresh_interval is None:
            return False  # cache for daemon lifetime
        return (time.monotonic() - cached.at) >= provider.refresh_interval
