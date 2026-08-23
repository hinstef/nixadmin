"""Loopback web security policy."""

from __future__ import annotations

from nixadmin.web import security

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

