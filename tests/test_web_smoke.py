"""Smoke tests for the web view — the security gate (pure) and page rendering.

The HTTP wiring and daemon round-trips are exercised live; here we pin down the
checks that must never regress: token, Host, and Origin gating.
"""

from __future__ import annotations

import pytest

from nixadmin import protocol as wire
from nixadmin.web import page, security
from nixadmin.web.dclient import Daemon
from nixadmin.web.server import _to_sse
from nixadmin.web.session import QuerySession, sse

PORT = 7677


def test_token_ok():
    tok = security.new_token()
    assert security.token_ok(tok, tok)
    assert not security.token_ok("wrong", tok)
    assert not security.token_ok(None, tok)
    assert not security.token_ok(tok, "")


def test_new_token_is_unguessable_and_unique():
    a, b = security.new_token(), security.new_token()
    assert a != b
    assert len(a) >= 32


def test_host_ok_only_loopback_on_our_port():
    assert security.host_ok(f"127.0.0.1:{PORT}", PORT)
    assert security.host_ok(f"localhost:{PORT}", PORT)
    assert security.host_ok("127.0.0.1", PORT)          # no port is fine
    assert not security.host_ok(f"127.0.0.1:{PORT + 1}", PORT)  # wrong port
    assert not security.host_ok(f"evil.example.com:{PORT}", PORT)  # DNS-rebind attempt
    assert not security.host_ok(None, PORT)


def test_origin_ok_get_vs_mutation():
    # safe GETs: missing Origin allowed, loopback allowed, cross-site refused
    assert security.origin_ok(None, PORT, require=False)
    assert security.origin_ok(f"http://127.0.0.1:{PORT}", PORT, require=False)
    assert not security.origin_ok("https://evil.example.com", PORT, require=False)
    assert not security.origin_ok(f"http://127.0.0.1:{PORT + 1}", PORT, require=False)
    # mutations: a missing Origin is refused (blocks form-based CSRF)
    assert not security.origin_ok(None, PORT, require=True)
    assert security.origin_ok(f"http://localhost:{PORT}", PORT, require=True)
    assert not security.origin_ok("http://attacker", PORT, require=True)


def test_page_embeds_token_and_no_placeholder_leaks():
    tok = security.new_token()
    html = page.render(tok)
    assert tok in html
    assert "__NIXADMIN_TOKEN__" not in html
    assert "<title>nixadmin</title>" in html


def test_page_has_hub_sections_and_timeline_wiring():
    """The document stays structural; behavior lives in packaged ES modules."""
    html = page.render(security.new_token())
    app = (page.asset("app.js") or ("", b""))[1].decode()
    timeline = (page.asset("timeline.js") or ("", b""))[1].decode()
    assert ">Now<" in html and ">Activity<" in html
    assert "/api/timeline" in timeline
    assert 'params.get("explain")' in app
    assert 'type="module"' in html


def test_daemon_client_graceful_when_socket_absent(tmp_path):
    d = Daemon(str(tmp_path / "nope.sock"))
    assert d.list_failures() is None       # unreachable → None, not a crash
    assert d.journal("x.service", "user") is None
    assert d.timeline() == ([], None)      # unreachable → empty, not a crash


def test_page_has_invoke_bar():
    """The hub carries the invoke bar and its streaming client."""
    html = page.render(security.new_token())
    assert 'id="ask"' in html and "What would you like?" in html
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert "/api/stream" in (page.asset("api.js") or ("", b""))[1].decode()
    assert "EventSource" in operation


def test_invoke_bar_comes_before_the_status_sections():
    """Single pane of glass: the prompt is the first thing on the page, not a
    control buried under the health readout."""
    html = page.render(security.new_token())
    body = html[html.index("<main>"):]
    assert body.index('id="ask"') < body.index('id="kept-sec"') < body.index(">Now<")


def test_common_action_chips_are_seeded_prompts_only():
    """A chip must not be a second path to the daemon. It either runs a query the
    user could have typed, or fills the box — never its own API call."""
    app = (page.asset("app.js") or ("", b""))[1].decode()
    assert 'fill: "install "' in app and 'run: "is anything broken?"' in app


def test_replies_stack_and_stay_capped():
    """Cards accumulate (a slow install stays visible) but the stack is bounded —
    a working record, not the chat transcript docs/ux.md rules out."""
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert "MAX_CARDS = 6" in operation and "container.prepend(card)" in operation
    assert "trimCards" in operation


def test_running_query_can_be_stopped():
    """The cancel endpoint is reachable from the UI, not just from the wire."""
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert 'control("/api/cancel"' in operation


def test_timeline_is_five_rows_cursor_paginated():
    timeline = (page.asset("timeline.js") or ("", b""))[1].decode()
    html = page.render(security.new_token())
    assert "PAGE_SIZE = 5" in timeline
    assert 'query.set("before"' in timeline
    assert 'id="older"' in html and 'id="newer"' in html
    assert "New activity" in html


def test_operations_use_progressive_disclosure_and_phases():
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert 'el("details")' in operation and '"Details"' in operation
    assert "Waiting for approval" in operation
    assert "Applying the change" in operation
    assert "Verifying the result" in operation


def test_assets_reject_traversal_and_unknown_types():
    assert page.asset("styles.css") is not None
    assert page.asset("../config.py") is None
    assert page.asset("page.html") is None


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
    s = QuerySession(str(tmp_path / "nope.sock"), "q1", "hi", "web")
    with pytest.raises(OSError):
        s.open()


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

    session = QuerySession(sock_path, "qid1", "install hello", "web")
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
