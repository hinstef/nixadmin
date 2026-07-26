"""Smoke tests for the web view — the security gate (pure) and page rendering.

The HTTP wiring and daemon round-trips are exercised live; here we pin down the
checks that must never regress: token, Host, and Origin gating.
"""

from __future__ import annotations

from nixadmin.web import page, security
from nixadmin.web.dclient import Daemon

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
    assert "nixadmin — system health" in html


def test_page_has_hub_sections_and_timeline_wiring():
    """The hub is a two-section page (Now + Timeline) that reads the event store."""
    html = page.render(security.new_token())
    assert ">Now<" in html and ">Timeline<" in html
    assert "/api/timeline" in html          # timeline is fetched, not baked in
    assert 'PARAMS.get("explain")' in html  # tray deep-link path exists


def test_daemon_client_graceful_when_socket_absent(tmp_path):
    d = Daemon(str(tmp_path / "nope.sock"))
    assert d.list_failures() is None       # unreachable → None, not a crash
    assert d.journal("x.service", "user") is None
    assert d.timeline() == []              # unreachable → empty, not a crash
