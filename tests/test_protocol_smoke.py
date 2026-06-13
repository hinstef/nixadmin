"""Smoke tests for the wire protocol — round-trips and edge cases.

These guard the daemon↔client contract. Deeper per-field unit tests come later;
for now we just prove encode/decode is symmetric and the obvious failure modes
behave.
"""

from __future__ import annotations

import pytest

from nixadmin import protocol as p


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


def test_decode_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown message type"):
        p.decode('{"type": "nonsense", "id": "x"}')


def test_decode_ignores_extra_fields():
    """Forward-compat: a newer daemon adding fields must not break an old client."""
    msg = p.decode('{"type": "done", "id": "q1", "future_field": 42}')
    assert msg == p.Done(id="q1")


def test_hello_defaults_version():
    assert p.Hello(chains=[], ready={}, default_chain="remote", modules=[]).version == p.VERSION
