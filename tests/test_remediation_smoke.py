"""Smoke tests for the remediation tier — the pure parse + unit-matching helpers.
The executor (systemctl restart + verify) is exercised live."""

from __future__ import annotations

import pytest

from nixadmin.remediation import match_unit, parse

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
