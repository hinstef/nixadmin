"""Small, dependency-free operational health primitives."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

MAX_COUNTER = 2**63 - 1
MAX_RATE_KEYS = 128


class OperationalState:
    """Thread-safe bounded counters and repeated-warning suppression."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._last_log: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def increment(self, name: str) -> None:
        with self._lock:
            self._counters[name] = min(MAX_COUNTER, self._counters.get(name, 0) + 1)

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def should_log(self, key: str, *, interval_s: float = 30.0) -> bool:
        now = time.monotonic()
        with self._lock:
            previous = self._last_log.get(key)
            if previous is not None and now - previous < interval_s:
                return False
            self._last_log[key] = now
            self._last_log.move_to_end(key)
            while len(self._last_log) > MAX_RATE_KEYS:
                self._last_log.popitem(last=False)
            return True
