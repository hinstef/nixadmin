"""Smoke tests for the web view — the security gate (pure) and page rendering.

The HTTP wiring and daemon round-trips are exercised live; here we pin down the
checks that must never regress: token, Host, and Origin gating.
"""

from __future__ import annotations

import socket
import threading
from types import SimpleNamespace

import pytest

from nixadmin.web.dclient import Daemon
from nixadmin.web.requests import QuerySpec, RequestError, UnitSpec, json_object
from nixadmin.web.server import (
    MAX_ACTIVE_SESSIONS,
    MAX_REQUEST_BODY_BYTES,
    _content_length,
    _remove_url_file,
    _RequestTooLarge,
    _url_file_lock,
    _WebServer,
    _write_url_file,
)

PORT = 7677


def test_request_body_limit_rejects_oversize_and_negative_lengths():
    assert _content_length(None) == 0
    with pytest.raises(ValueError):
        _content_length("-1")
    with pytest.raises(ValueError):
        _content_length("not-an-int")
    assert _content_length(str(MAX_REQUEST_BODY_BYTES)) == MAX_REQUEST_BODY_BYTES
    with pytest.raises(_RequestTooLarge):
        _content_length(str(MAX_REQUEST_BODY_BYTES + 1))


def test_typed_web_request_validation():
    query = QuerySpec.from_query({"qid": ["q1"], "text": ["  hello  "]})
    assert query == QuerySpec(qid="q1", text="hello", session_id="web")
    assert UnitSpec.from_body({"unit": "cups.service", "scope": "user"}) == UnitSpec(
        unit="cups.service", scope="user",
    )
    assert json_object(b'{"unit":"cups.service"}') == {"unit": "cups.service"}
    with pytest.raises(RequestError, match="empty query"):
        QuerySpec.from_query({})
    with pytest.raises(RequestError, match="scope"):
        UnitSpec.from_body({"unit": "cups.service", "scope": "machine"})
    with pytest.raises(RequestError, match="object"):
        json_object(b"[]")


def test_session_registry_rejects_duplicates_and_caps_capacity(tmp_path):
    server = _WebServer(("127.0.0.1", 0), "token", Daemon(str(tmp_path / "daemon.sock")))

    class Session:
        def __init__(self, qid):
            self.qid = qid

        def cancel(self):
            pass

        def close(self):
            pass

    try:
        assert server.register_session(Session("same")) == "ok"  # type: ignore[arg-type]
        assert server.register_session(Session("same")) == "duplicate"  # type: ignore[arg-type]
        for index in range(1, MAX_ACTIVE_SESSIONS):
            assert server.register_session(Session(str(index))) == "ok"  # type: ignore[arg-type]
        assert server.register_session(Session("overflow")) == "full"  # type: ignore[arg-type]
    finally:
        server.server_close()


def test_web_server_rejects_connections_when_thread_slots_are_full(tmp_path):
    server = _WebServer(("127.0.0.1", 0), "token", Daemon(str(tmp_path / "daemon.sock")))
    client, request = socket.socketpair()
    try:
        for _ in range(32):
            assert server._handler_slots.acquire(blocking=False)
        server.process_request(request, ("127.0.0.1", 1))
        assert b"503 Service Unavailable" in client.recv(1024)
    finally:
        client.close()
        request.close()
        for _ in range(32):
            server._handler_slots.release()
        server.server_close()


def test_server_close_cancels_active_sessions(tmp_path):
    server = _WebServer(("127.0.0.1", 0), "token", Daemon(str(tmp_path / "daemon.sock")))
    calls: list[str] = []
    session = SimpleNamespace(
        qid="active", cancel=lambda: calls.append("cancel"), close=lambda: calls.append("close"),
    )
    assert server.register_session(session) == "ok"  # type: ignore[arg-type]
    server.server_close()
    assert calls == ["cancel", "close"]
    assert not server.sessions


def test_url_file_is_atomic_and_cleanup_is_instance_owned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    old = "http://127.0.0.1:7677/?token=old"
    new = "http://127.0.0.1:7677/?token=new"

    _write_url_file(old)
    assert (tmp_path / "nixadmin-web.url").read_text() == old + "\n"
    assert (tmp_path / "nixadmin-web.url").stat().st_mode & 0o777 == 0o600

    _write_url_file(new)
    _remove_url_file(old)
    assert (tmp_path / "nixadmin-web.url").read_text() == new + "\n"
    _remove_url_file(new)
    assert not (tmp_path / "nixadmin-web.url").exists()


def test_url_file_updates_serialize_across_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    finished = threading.Event()

    def write() -> None:
        _write_url_file("http://127.0.0.1:7677/?token=new")
        finished.set()

    with _url_file_lock():
        worker = threading.Thread(target=write)
        worker.start()
        assert not finished.wait(0.05)
    worker.join(timeout=1)
    assert finished.is_set()


