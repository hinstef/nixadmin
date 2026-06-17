"""Smoke tests for routing — mutation detection and two-stage resolution."""

from __future__ import annotations

import pytest

from nixadmin.routing import Decision, detect_mutation, resolve, resolve_desired_chain
from nixadmin.sdk import SPEC_VERSION, Module


def _mod(name: str, routing: str) -> Module:
    return Module(spec_version=SPEC_VERSION, name=name, description="x", routing=routing)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["install firefox", "please fix my wifi", "turn off bluetooth",
                                  "remove gimp", "update the system"])
def test_detect_mutation_true(text):
    assert detect_mutation(text) is True


@pytest.mark.parametrize("text", ["what apps are installed?", "is my wifi working?",
                                  "how much disk space do I have", "are any services failing"])
def test_detect_mutation_false(text):
    assert detect_mutation(text) is False


@pytest.mark.parametrize("text", ["can you install new apps?", "could you remove gimp?",
                                  "how do I enable bluetooth?", "is it possible to update?",
                                  "do you update the system?"])
def test_capability_questions_are_not_mutations(text):
    """Interrogative phrasing mentioning an action verb is a question, not a write."""
    assert detect_mutation(text) is False


def test_stage1_explicit_wins_and_pins():
    chain, pinned = resolve_desired_chain(explicit="local", matched=[], default_chain="remote")
    assert chain == "local" and pinned is True


def test_stage1_privacy_hint_pins_local():
    chain, pinned = resolve_desired_chain(
        explicit=None, matched=[_mod("vault", "local"), _mod("apps", "auto")],
        default_chain="remote")
    assert chain == "local" and pinned is True  # local > auto


def test_stage1_falls_back_to_default():
    chain, pinned = resolve_desired_chain(explicit=None, matched=[_mod("apps", "auto")],
                                          default_chain="remote")
    assert chain == "remote" and pinned is False


def test_stage2_remote_ready_proceeds():
    d = resolve(desired="remote", pinned_local=False, local_ready=True, remote_ready=True)
    assert d == Decision("remote", "proceed", False)


def test_stage2_remote_down_offers_local():
    d = resolve(desired="remote", pinned_local=False, local_ready=True, remote_ready=False)
    assert d.action == "confirm_remote" and d.chain == "local"


def test_stage2_pinned_local_warming_requires_consent_to_leave_device():
    d = resolve(desired="local", pinned_local=True, local_ready=False, remote_ready=True)
    assert d.action == "confirm_remote"
    assert "leave this device" in d.message


def test_stage2_local_only_machine_waits():
    d = resolve(desired="local", pinned_local=True, local_ready=False, remote_ready=False)
    assert d.action == "wait_local"
