"""Smoke tests for the action tier — parsing and the pure package editor.

The full worktree-validated executor is exercised live (it needs git + nix); here
we cover the deterministic, pure pieces.
"""

from __future__ import annotations

import pytest

from nixadmin.actions import edit_packages, parse_action, sanitize_attr
from nixadmin.errors import NixadminError

SAMPLE = """\
{ pkgs, ... }:
{
  home.packages = with pkgs; [
    firefox
    git
  ];
}
"""


@pytest.mark.parametrize("text,kind,target", [
    ("install steam", "install_app", "steam"),
    ("add spotify please", "install_app", "spotify"),
    ("install chrome", "install_app", "google-chrome"),  # alias
    ("remove gimp", "remove_app", "gimp"),
    ("uninstall the gimp", "remove_app", "gimp"),         # alias + 'the'
    ("enable bluetooth", "toggle", "enable bluetooth"),
])
def test_parse_action(text, kind, target):
    a = parse_action(text)
    assert a is not None
    assert a.kind == kind
    assert a.target == target


@pytest.mark.parametrize("text", ["is my wifi working?", "what apps are installed?"])
def test_parse_action_none_for_questions(text):
    assert parse_action(text) is None


def test_edit_add_inserts_before_close():
    out = edit_packages(SAMPLE, "steam", add=True)
    assert "    steam\n" in out
    assert out.index("steam") < out.index("];")


def test_edit_add_idempotent_when_present():
    assert edit_packages(SAMPLE, "firefox", add=True) == SAMPLE


def test_edit_remove_deletes_line():
    out = edit_packages(SAMPLE, "git", add=False)
    assert "\n    git\n" not in out
    assert "firefox" in out  # others untouched


def test_edit_remove_absent_raises():
    with pytest.raises(NixadminError, match="not in the package list"):
        edit_packages(SAMPLE, "vim", add=False)


def test_edit_no_list_raises():
    with pytest.raises(NixadminError, match="home.packages"):
        edit_packages("{ }:\n{ }\n", "steam", add=True)


@pytest.mark.parametrize("raw,expected", [
    ("steam", "steam"),
    ("  Steam.\n", "Steam"),            # trimmed + punctuation stripped (case preserved)
    ("`firefox`", "firefox"),
    ("google-chrome", "google-chrome"),
    ("did you mean steam", "did"),       # takes first token only
    ("unknown", ""),                      # explicit unknown rejected
    ("foo; rm -rf /", ""),                # shell metachars → rejected
])
def test_sanitize_attr(raw, expected):
    # The filter only guarantees a single well-formed token; the real safety net is
    # the worktree `nix eval` (a bogus name simply fails to evaluate).
    assert sanitize_attr(raw) == expected


def test_sanitize_rejects_injection_chars():
    for bad in ["foo; rm -rf /", "a b", "$(evil)", "../x", ""]:
        out = sanitize_attr(bad)
        assert out == "" or out.replace("-", "").replace("_", "").replace(".", "").isalnum()
