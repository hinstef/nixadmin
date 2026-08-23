"""Event store — the persistent system-event timeline.

An append-oriented log of what the daemon *observed* and *did*: a unit failed, an
explanation was generated, a restart was attempted (and whether it stuck), a
journal was snapshotted, a monitor fired. This is the observability substrate —
what makes the machine's recent history legible in the web hub, and the record a
future autofix engine reads (what already failed, what was already tried) and
writes (what it changed).

Distinct from :mod:`nixadmin.history` on purpose: that stores *conversation* turns
keyed by session; this stores *system events* on one timeline. Different shape,
different lifetime, different readers.

Backed by ``sqlite3`` (stdlib — no new dependency, and queryable by unit / kind /
time, which a flat log is not). Every call runs the blocking SQLite work in a
worker thread so the daemon's event loop never stalls. ``NullStore`` is the
no-op backend for tests and for a machine that opts out of persistence.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nixadmin.errors import ConfigError
from nixadmin.log import get_logger

log = get_logger(__name__)

#: An event as handed back to callers / put on the wire. ``ts`` is unix epoch
#: seconds; ``meta`` is the decoded JSON blob (``{}`` when absent).
Event = dict[str, Any]

#: Recognised event kinds. Kept open (plain strings) so a module or the autofix
#: tier can add its own without a schema migration — these are the ones the daemon
#: writes today.
KINDS = frozenset({
    "failure_observed", "failure_cleared", "explanation",
    "restart", "journal_snapshot", "monitor_event",
    "ask", "action", "autofix",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL    NOT NULL,
    kind     TEXT    NOT NULL,
    unit     TEXT,
    scope    TEXT,
    severity TEXT,
    text     TEXT    NOT NULL DEFAULT '',
    meta     TEXT
);
CREATE INDEX IF NOT EXISTS events_ts   ON events (ts);
CREATE INDEX IF NOT EXISTS events_unit ON events (unit);
"""

_COLUMNS = ("id", "ts", "kind", "unit", "scope", "severity", "text", "meta")
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000


@runtime_checkable
class Store(Protocol):
    """Append-only event timeline. Both methods are async and never raise on the
    hot path — a persistence failure degrades observability, it must not take down
    a query or an explanation."""

    async def append(
        self, kind: str, *, unit: str | None = None, scope: str | None = None,
        severity: str | None = None, text: str = "", meta: dict[str, Any] | None = None,
    ) -> int: ...

    async def recent(
        self, limit: int = 100, *, unit: str | None = None,
        kind: str | None = None, since: float | None = None,
        before_id: int | None = None,
    ) -> list[Event]: ...

    async def earliest(self) -> float | None:
        """Timestamp of the oldest event, or ``None`` if the store is empty. Cheap
        (``MIN(ts)``, indexed) — used as the ledger's honest streak floor when a
        capped ``recent`` scan can't see back far enough."""
        ...

    async def prune(self, before: float) -> int: ...

    async def aclose(self) -> None: ...


class NullStore:
    """No-op backend. Appends vanish; ``recent`` is always empty."""

    async def append(
        self, kind: str, *, unit: str | None = None, scope: str | None = None,
        severity: str | None = None, text: str = "", meta: dict[str, Any] | None = None,
    ) -> int:
        return 0

    async def recent(
        self, limit: int = 100, *, unit: str | None = None,
        kind: str | None = None, since: float | None = None,
        before_id: int | None = None,
    ) -> list[Event]:
        return []

    async def earliest(self) -> float | None:
        return None

    async def prune(self, before: float) -> int:
        return 0

    async def aclose(self) -> None:
        return None


class EventStore:
    """SQLite-backed timeline. One connection, shared across the worker threads
    (``check_same_thread=False``) and serialised by an :class:`asyncio.Lock` so
    concurrent appends can't interleave a write. WAL keeps a reader (a ``recent``
    query) from blocking behind a writer."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._db.execute("PRAGMA journal_mode=WAL")
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            self._db.close()
            raise ConfigError(
                f"event store schema v{version} is newer than supported v{SCHEMA_VERSION}"
            )
        self._db.executescript(_SCHEMA)
        if version < SCHEMA_VERSION:
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()
        self._lock = asyncio.Lock()
        self._closed = False
        log.info("event store ready", path=str(self.path))

    async def append(
        self, kind: str, *, unit: str | None = None, scope: str | None = None,
        severity: str | None = None, text: str = "", meta: dict[str, Any] | None = None,
    ) -> int:
        ts = time.time()
        if self._closed:
            return 0
        meta_json = json.dumps(meta) if meta else None
        try:
            async with self._lock:
                return await asyncio.to_thread(
                    self._insert, ts, kind, unit, scope, severity, text, meta_json
                )
        except sqlite3.Error as e:  # never let persistence break the caller
            log.warning("event append failed", kind=kind, unit=unit, error=str(e))
            return 0

    def _insert(
        self, ts: float, kind: str, unit: str | None, scope: str | None,
        severity: str | None, text: str, meta_json: str | None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO events (ts, kind, unit, scope, severity, text, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, kind, unit, scope, severity, text, meta_json),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    async def recent(
        self, limit: int = 100, *, unit: str | None = None,
        kind: str | None = None, since: float | None = None,
        before_id: int | None = None,
    ) -> list[Event]:
        if self._closed:
            return []
        try:
            async with self._lock:
                return await asyncio.to_thread(self._query, limit, unit, kind, since, before_id)
        except sqlite3.Error as e:
            log.warning("event query failed", error=str(e))
            return []

    def _query(
        self, limit: int, unit: str | None, kind: str | None, since: float | None,
        before_id: int | None,
    ) -> list[Event]:
        where: list[str] = []
        params: list[Any] = []
        if unit is not None:
            where.append("unit = ?")
            params.append(unit)
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, min(limit, 1000)))
        rows = self._db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM events {clause} ORDER BY ts DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row(r) for r in rows]

    async def earliest(self) -> float | None:
        if self._closed:
            return None
        try:
            async with self._lock:
                return await asyncio.to_thread(self._earliest)
        except sqlite3.Error as e:
            log.warning("event earliest failed", error=str(e))
            return None

    def _earliest(self) -> float | None:
        row = self._db.execute("SELECT MIN(ts) AS m FROM events").fetchone()
        return None if row is None or row["m"] is None else float(row["m"])

    async def prune(self, before: float) -> int:
        """Delete events older than ``before`` and return the number removed."""
        if self._closed:
            return 0
        try:
            async with self._lock:
                return await asyncio.to_thread(self._prune, before)
        except sqlite3.Error as e:
            log.warning("event prune failed", error=str(e))
            return 0

    def _prune(self, before: float) -> int:
        cursor = self._db.execute("DELETE FROM events WHERE ts < ?", (before,))
        self._db.commit()
        self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return max(0, cursor.rowcount)

    @staticmethod
    def _row(r: sqlite3.Row) -> Event:
        ev = dict(r)
        raw = ev.pop("meta", None)
        try:
            decoded = json.loads(raw) if raw else {}
            if not isinstance(decoded, dict):
                raise TypeError("metadata is not an object")
            ev["meta"] = decoded
        except (json.JSONDecodeError, TypeError) as error:
            log.warning("invalid event metadata", event_id=ev.get("id"), error=str(error))
            ev["meta"] = {}
        return ev

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    async def aclose(self) -> None:
        async with self._lock:
            if not self._closed:
                await asyncio.to_thread(self.close)


def make_store(kind: str, state_dir: str | Path) -> Store:
    """Construct the configured event store (``services.nixadmin.events``).

    ``"sqlite"`` writes ``<state_dir>/events.db``; ``"null"`` disables persistence.
    Unknown kinds fail loud rather than silently dropping the timeline.
    """
    if kind == "null":
        return NullStore()
    if kind == "sqlite":
        return EventStore(Path(state_dir) / "events.db")
    raise ConfigError(f"unknown event store backend: {kind!r}")
