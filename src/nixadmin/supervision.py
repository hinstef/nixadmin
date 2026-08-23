"""Minimal systemd readiness/watchdog notification using the stdlib."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping


def notify(message: str, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    address = values.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
        return True
    except OSError:
        return False


def watchdog_interval(env: Mapping[str, str] | None = None) -> float | None:
    """Return a safe heartbeat interval, or None outside a watchdog service."""
    values = os.environ if env is None else env
    try:
        usec = int(values.get("WATCHDOG_USEC", "0"))
        owner = int(values.get("WATCHDOG_PID", str(os.getpid())))
    except ValueError:
        return None
    if usec <= 0 or owner != os.getpid():
        return None
    return max(0.1, usec / 2_000_000)
