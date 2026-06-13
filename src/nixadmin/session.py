"""Per-session state and serialization.

Two concerns, both keyed by ``session`` and both in-memory (cleared on restart):

* :class:`SessionState` — scratch facts that are *not* conversation history but
  must persist across turns. Notably the safety gate's "a ``test`` succeeded
  earlier this session" flag. Always present, never nullable.
* a per-session :class:`asyncio.Lock` — enforces **one in-flight query per
  session** so history appends and scratch state can't interleave. Different
  sessions still run concurrently.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class SessionState:
    """Mutable scratch state for a single conversation session."""

    last_test_ok: bool = False
    last_test_at: float | None = None
    #: Remembered routing choices, for a future "remember-my-choice" UX.
    remembered: dict[str, str] = field(default_factory=dict)

    def record_test(self, ok: bool) -> None:
        self.last_test_ok = ok
        self.last_test_at = time.monotonic()


class SessionRegistry:
    """Owns per-session state and locks. Lazily creates entries on first use."""

    def __init__(self) -> None:
        self._state: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def state(self, session: str) -> SessionState:
        st = self._state.get(session)
        if st is None:
            st = self._state[session] = SessionState()
        return st

    def lock(self, session: str) -> asyncio.Lock:
        lk = self._locks.get(session)
        if lk is None:
            lk = self._locks[session] = asyncio.Lock()
        return lk
