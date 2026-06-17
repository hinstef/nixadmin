"""Smoke tests for the action tier — parsing and the pure package editor.

The full worktree-validated executor is exercised live (it needs git + nix); here
we cover the deterministic, pure pieces.
"""

from __future__ import annotations

import pytest

from nixadmin.actions import edit_packages, parse_action
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
