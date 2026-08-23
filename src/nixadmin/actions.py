"""Deterministic action tier — common writes without the frontier model.

The third routing tier (read · **known action** · open-ended change). A known
action like "install steam" needs only slot extraction (the local model / a regex)
plus a deterministic, daemon-driven edit — no agent required, so it works on a
machine with only a local model.

Safety model for the config edit (chosen in ADR follow-up):
1. validate the edit in an **isolated git worktree** via `nix eval` (proves the
   change evaluates and the package exists) — pure, unprivileged, never touches
   the live tree;
2. show the diff and get explicit confirmation;
3. only then apply the same edit to the real tree and `switch` via the root helper.

The daemon performs and reports the real result — the model never claims success.
Apps (install/remove) are implemented. Toggles (enable/disable a setting) are
recognised but deferred: safely editing nested Nix options needs per-option
templates, tracked separately.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from nixadmin.errors import NixadminError
from nixadmin.log import get_logger
from nixadmin.util import run as _run
from nixadmin.util import run_checked as _run_checked

log = get_logger(__name__)

HOME_FILE = "modules/home-manager/default.nix"
WORKTREE_PREFIX = "nixadmin-wt-"
WORKTREE_MARKER = ".nixadmin-owner.json"

# In-memory projection of the pinned nixpkgs attribute names. Built lazily from
# the local store (no shadow file, no nix-internal DB), held for the daemon's
# lifetime, and naturally refreshed when the daemon restarts on a rebuild.
_names_cache: dict[str, list[str]] = {}
_names_lock = asyncio.Lock()

# Nix expression: attribute names of the flake's pinned nixpkgs (names only —
# attrNames does not evaluate the packages, so this is cheap). Falls back to the
# NIX_PATH <nixpkgs> if the flake has no input named "nixpkgs".
_ATTRNAMES_EXPR = (
    'let f = builtins.getFlake "path:{flake}"; '
    'sys = builtins.currentSystem; '
    'np = (f.inputs.nixpkgs or null); '
    'p = if np != null then np.legacyPackages.${{sys}} else import <nixpkgs> {{}}; '
    "in builtins.concatStringsSep \"\\n\" (builtins.attrNames p)"
)

ConfirmFn = Callable[[str], Awaitable[bool]]
StatusFn = Callable[[str], Awaitable[None]]
SwitchFn = Callable[[], Awaitable[str]]
RollbackFn = Callable[[], Awaitable[str]]
# Given a phrase, return a real package name the user likely meant, or ''.
SuggestFn = Callable[[str], Awaitable[str]]


def _system_generation() -> str | None:
    """Target of the system profile symlink. It advances when a new generation is
    created — i.e. once a rebuild gets *past the build phase*. None if unreadable.
    Used to tell a build-phase failure (system untouched) from a mid-activation
    failure (system possibly in a mixed state, rollback warranted)."""
    try:
        return os.readlink("/nix/var/nix/profiles/system")
    except OSError:
        return None


async def load_package_names(flake_dir: str) -> list[str]:
    """Attribute names of the pinned nixpkgs, cached in memory for the daemon's
    lifetime. Empty list if evaluation fails (suggestions simply disabled)."""
    async with _names_lock:
        if flake_dir in _names_cache:
            return _names_cache[flake_dir]
        expr = _ATTRNAMES_EXPR.format(flake=flake_dir)
        rc, out = await _run("nix", "eval", "--impure", "--raw", "--expr", expr)
        names = out.split("\n") if rc == 0 and out else []
        if not names:
            log.warning("could not load nixpkgs names; suggestions disabled")
        _names_cache[flake_dir] = names
        return names


def fuzzy_candidates(query: str, names: list[str], *, n: int = 8) -> list[str]:
    """Nearest real package names by edit distance — candidates for the judge."""
    return difflib.get_close_matches(query.lower(), names, n=n, cutoff=0.5)

_INSTALL_RE = re.compile(r"\b(?:install|add)\b\s+(?:the\s+)?(.+)", re.IGNORECASE)
_REMOVE_RE = re.compile(r"\b(?:uninstall|remove|delete)\b\s+(?:the\s+)?(.+)", re.IGNORECASE)
_TOGGLE_RE = re.compile(r"\b(enable|disable|turn\s+(?:on|off))\b", re.IGNORECASE)


@dataclass(frozen=True)
class Action:
    kind: str  # "install_app" | "remove_app" | "toggle"
    target: str


def parse_action(text: str) -> Action | None:
    """Map an imperative query to a known action, or None if not recognised."""
    if m := _INSTALL_RE.search(text):
        return Action("install_app", _normalize(m.group(1)))
    if m := _REMOVE_RE.search(text):
        return Action("remove_app", _normalize(m.group(1)))
    if _TOGGLE_RE.search(text):
        return Action("toggle", text.strip())
    return None


def _normalize(phrase: str) -> str:
    """Clean an extracted target: lowercase, drop filler words. Keep multi-word
    phrases intact — the literal attempt handles single real attrs, and anything
    else flows to the candidates+judge step, which needs the full phrase."""
    s = phrase.strip().rstrip("?.!").lower()
    s = re.sub(r"\b(the|a|an|app|application|package|program|please|for me)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# Pure editor (unit-tested)
# --------------------------------------------------------------------------- #


def edit_packages(text: str, pkg: str, *, add: bool) -> str:
    """Add or remove a bare package name in the `home.packages = … [ … ];` list.

    Raises :class:`NixadminError` if the list can't be located, or (on remove) the
    package isn't present.
    """
    lines = text.splitlines(keepends=True)
    start = _find(lines, re.compile(r"home\.packages\s*=.*\["))
    if start is None:
        raise NixadminError("couldn't locate the home.packages list to edit")
    end = _find(lines, re.compile(r"^\s*\];"), start + 1)
    if end is None:
        raise NixadminError("couldn't find the end of the home.packages list")

    present = any(lines[i].strip() == pkg for i in range(start + 1, end))
    if add:
        if present:
            return text  # already installed — no-op
        newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
        entries = [line for line in lines[start + 1:end] if line.strip()]
        if entries:
            indent = entries[0][:len(entries[0]) - len(entries[0].lstrip())]
        else:
            closing_indent = lines[end][:len(lines[end]) - len(lines[end].lstrip())]
            indent = closing_indent + "  "
        lines.insert(end, f"{indent}{pkg}{newline}")
    else:
        if not present:
            raise NixadminError(f"{pkg} is not in the package list")
        lines = [
            ln for i, ln in enumerate(lines)
            if not (start < i < end and ln.strip() == pkg)
        ]
    return "".join(lines)


def _find(lines: list[str], pat: re.Pattern[str], start: int = 0) -> int | None:
    for i in range(start, len(lines)):
        if pat.search(lines[i]):
            return i
    return None


# --------------------------------------------------------------------------- #
# Executor (worktree-validated; integration-tested live)
# --------------------------------------------------------------------------- #


async def run_app_action(
    action: Action,
    *,
    flake_dir: str,
    hostname: str,
    confirm: ConfirmFn,
    status: StatusFn,
    switch: SwitchFn,
    rollback: RollbackFn | None = None,
    suggest: SuggestFn | None = None,
) -> str:
    """Install or remove an app. Returns a plain-language result for the user."""
    add = action.kind == "install_app"
    pkg = action.target
    if not pkg:
        return "I didn't catch which app you meant."

    src = Path(flake_dir) / HOME_FILE
    if not src.exists():
        return "I couldn't find the packages file to edit."

    original = src.read_text()
    try:
        edited = edit_packages(original, pkg, add=add)
    except NixadminError as e:
        return str(e)
    if edited == original:
        return f"{pkg} looks like it's already {'installed' if add else 'not installed'}."

    await status(f"checking that {pkg} {'installs' if add else 'removes'} cleanly…")
    ok, diff = await _validate_in_worktree(flake_dir, hostname, edited)

    # Recovery: real candidates from nixpkgs (difflib) + the model judges which one
    # (its strength). The candidate is a real attribute, then worktree-validated too.
    preamble = ""
    if not ok and add and suggest is not None:
        await status(f"'{pkg}' isn't a package — figuring out what you meant…")
        cand = await suggest(action.target)
        if cand and cand != pkg:
            cand_edited = edit_packages(original, cand, add=True)
            if cand_edited != original:
                ok, diff = await _validate_in_worktree(flake_dir, hostname, cand_edited)
                if ok:
                    preamble = f"I couldn't find '{pkg}'. Did you mean '{cand}'?\n\n"
                    pkg, edited = cand, cand_edited

    if not ok:
        verb = "install" if add else "remove"
        return f"I couldn't {verb} '{pkg}' — it didn't evaluate (is the name right?)."

    verb = "install" if add else "remove"
    if not await confirm(f"{preamble}This will {verb} {pkg}:\n\n{diff}\nApply and rebuild?"):
        # Audit: structured event → journald. query_id/session ride contextvars.
        log.info("action", kind=action.kind, requested=action.target, package=pkg,
                 outcome="cancelled")
        return "Cancelled — no changes made."

    src.write_text(edited)  # apply to the real tree
    await status("applying the change and rebuilding…")
    gen_before = _system_generation()
    try:
        result = await switch()
    except NixadminError as e:
        src.write_text(original)  # always revert the source edit
        # Did a new generation get activated before the failure? If the system
        # profile advanced, the switch failed *mid-activation* and the running
        # system may be in a mixed state — roll it back to the last good
        # generation. If the profile is unchanged the build failed first and the
        # system was never touched, so reverting the file is enough. (Never roll
        # back on a build failure — that would undo a healthy prior generation.)
        if rollback is not None and _system_generation() != gen_before:
            await status("the rebuild failed mid-activation — rolling the system "
                         "back to the last working generation…")
            try:
                await rollback()
            except NixadminError as re:
                log.error("action", kind=action.kind, requested=action.target,
                          package=pkg, outcome="rollback_failed",
                          error=f"{e}; rollback: {re}")
                return ("The rebuild failed partway through AND the automatic "
                        "rollback failed — the system may be in a mixed state. "
                        f"Please roll back manually. ({e}; rollback: {re})")
            log.warning("action", kind=action.kind, requested=action.target,
                        package=pkg, outcome="failed_rolled_back", error=str(e))
            return ("The rebuild failed partway through, so I rolled the system back "
                    "to the previous working generation and reverted the config "
                    f"edit. ({e})")
        log.warning("action", kind=action.kind, requested=action.target, package=pkg,
                    outcome="failed", error=str(e))
        return f"The rebuild failed before anything changed, so I reverted the config edit. ({e})"

    # Audit: the durable record of what nixadmin changed — query it from journald
    # with: journalctl --user -u nixadmin-daemon -o json | jq 'select(.event=="action")'
    log.info("action", kind=action.kind, requested=action.target, package=pkg,
             outcome="installed" if add else "removed", file=HOME_FILE)
    return (
        f"Done — {pkg} {'installed' if add else 'removed'}.\n{result}\n"
        "(The config edit is left uncommitted for you to review.)"
    )


async def _validate_in_worktree(flake_dir: str, hostname: str, edited: str) -> tuple[bool, str]:
    """Apply `edited` in a throwaway worktree and prove it evaluates. Returns
    (ok, diff). Never touches the live working tree."""
    wt = tempfile.mkdtemp(prefix=WORKTREE_PREFIX)
    try:
        await _git(flake_dir, "worktree", "add", "--detach", wt, "HEAD")
        repo_path = await asyncio.to_thread(os.path.realpath, flake_dir)
        (Path(wt) / WORKTREE_MARKER).write_text(json.dumps({
            "repo": repo_path, "pid": os.getpid(),
        }))
        (Path(wt) / HOME_FILE).write_text(edited)
        attr = f"path:{wt}#nixosConfigurations.{hostname}.config.system.build.toplevel.drvPath"
        rc, _out = await _run("nix", "eval", "--raw", attr)
        diff = ""
        if rc == 0:
            _rc, diff = await _run("git", "-C", wt, "diff", "--", HOME_FILE)
        return rc == 0, diff
    finally:
        await _git(flake_dir, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)


async def prune_abandoned_worktrees(
    flake_dir: str, *, temp_root: str | Path | None = None,
) -> int:
    """Remove dead nixadmin-owned validation worktrees for this exact flake."""
    root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    expected_repo = await asyncio.to_thread(os.path.realpath, flake_dir)
    removed = 0
    for candidate in root.glob(f"{WORKTREE_PREFIX}*"):
        marker = candidate / WORKTREE_MARKER
        try:
            owner = json.loads(marker.read_text())
            if owner.get("repo") != expected_repo or _pid_alive(int(owner["pid"])):
                continue
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        try:
            await _git(flake_dir, "worktree", "remove", "--force", str(candidate))
        except NixadminError as error:
            log.warning("could not prune abandoned worktree", path=str(candidate), error=str(error))
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed += 1
    if removed:
        await _git(flake_dir, "worktree", "prune")
        log.info("pruned abandoned validation worktrees", count=removed)
    return removed


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _git(repo: str, *args: str) -> str:
    return await _run_checked("git", "-C", repo, *args)
