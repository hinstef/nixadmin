"""Typed validation at the HTTP-to-daemon boundary."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

MAX_REQUEST_BODY_BYTES = 64 * 1024
MAX_QUERY_CHARS = 4_000


class RequestError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class QuerySpec:
    qid: str
    text: str
    session_id: str

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> QuerySpec:
        text = (query.get("text") or [""])[0].strip()
        if not text:
            raise RequestError(400, "empty query")
        if len(text) > MAX_QUERY_CHARS:
            raise RequestError(413, "query too large")
        qid = (query.get("qid") or [""])[0].strip() or uuid.uuid4().hex[:12]
        session_id = (query.get("session") or ["web"])[0].strip() or "web"
        return cls(qid=qid, text=text, session_id=session_id)


@dataclass(frozen=True, slots=True)
class UnitSpec:
    unit: str
    scope: str

    @classmethod
    def from_body(cls, body: dict[str, object]) -> UnitSpec:
        unit = str(body.get("unit", "")).strip()
        scope = str(body.get("scope", "system"))
        if not unit:
            raise RequestError(400, "unit is required")
        if scope not in ("system", "user"):
            raise RequestError(400, "scope must be 'system' or 'user'")
        return cls(unit=unit, scope=scope)


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    payload: dict[str, object]

    @classmethod
    def error(cls, error: RequestError) -> HttpResult:
        return cls(error.status, {"error": str(error)})


def content_length(value: str | None) -> int:
    try:
        length = int(value or "0")
    except ValueError as error:
        raise RequestError(400, "invalid content length") from error
    if length < 0:
        raise RequestError(400, "invalid content length")
    if length > MAX_REQUEST_BODY_BYTES:
        raise RequestError(413, "request body too large")
    return length


def json_object(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(raw or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestError(400, "invalid JSON body") from error
    if not isinstance(parsed, dict):
        raise RequestError(400, "JSON body must be an object")
    return parsed
