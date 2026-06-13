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
from dataclasses import dataclass, field, fields
from typing import ClassVar

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
class Event:
    """Unsolicited monitor event, broadcast to all clients (not tied to a query)."""

    source: str
    severity: str
    text: str
    TYPE: ClassVar[str] = "event"


# --------------------------------------------------------------------------- #
# (de)serialization
# --------------------------------------------------------------------------- #

Message = (
    Query | Cancel | Respond | Hello | Delta | Status | Done | Error | Confirm | Input | Event
)

_REGISTRY: dict[str, type] = {
    cls.TYPE: cls
    for cls in (Query, Cancel, Respond, Hello, Delta, Status, Done, Error, Confirm, Input, Event)
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

    Raises ``ValueError`` for unknown or malformed message types so callers can
    skip junk rather than crash.
    """
    raw = json.loads(line)
    type_ = raw.pop("type", None)
    cls = _REGISTRY.get(type_)
    if cls is None:
        raise ValueError(f"unknown message type: {type_!r}")
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})
