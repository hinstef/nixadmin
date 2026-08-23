"""Streaming web query sessions and SSE mapping."""

from __future__ import annotations

import threading

import pytest

from nixadmin import protocol as wire
from nixadmin.web.requests import QuerySpec
from nixadmin.web.server import _to_sse
from nixadmin.web.session import QuerySession, sse


def test_sse_frame_format():
    frame = sse("delta", {"text": "hi"})
    assert frame == b'event: delta\ndata: {"text": "hi"}\n\n'


def test_to_sse_maps_messages():
    assert b"event: delta" in (_to_sse(wire.Delta(id="x", text="hi")) or b"")
    assert b"event: status" in (_to_sse(wire.Status(id="x", text="…")) or b"")
    assert b"event: confirm" in (_to_sse(wire.Confirm(id="x", text="ok?")) or b"")
    assert b"event: done" in (_to_sse(wire.Done(id="x")) or b"")
    # Daemon errors are named "failed" (EventSource reserves "error").
    assert b"event: failed" in (_to_sse(wire.Error(id="x", text="boom")) or b"")
    # Non-invoke messages are skipped.
    assert _to_sse(wire.Hello(chains=[], ready={}, default_chain="local", modules=[])) is None


def test_query_session_graceful_when_socket_absent(tmp_path):
    s = QuerySession(str(tmp_path / "nope.sock"), QuerySpec("q1", "hi", "web"))
    with pytest.raises(OSError):
        s.open()


@pytest.mark.parametrize("payload", [b"not-json\n", b""])
def test_query_session_closes_on_bad_handshake(tmp_path, payload):
    import socket as _socket

    sock_path = str(tmp_path / "bad-daemon.sock")
    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    def bad_daemon() -> None:
        conn, _ = server.accept()
        with conn:
            if payload:
                conn.sendall(payload)

    worker = threading.Thread(target=bad_daemon)
    worker.start()
    session = QuerySession(sock_path, QuerySpec("qid", "hello", "web"))
    try:
        with pytest.raises(wire.ProtocolError):
            session.open()
        assert session._closed
        assert session._sock is not None and session._sock.fileno() == -1
    finally:
        worker.join(timeout=1)
        server.close()


def test_query_session_confirm_round_trip(tmp_path):
    """Drive a full confirm exchange against a fake daemon over a real socket."""
    import socket as _socket
    import threading

    sock_path = str(tmp_path / "d.sock")
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    seen: dict[str, object] = {}

    def fake_daemon() -> None:
        conn, _ = srv.accept()
        with conn, conn.makefile("r") as f:
            conn.sendall(wire.encode(wire.Hello(
                chains=["local"], ready={"local": True},
                default_chain="local", modules=[])).encode())
            query = wire.decode(f.readline().strip())
            seen["query"] = query
            conn.sendall(wire.encode(wire.Confirm(id=query.id, text="Apply?")).encode())  # type: ignore[attr-defined]
            resp = wire.decode(f.readline().strip())
            seen["respond"] = resp
            conn.sendall(wire.encode(wire.Delta(id=query.id, text="done")).encode())  # type: ignore[attr-defined]
            conn.sendall(wire.encode(wire.Done(id=query.id, chain="local")).encode())  # type: ignore[attr-defined]

    t = threading.Thread(target=fake_daemon)
    t.start()

    session = QuerySession(sock_path, QuerySpec("qid1", "install hello", "web"))
    session.open()
    kinds: list[str] = []
    for msg in session.messages():
        kinds.append(msg.TYPE)
        if isinstance(msg, wire.Confirm):
            session.answer(confirmed=True)   # would come from POST /api/respond
    t.join(timeout=5)
    srv.close()

    assert kinds == ["confirm", "delta", "done"]
    assert isinstance(seen["query"], wire.Query) and seen["query"].text == "install hello"
    assert isinstance(seen["respond"], wire.Respond) and seen["respond"].confirmed is True
    assert seen["query"].id == "qid1"  # daemon query id == browser qid
