"""nixadmin-tray — a StatusNotifierItem (system-tray) client for the daemon.

Deliberately lightweight: it speaks only :mod:`nixadmin.protocol` to the daemon
(over the same Unix socket the terminal client uses) and ``dbus-fast`` to the
desktop's tray host. No daemon internals, no LLM, no heavy deps.

The tray is the "kept-well ledger" surface (see ``docs/ux.md``): quiet by default,
green when everything is fine, amber when something failed — pull, not push.
"""
