"""Smoke tests for the remediation tier — the pure parse + unit-matching helpers.
The executor (systemctl restart + verify) is exercised live."""

from __future__ import annotations

import pytest

import nixadmin.remediation as remediation
from nixadmin.remediation import Remediation, match_unit, parse

# (unit, active_state, description) triples, as gathered from the live system.
UNITS = [
    ("nixadmin-backup.service", "failed", "Nightly backup"),
    ("pipewire.service", "active", "PipeWire Multimedia Service"),
    ("nixadmin-daemon.service", "active", "nixadmin ambient intelligence daemon"),
]


@pytest.mark.parametrize("text,target", [
    ("restart the backup service", "backup"),
    ("relaunch nixadmin-backup", "nixadmin-backup"),
    ("can you restart the backup service please", "backup"),
    ("reload pipewire", "pipewire"),
])
def test_parse_recognises_restart(text, target):
    r = parse(text)
    assert r is not None
    assert r.kind == "restart_unit"
    assert r.target == target


@pytest.mark.parametrize("text", [
    "how do I restart the backup service?",   # how-to question, not a command
    "are there any errors?",
    "what failed last night?",
])
def test_parse_ignores_non_commands(text):
    assert parse(text) is None


def test_match_by_name_stem():
    assert match_unit("backup", UNITS) == ["nixadmin-backup.service"]


def test_match_by_description_token():
    # "multimedia" only appears in pipewire's description
    assert match_unit("multimedia", UNITS) == ["pipewire.service"]


def test_match_none_for_unknown():
    assert match_unit("nonexistent-thing", UNITS) == []


def test_match_can_be_ambiguous():
    # "nixadmin" matches both nixadmin-* units → caller disambiguates
    assert set(match_unit("nixadmin", UNITS)) == {
        "nixadmin-backup.service", "nixadmin-daemon.service"
    }


# --- scope routing: system units go through the privileged helper ---------- #

async def _yes(_):
    return True


async def _noop(_):
    return None


def _only_system_failing(monkeypatch, unit="cups.service"):
    async def fake_list(scope):
        return [(unit, "failed", "CUPS printing")] if scope == "system" else []
    monkeypatch.setattr(remediation, "_list_units", fake_list)


async def test_system_unit_routes_through_helper(monkeypatch):
    _only_system_failing(monkeypatch)

    async def healthy(_unit, _scope):  # is-failed after restart → not failed
        return False
    monkeypatch.setattr(remediation, "_is_failed", healthy)

    called = {}

    async def fake_restart_system(unit):
        called["unit"] = unit
        return "ok"

    out = await remediation.run(
        Remediation("restart_unit", "cups"),
        confirm=_yes, status=_noop, restart_system=fake_restart_system,
    )
    assert called["unit"] == "cups.service"  # privileged path, not systemctl --user
    assert "healthy again" in out


async def test_system_unit_without_privileged_path_declines(monkeypatch):
    _only_system_failing(monkeypatch)
    out = await remediation.run(
        Remediation("restart_unit", "cups"), confirm=_yes, status=_noop,
    )
    assert "system service" in out.lower()  # can't do it without the helper injected


# --- restart_resolved: the structured (tray) path, no matching/confirm ------ #

async def test_restart_resolved_system_verifies_and_reports(monkeypatch):
    async def healthy(_unit, _scope):
        return False  # is-failed → healthy after restart
    monkeypatch.setattr(remediation, "_is_failed", healthy)
    called = {}

    async def fake_restart_system(unit):
        called["unit"] = unit
        return "ok"

    out = await remediation.restart_resolved(
        "cups.service", "system", status=_noop, restart_system=fake_restart_system,
    )
    assert called["unit"] == "cups.service"  # exact unit, privileged path
    assert "healthy again" in out


async def test_restart_resolved_still_failing_is_honest(monkeypatch):
    async def still_failed(_unit, _scope):
        return True
    async def tail(_unit, _scope):
        return "fatal: disk quota exceeded"
    monkeypatch.setattr(remediation, "_is_failed", still_failed)
    monkeypatch.setattr(remediation, "_unit_tail", tail)

    out = await remediation.restart_resolved(
        "cups.service", "system", status=_noop, restart_system=lambda _u: _ok(),
    )
    assert "still failing" in out
    assert "disk quota exceeded" in out  # honest — shows the real reason, no fake "Done!"


async def _ok():
    return "ok"
