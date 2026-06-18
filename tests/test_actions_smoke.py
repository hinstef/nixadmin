"""Smoke tests for the action tier — parsing and the pure package editor.

The full worktree-validated executor is exercised live (it needs git + nix); here
we cover the deterministic, pure pieces.
"""

from __future__ import annotations

import pytest

from nixadmin.actions import edit_packages, fuzzy_candidates, parse_action
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


# A stand-in for the real (huge) nixpkgs name list — fuzzy_candidates is the pure
# part; the model judge over these candidates is exercised live.
FAKE_NAMES = ["steam", "stem", "stemroller", "firefox", "foremost", "chromium",
              "chrome-gnome-shell", "discord", "blender", "vlc", "obs-studio"]


@pytest.mark.parametrize("typo,must_include", [
    ("stem", "steam"),
    ("forefax", "firefox"),
    ("discrod", "discord"),
    ("blendr", "blender"),
])
def test_fuzzy_candidates_surface_the_real_target(typo, must_include):
    cands = fuzzy_candidates(typo, FAKE_NAMES)
    assert must_include in cands  # the real package is among the candidates to judge


def test_fuzzy_candidates_empty_for_nonsense():
    assert fuzzy_candidates("zzqwxyzzz", FAKE_NAMES) == []
