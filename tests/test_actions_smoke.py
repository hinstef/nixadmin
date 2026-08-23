"""Smoke tests for the action tier — parsing, the pure editor, and the executor's
apply / cancel / revert + audit behaviour (worktree validation mocked, since it
needs real git + nix; the rest of the path is real)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

import nixadmin.actions as actions
from nixadmin.actions import Action, edit_packages, fuzzy_candidates, parse_action
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
    ("install chrome", "install_app", "chrome"),          # judge maps it later
    ("install google chrome", "install_app", "google chrome"),  # multi-word kept intact
    ("remove gimp", "remove_app", "gimp"),
    ("uninstall the gimp", "remove_app", "gimp"),         # filler 'the' dropped
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


PACKAGE_NAME = st.from_regex(r"[a-z][a-z0-9-]{0,15}", fullmatch=True)


@given(
    packages=st.lists(PACKAGE_NAME, unique=True, max_size=20),
    target=PACKAGE_NAME,
    newline=st.sampled_from(["\n", "\r\n"]),
    indent=st.sampled_from(["    ", "      ", "\t"]),
)
def test_edit_add_is_idempotent_and_reversible(packages, target, newline, indent):
    packages = [package for package in packages if package != target]
    body = "".join(f"{indent}{package}{newline}" for package in packages)
    original = (
        f"{{ pkgs, ... }}:{newline}{{{newline}"
        f"  # preserved before{newline}"
        f"  home.packages = with pkgs; [{newline}{body}  ];{newline}"
        f"  # preserved after{newline}}}{newline}"
    )

    added = edit_packages(original, target, add=True)
    assert edit_packages(added, target, add=True) == added
    assert edit_packages(added, target, add=False) == original
    expected_indent = indent if packages else "    "
    assert f"{expected_indent}{target}{newline}" in added


@given(packages=st.lists(PACKAGE_NAME, unique=True, min_size=1, max_size=20))
def test_edit_existing_packages_preserves_unrelated_entries(packages):
    original = "home.packages = with pkgs; [\n" + "".join(
        f"  {package}\n" for package in packages
    ) + "];\n"
    target = packages[0]

    assert edit_packages(original, target, add=True) == original
    removed = edit_packages(original, target, add=False)
    assert target not in {line.strip() for line in removed.splitlines()}
    assert all(package in {line.strip() for line in removed.splitlines()}
               for package in packages[1:])


async def test_prune_abandoned_worktrees_is_marker_and_repo_scoped(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    dead = tmp_path / "nixadmin-wt-dead"
    active = tmp_path / "nixadmin-wt-active"
    foreign = tmp_path / "nixadmin-wt-foreign"
    unrelated = tmp_path / "nixadmin-wt-unmarked"
    for path in (dead, active, foreign, unrelated):
        path.mkdir()
    (dead / actions.WORKTREE_MARKER).write_text(json.dumps({
        "repo": str(repo.resolve()), "pid": 999_999_999,
    }))
    (active / actions.WORKTREE_MARKER).write_text(json.dumps({
        "repo": str(repo.resolve()), "pid": os.getpid(),
    }))
    (foreign / actions.WORKTREE_MARKER).write_text(json.dumps({
        "repo": str((tmp_path / "other").resolve()), "pid": 999_999_999,
    }))
    calls: list[tuple[str, ...]] = []

    async def fake_git(_repo, *args):
        calls.append(args)
        if args[:3] == ("worktree", "remove", "--force"):
            import shutil
            shutil.rmtree(args[3])
        return ""

    monkeypatch.setattr(actions, "_git", fake_git)
    assert await actions.prune_abandoned_worktrees(str(repo), temp_root=tmp_path) == 1
    assert not dead.exists()
    assert active.exists() and foreign.exists() and unrelated.exists()
    assert ("worktree", "prune") in calls


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


# --- executor: apply / cancel / revert + audit (worktree validation mocked) --- #

@pytest.fixture
def flake(tmp_path, monkeypatch):
    """A throwaway flake dir with a real home.nix, and worktree validation stubbed
    to succeed (the only genuinely external bit — needs git + nix)."""
    home = tmp_path / "modules" / "home-manager"
    home.mkdir(parents=True)
    (home / "default.nix").write_text(SAMPLE)

    async def fake_validate(flake_dir, hostname, edited):
        return True, "+    hello"

    monkeypatch.setattr(actions, "_validate_in_worktree", fake_validate)
    return tmp_path


def _home(flake_dir: Path) -> str:
    return (flake_dir / "modules" / "home-manager" / "default.nix").read_text()


async def _yes(_text): return True
async def _no(_text): return False
async def _noop(_text): return None
async def _switch_ok(): return "Done. new configuration."


async def test_action_applies_and_audits(flake):
    with capture_logs() as logs:
        out = await actions.run_app_action(
            Action("install_app", "hello"),
            flake_dir=str(flake), hostname="laptop",
            confirm=_yes, status=_noop, switch=_switch_ok,
        )
    assert "installed" in out
    assert "hello" in _home(flake)  # really written to the config
    audit = [e for e in logs if e.get("event") == "action"]
    assert audit and audit[0]["outcome"] == "installed"
    assert audit[0]["package"] == "hello"


async def test_action_cancel_audits_and_leaves_config(flake):
    with capture_logs() as logs:
        out = await actions.run_app_action(
            Action("install_app", "hello"),
            flake_dir=str(flake), hostname="laptop",
            confirm=_no, status=_noop, switch=_switch_ok,
        )
    assert "cancelled" in out.lower()
    assert "hello" not in _home(flake)  # nothing written
    assert any(e.get("event") == "action" and e["outcome"] == "cancelled" for e in logs)


async def test_action_revert_on_rebuild_failure(flake, monkeypatch):
    # Build-phase failure: the system generation never advanced, so we must NOT
    # roll the system back — just revert the config edit.
    monkeypatch.setattr(actions, "_system_generation", lambda: "system-1-link")

    async def _switch_fail():
        raise NixadminError("rebuild blew up")

    rolled = {"called": False}

    async def _rollback():
        rolled["called"] = True
        return "should not happen"

    with capture_logs() as logs:
        out = await actions.run_app_action(
            Action("install_app", "hello"),
            flake_dir=str(flake), hostname="laptop",
            confirm=_yes, status=_noop, switch=_switch_fail, rollback=_rollback,
        )
    assert "reverted" in out.lower()
    assert not rolled["called"]  # build failure → no system rollback
    assert "hello" not in _home(flake)  # config edit rolled back
    assert any(e.get("event") == "action" and e["outcome"] == "failed" for e in logs)


async def test_action_rolls_system_back_on_mid_activation_failure(flake, monkeypatch):
    # The profile advanced before the switch failed → a generation was activated,
    # so the system may be in a mixed state and must be rolled back.
    gens = iter(["system-1-link", "system-2-link"])
    monkeypatch.setattr(actions, "_system_generation", lambda: next(gens))

    async def _switch_fail():
        raise NixadminError("activation died")

    rolled = {"called": False}

    async def _rollback():
        rolled["called"] = True
        return "rolled back to generation 1"

    with capture_logs() as logs:
        out = await actions.run_app_action(
            Action("install_app", "hello"),
            flake_dir=str(flake), hostname="laptop",
            confirm=_yes, status=_noop, switch=_switch_fail, rollback=_rollback,
        )
    assert rolled["called"]  # system rolled back
    assert "rolled the system back" in out.lower()
    assert "hello" not in _home(flake)  # config edit also reverted
    assert any(e.get("event") == "action" and e["outcome"] == "failed_rolled_back"
               for e in logs)


async def test_action_reports_mixed_state_when_rollback_also_fails(flake, monkeypatch):
    gens = iter(["system-1-link", "system-2-link"])
    monkeypatch.setattr(actions, "_system_generation", lambda: next(gens))

    async def _switch_fail():
        raise NixadminError("activation died")

    async def _rollback_fail():
        raise NixadminError("rollback blew up too")

    with capture_logs() as logs:
        out = await actions.run_app_action(
            Action("install_app", "hello"),
            flake_dir=str(flake), hostname="laptop",
            confirm=_yes, status=_noop, switch=_switch_fail, rollback=_rollback_fail,
        )
    assert "mixed state" in out.lower()  # honest about the worst case
    assert any(e.get("event") == "action" and e["outcome"] == "rollback_failed"
               for e in logs)
