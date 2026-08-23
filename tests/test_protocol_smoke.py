"""Smoke tests for the wire protocol — round-trips and edge cases.

These guard the daemon↔client contract. Deeper per-field unit tests come later;
for now we just prove encode/decode is symmetric and the obvious failure modes
behave.
"""

from __future__ import annotations

import pytest

from nixadmin import protocol as p
from nixadmin.errors import ProtocolError


def test_roundtrip_every_message_type():
    """Every message survives encode → decode unchanged."""
    samples: list[p.Message] = [
        p.Query(id="q1", text="is my wifi working?"),
        p.Query(id="q2", text="hi", session="s1", chain="local"),
        p.Cancel(id="q1"),
        p.Respond(id="q1", confirmed=True),
        p.Respond(id="q1", value="firefox"),
        p.Hello(chains=["local", "remote"], ready={"local": False, "remote": True},
                default_chain="remote", modules=["apps"]),
        p.Delta(id="q1", text="Yes, "),
        p.Status(id="q1", text="warming up…"),
        p.Done(id="q1", chain="local", model="qwen2.5:3b"),
        p.Error(id="q1", text="backend unavailable"),
        p.Confirm(id="q1", text="Use remote instead?"),
        p.Input(id="q1", prompt="Package name:"),
        p.Event(source="monitor.x", severity="error", text="nginx down"),
        p.ListFailures(id="q1"),
        p.RestartUnit(id="q1", unit="cups.service", scope="system"),
        p.ExplainUnit(id="q1", unit="cups.service", scope="user"),
        p.UnitJournal(id="q1", unit="cups.service", scope="system"),
        p.Journal(id="q1", unit="cups.service", text="line1\nline2"),
        p.GetTimeline(id="q1"),
        p.GetTimeline(id="q2", limit=50, unit="cups.service", before_id=42),
        p.Timeline(id="q1", events=[
            {"id": 3, "ts": 1.0, "kind": "explanation", "unit": "cups.service",
             "scope": "system", "severity": None, "text": "it broke",
             "meta": {"model": "qwen2.5:3b"}},
        ]),
        p.Timeline(id="q2", events=[], next_cursor=12),
        p.GetLedger(id="q1"),
        p.Ledger(id="q1", data={
            "streak_days": 23, "healthy_now": True, "since_ts": 1.0,
            "headline": "Looked after itself for 23 days.",
            "tally": ["quietly restarted 2 services"],
        }),
        p.Ledger(id="q2", data={}),
        p.Failures(id="q1", units=[
            {"unit": "cups.service", "scope": "system", "description": "CUPS printing"},
            {"unit": "nixadmin-backup.service", "scope": "user", "description": "Nightly backup"},
        ]),
        p.Failures(id="q1", units=[]),  # no failures
    ]
    for msg in samples:
        line = p.encode(msg)
        assert line.endswith("\n")
        assert p.decode(line) == msg


def test_encode_omits_unset_optionals():
    """Done without a resolved chain (e.g. cancel) must not emit null fields."""
    line = p.encode(p.Done(id="q1"))
    assert "chain" not in line
    assert "model" not in line
    # round-trips back to defaults
    assert p.decode(line) == p.Done(id="q1")


@pytest.mark.parametrize("bad", [
    '{"type": "nonsense", "id": "x"}',  # unknown type
    '{"type": "query", "id": "x"}',      # missing required field (text)
    "not json at all",                    # invalid JSON
    "[1, 2, 3]",                          # valid JSON, not an object
])
def test_decode_malformed_raises_protocol_error(bad):
    """All malformed input funnels to one catchable exception type."""
    with pytest.raises(ProtocolError):
        p.decode(bad)


def test_decode_ignores_extra_fields():
    """Forward-compat: a newer daemon adding fields must not break an old client."""
    msg = p.decode('{"type": "done", "id": "q1", "future_field": 42}')
    assert msg == p.Done(id="q1")


def test_hello_defaults_version():
    assert p.Hello(chains=[], ready={}, default_chain="remote", modules=[]).version == p.VERSION
