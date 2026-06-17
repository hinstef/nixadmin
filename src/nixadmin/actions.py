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
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from nixadmin.errors import NixadminError
from nixadmin.log import get_logger

log = get_logger(__name__)

HOME_FILE = "modules/home-manager/default.nix"

# Curated common nixpkgs apps, used for deterministic typo correction (a 3B local
# model is unreliable at fuzzy name matching; difflib against this list is not).
# Extend freely — a wrong guess is still gated by "did you mean?" + worktree eval.
COMMON_APPS = [
    "firefox", "chromium", "google-chrome", "brave", "tor-browser",
    "vlc", "mpv", "obs-studio", "audacity", "handbrake", "kdenlive", "shotcut",
    "spotify", "discord", "slack", "telegram-desktop", "signal-desktop",
    "element-desktop", "zoom-us", "teams-for-linux", "thunderbird",
    "gimp", "inkscape", "krita", "blender", "darktable", "freecad", "openscad",
    "libreoffice", "onlyoffice-bin", "obsidian", "logseq", "zotero", "calibre",
    "vscode", "vscodium", "sublime4", "neovim", "vim", "emacs", "zed-editor",
    "git", "gh", "lazygit", "docker", "podman", "podman-compose",
    "nodejs", "python3", "go", "rustc", "cargo", "uv", "ruff",
    "kubectl", "k9s", "kubernetes-helm", "terraform", "ansible",
    "keepassxc", "bitwarden", "nextcloud-client", "syncthing", "rclone",
    "transmission", "qbittorrent", "deluge",
    "htop", "btop", "fastfetch", "neofetch", "tree", "ripgrep", "fd", "bat",
    "eza", "fzf", "jq", "yq", "curl", "wget", "tmux", "zellij",
    "steam", "lutris", "heroic", "prismlauncher", "mangohud",
    "mission-center", "gnome-disk-utility", "flatseal", "wireshark", "vlc",
]

# Natural phrases → nixpkgs attribute names. Extend as needed; unknown names are
# tried verbatim and validated by the worktree build.
ALIASES = {
    "chrome": "google-chrome",
    "google chrome": "google-chrome",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "the gimp": "gimp",
    "signal": "signal-desktop",
}

ConfirmFn = Callable[[str], Awaitable[bool]]
StatusFn = Callable[[str], Awaitable[None]]
SwitchFn = Callable[[], Awaitable[str]]


def closest_app(name: str) -> str:
    """Deterministic typo correction: nearest common app by edit distance, or ''."""
    matches = difflib.get_close_matches(name.lower(), COMMON_APPS, n=1, cutoff=0.6)
    return matches[0] if matches else ""

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
    s = phrase.strip().rstrip("?.!").lower()
    s = re.sub(r"\b(app|application|package|program|please|for me)\b", "", s).strip()
    if s in ALIASES:
        return ALIASES[s]
    # Multi-word leftovers we don't recognise: take the first token (best effort;
    # the worktree build is the real validator).
    return s.split()[0] if s else s


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
        lines.insert(end, f"    {pkg}\n")
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

    # Typo recovery: deterministic nearest common app, validated in the worktree too.
    preamble = ""
    if not ok and add:
        cand = closest_app(pkg)
        if cand and cand != pkg:
            await status(f"'{pkg}' isn't a package — did you mean '{cand}'? checking…")
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
        return "Cancelled — no changes made."

    src.write_text(edited)  # apply to the real tree
    await status("applying the change and rebuilding…")
    try:
        result = await switch()
    except NixadminError as e:
        src.write_text(original)  # roll back the edit if the rebuild can't run
        return f"The rebuild failed, so I reverted the change. ({e})"

    return (
        f"Done — {pkg} {'installed' if add else 'removed'}.\n{result}\n"
        "(The config edit is left uncommitted for you to review.)"
    )


async def _validate_in_worktree(flake_dir: str, hostname: str, edited: str) -> tuple[bool, str]:
    """Apply `edited` in a throwaway worktree and prove it evaluates. Returns
    (ok, diff). Never touches the live working tree."""
    wt = tempfile.mkdtemp(prefix="nixadmin-wt-")
    try:
        await _git(flake_dir, "worktree", "add", "--detach", wt, "HEAD")
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


async def _git(repo: str, *args: str) -> str:
    rc, out = await _run("git", "-C", repo, *args)
    if rc != 0:
        raise NixadminError(f"git {' '.join(args)} failed: {out.strip()}")
    return out


async def _run(*cmd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")
