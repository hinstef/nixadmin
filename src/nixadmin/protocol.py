"""Wire protocol — the contract between the daemon and its clients.

Transport is newline-delimited JSON over a Unix socket. Each line is one message,
tagged by a ``"type"`` field. This module is **stdlib-only on purpose**: a client
(terminal, GTK, web bridge) depends on *only* this file and never pulls in the
daemon's heavy dependencies.

Usage is symmetric — both sides use :func:`encode` to serialize and :func:`decode`
to parse:

    sock.sendall(encode(Query(id="q1", text="is my wifi working?")).encode())
    msg = decode(line)
    if isinstance(msg, Delta):
        print(msg.text, end="")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import ClassVar

from nixadmin.errors import ProtocolError

#: Protocol version, sent in :class:`Hello`. On mismatch a client warns and
#: proceeds (best-effort) rather than disconnecting — see the spec.
VERSION = 1


# --------------------------------------------------------------------------- #
# Client → Daemon
# --------------------------------------------------------------------------- #


@dataclass
class Query:
    """Ask a question or request an action."""

    id: str
    text: str
    session: str = "default"
    chain: str | None = None  # "local" | "remote"; None = daemon decides
    TYPE: ClassVar[str] = "query"


@dataclass
class Cancel:
    """Abort an in-flight query by id."""

    id: str
    TYPE: ClassVar[str] = "cancel"


@dataclass
class Respond:
    """Answer a pending ``confirm`` (use ``confirmed``) or ``input`` (use ``value``)."""

    id: str
    confirmed: bool | None = None
    value: str | None = None
    TYPE: ClassVar[str] = "respond"


# --------------------------------------------------------------------------- #
# Daemon → Client
# --------------------------------------------------------------------------- #


@dataclass
class Hello:
    """First message on connect. Advertises capabilities and readiness."""

    chains: list[str]
    ready: dict[str, bool]
    default_chain: str
    modules: list[str]
    version: int = VERSION
    TYPE: ClassVar[str] = "hello"


@dataclass
class Delta:
    """A chunk of streamed assistant text for a query."""

    id: str
    text: str
    TYPE: ClassVar[str] = "delta"


@dataclass
class Status:
    """Non-blocking progress note (e.g. "local model warming up…"). No response."""

    id: str
    text: str
    TYPE: ClassVar[str] = "status"


@dataclass
class Done:
    """Terminal success for a query. ``chain``/``model`` may be absent if the
    query was cancelled before a chain was chosen."""

    id: str
    chain: str | None = None
    model: str | None = None
    TYPE: ClassVar[str] = "done"


@dataclass
class Error:
    """Terminal failure for a query. Partial deltas already shown remain."""

    id: str
    text: str
    TYPE: ClassVar[str] = "error"


@dataclass
class Confirm:
    """Blocking yes/no prompt. Client replies with ``Respond(confirmed=…)``."""

    id: str
    text: str
    TYPE: ClassVar[str] = "confirm"


@dataclass
class Input:
    """Blocking free-text prompt. Client replies with ``Respond(value=…)``."""

    id: str
    prompt: str
    TYPE: ClassVar[str] = "input"


@dataclass
class Ready:
    """A chain became available after starting unready (warming up). Broadcast."""

    chain: str
    TYPE: ClassVar[str] = "ready"


@dataclass
class Event:
    """Unsolicited monitor event, broadcast to all clients (not tied to a query)."""

    source: str
    severity: str
    text: str
    TYPE: ClassVar[str] = "event"


@dataclass
class ListFailures:
    """Client → daemon: request the current failed units as structured data."""

    id: str
    TYPE: ClassVar[str] = "list_failures"


@dataclass
class Failures:
    """Daemon → client: the current failed units. Each entry is
    ``{"unit": …, "scope": "system"|"user", "description": …}``."""

    id: str
    units: list[dict[str, str]]
    TYPE: ClassVar[str] = "failures"


@dataclass
class RestartUnit:
    """Client → daemon: restart one already-resolved unit (a tray "fix it"). The
    client names the exact ``unit`` and ``scope`` from a prior :class:`Failures`,
    so no natural-language matching is involved — the deterministic core acts
    directly. The daemon replies with the usual ``Status``/``Delta``/``Done``."""

    id: str
    unit: str
    scope: str  # "system" | "user"
    TYPE: ClassVar[str] = "restart_unit"


# --------------------------------------------------------------------------- #
# (de)serialization
# --------------------------------------------------------------------------- #

Message = (
    Query | Cancel | Respond | Hello | Delta | Status | Done | Error
    | Confirm | Input | Ready | Event | ListFailures | Failures | RestartUnit
)

_REGISTRY: dict[str, type] = {
    cls.TYPE: cls
    for cls in (
        Query, Cancel, Respond, Hello, Delta, Status, Done, Error,
        Confirm, Input, Ready, Event, ListFailures, Failures, RestartUnit,
    )
}


def encode(msg: Message) -> str:
    """Serialize a message to a single newline-terminated JSON line."""
    data = {"type": msg.TYPE}
    for f in fields(msg):
        value = getattr(msg, f.name)
        if value is not None:  # omit unset optionals (e.g. Done.chain on cancel)
            data[f.name] = value
    return json.dumps(data) + "\n"


def decode(line: str) -> Message:
    """Parse one JSON line into its message dataclass.

    Raises :class:`~nixadmin.errors.ProtocolError` for any malformed input —
    invalid JSON, unknown type, or missing required fields — so callers catch one
    exception type and skip junk rather than crash on three different ones.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ProtocolError(f"expected a JSON object, got {type(raw).__name__}")

    type_ = raw.pop("type", None)
    cls = _REGISTRY.get(type_)
    if cls is None:
        raise ProtocolError(f"unknown message type: {type_!r}")

    known = {f.name for f in fields(cls)}
    try:
        msg: Message = cls(**{k: v for k, v in raw.items() if k in known})
    except TypeError as e:  # missing required field
        raise ProtocolError(f"malformed {type_!r} message: {e}") from e
    return msg
