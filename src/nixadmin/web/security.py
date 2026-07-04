"""Pure request-gating checks for the local web view — no I/O, easy to test.

Threat model: a localhost HTTP server on a shared-ish desktop. The risks are
(1) another local user or process guessing the URL, (2) a malicious web page in
the user's own browser reaching the server (CSRF / DNS-rebinding). We counter (1)
with an unguessable bearer token and (2) with strict Host + Origin checks.
"""

from __future__ import annotations

import hmac
import secrets

# Origins/Hosts we consider "ourselves". Only loopback — never a hostname that
# could resolve elsewhere (DNS-rebinding defence).
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def new_token() -> str:
    """A fresh, unguessable session token (URL-safe)."""
    return secrets.token_urlsafe(32)


def token_ok(provided: str | None, expected: str) -> bool:
    """Constant-time token comparison; ``None``/empty never matches."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def host_ok(host_header: str | None, port: int) -> bool:
    """Accept only ``<loopback>:<port>`` (or bare loopback). Rejects a spoofed
    Host pointing at a name that resolves back to us (DNS-rebinding)."""
    if not host_header:
        return False
    host, _, hport = host_header.rpartition(":")
    # rpartition leaves host empty if there was no colon → whole thing is the host
    name = host or hport
    port_part = hport if host else ""
    if port_part and port_part != str(port):
        return False
    return name in _LOOPBACK_HOSTS


def origin_ok(origin: str | None, port: int, *, require: bool) -> bool:
    """Validate a request's ``Origin``.

    ``require=False`` (safe GETs): a missing Origin is fine (top-level navigation
    sends none); a present one must be loopback. ``require=True`` (mutations): the
    Origin must be present *and* loopback — a cross-site or origin-less POST is
    refused, which is what stops form-based CSRF."""
    if origin is None or origin == "":
        return not require
    scheme, _, rest = origin.partition("://")
    if scheme not in ("http", "https"):
        return False
    host, _, oport = rest.partition(":")
    if oport and oport != str(port):
        return False
    return host in _LOOPBACK_HOSTS
