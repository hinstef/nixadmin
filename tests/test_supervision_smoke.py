"""Pure and local-socket tests for systemd supervision notifications."""

from __future__ import annotations

import os
import socket

from nixadmin.supervision import notify, watchdog_interval


def test_watchdog_interval_uses_half_manager_deadline():
    env = {"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(os.getpid())}
    assert watchdog_interval(env) == 30.0
    assert watchdog_interval({"WATCHDOG_USEC": "bad"}) is None
    assert watchdog_interval({"WATCHDOG_USEC": "0"}) is None
    assert watchdog_interval({"WATCHDOG_USEC": "1000000", "WATCHDOG_PID": "1"}) is None


def test_notify_sends_datagram_to_manager_socket(tmp_path):
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(path)
        assert notify("READY=1", {"NOTIFY_SOCKET": path})
        assert receiver.recv(100) == b"READY=1"


def test_notify_is_optional_and_failure_safe(tmp_path):
    assert not notify("READY=1", {})
    assert not notify("READY=1", {"NOTIFY_SOCKET": str(tmp_path / "missing")})
