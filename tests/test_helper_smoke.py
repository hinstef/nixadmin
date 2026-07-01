"""Smoke tests for the privileged rebuild helper (nix/nixadmin-helper.py).

The helper is a stdlib-only script run as root via writePython3Bin, so it isn't
part of the nixadmin package — we load it by path. The genuinely dangerous parts
(detached systemd-run, surviving a helper restart, journal streaming) are
integration-level and validated live; here we lock down the *pure decision logic*,
which is where a silent regression would do real damage (e.g. reaping a live
rebuild, or reporting a failed switch as success)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HELPER_PATH = Path(__file__).resolve().parent.parent / "nix" / "nixadmin-helper.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("nixadmin_helper", HELPER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # imports clean — no env required at module load
    return mod


helper = _load_helper()


# --- action -> argv -------------------------------------------------------- #

@pytest.mark.parametrize("action", ["switch", "test", "boot"])
def test_build_cmd_uses_flake_target(action):
    cmd = helper.build_cmd(action, "/etc/nixos", "laptop")
    assert cmd == [helper.NIXOS_REBUILD, action, "--flake", "path:/etc/nixos#laptop"]


def test_build_cmd_revert_maps_to_switch_rollback():
    # 'revert' has no nixos-rebuild subcommand — it must become switch --rollback,
    # and must NOT carry a --flake (rollback targets the previous generation).
    cmd = helper.build_cmd("revert", "/etc/nixos", "laptop")
    assert cmd == [helper.NIXOS_REBUILD, "switch", "--rollback"]
    assert "--flake" not in cmd


# --- finished detection (streaming completion) ----------------------------- #

@pytest.mark.parametrize("state,done", [
    ("active", True),       # oneshot+RemainAfterExit success => exited
    ("failed", True),
    ("activating", False),  # still running — must NOT be treated as finished
    ("reloading", False),
    ("inactive", False),    # not yet started
])
def test_unit_is_finished(state, done):
    assert helper.unit_is_finished(state) is done


# --- exit code from unit state --------------------------------------------- #

def test_exit_code_active_is_zero():
    assert helper.exit_code_from("active", "0") == 0


@pytest.mark.parametrize("state,status,expected", [
    ("failed", "1", 1),
    ("failed", "9", 9),     # killed by SIGKILL (watchdog timeout)
    ("failed", "0", 1),     # failed but status 0 -> forced nonzero (never fake success)
    ("failed", "", 1),      # unparseable -> 1
    ("failed", "x", 1),
])
def test_exit_code_failed_is_nonzero(state, status, expected):
    assert helper.exit_code_from(state, status) == expected


# --- reap classification (startup cleanup safety) -------------------------- #

@pytest.mark.parametrize("state,reap", [
    ("active", True),       # finished (exited) leftover — safe to remove
    ("failed", True),
    ("inactive", True),
    ("activating", False),  # a LIVE rebuild — reaping it would kill a switch
    ("reloading", False),
])
def test_is_reapable_never_touches_running(state, reap):
    assert helper.is_reapable(state) is reap


# --- restart-target validation (privileged; must reject junk + self) ------- #

@pytest.mark.parametrize("unit,ok", [
    ("bluetooth.service", True),
    ("cups.socket", True),
    ("systemd-timesyncd.service", True),
    ("dev-disk-by\\x2duuid.mount", True),   # systemd-escaped names contain backslashes
    ("nixadmin-helper.service", False),      # deny-list: never restart ourselves
    ("", False),
    ("bluetooth", False),                    # no unit suffix
    ("evil.service; rm -rf /", False),       # junk / would-be injection
    ("../../etc/passwd", False),
    ("foo.exe", False),                      # unknown suffix
])
def test_valid_unit(unit, ok):
    assert helper.valid_unit(unit) is ok


# --- _send tolerates a dead client ----------------------------------------- #

class _OkWriter:
    def write(self, _b):
        return None


class _BrokenWriter:
    def write(self, _b):
        raise BrokenPipeError


def test_send_returns_true_on_success():
    assert helper._send(_OkWriter(), {"stream": "hi"}) is True


def test_send_returns_false_on_broken_pipe():
    assert helper._send(_BrokenWriter(), {"stream": "hi"}) is False
