"""nixadmin-web — a token-gated localhost detail view for the daemon.

The tray is the glanceable surface; this is the "open detail" one — the honest
home for a journal tail, an explanation, and per-unit actions that don't fit a
menu. It is a **protocol client** like the tray: it talks only to the daemon's
Unix socket and never touches the system directly, so it needs no privileges.

Security is deliberately conservative (see :mod:`nixadmin.web.security`):

* bound to loopback only (127.0.0.1),
* every request carries a per-session bearer token (unguessable, 0600 on disk),
* Host and Origin are checked to defeat DNS-rebinding and cross-site requests,
* mutations (restart) require a matching Origin — it is request-only, never push.
"""
