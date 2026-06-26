# Build Progress — nixadmin v3

Living checklist for the greenfield build on `feat/v3-daemon`. Update as layers
land. Source of truth for *what* to build is [`nixadmin-v3-spec.md`](nixadmin-v3-spec.md);
the *why* (and what we're really building) is [`vision.md`](vision.md).

> **North star:** *a computer you can give to someone you love.* Judge features by
> "does this make the person think *less* in machine terms?" By that test the
> highest-leverage next work is **UX (GTK/voice — the terminal is the least human
> surface)** and **proactive nudges**, over more modules. Trust/safety *is* the
> human work.

## How to resume

```bash
cd ~/workspace/nix-nixadmin
git checkout feat/v3-daemon
git log --oneline -12          # see where we stopped
# run the smoke tests (full set needs httpx + litellm):
nix shell --impure --expr 'let p = import <nixpkgs> {}; in
  p.python313.withPackages (ps: [ ps.pytest ps.pytest-asyncio ps.structlog ps.httpx ps.litellm ps.dbus-fast ])' \
  --command python -m pytest -q
```

Build order is **bottom-up** (contracts → services → daemon → client → packaging).
Pick the first unchecked item.

## Status

### Foundation — DONE
- [x] `pyproject.toml` (src layout, hatchling, ruff+mypy, entry points)
- [x] `protocol.py` — wire contract, stdlib-only, encode/decode
- [x] `sdk.py` — module-author API + fail-fast validation
- [x] `errors.py` — NixadminError hierarchy
- [x] `log.py` — structlog convention (json/console, contextvars)
- [x] `py.typed`, `README.md`
- [x] smoke tests (protocol round-trips, sdk validation)

### Module system — DONE
- [x] `config.py` — Config dataclass + `from_env()` (env set by NixOS module)
- [x] `registry.py` — `load_modules()`: builtins + entry-point discovery, spec_version gating, name-collision guard
- [x] `builtins/` — apps, network, disk, services (each a `manifest`)

### Core services — DONE
- [x] `history.py` — HistoryBackend Protocol + NullHistory + factory
- [x] `session.py` — SessionState (scratch, incl. last_test_ok) + per-session lock (one in-flight)
- [x] `prefetch.py` — parallel fetcher exec (asyncio.to_thread), grounding guard
- [x] `routing.py` — deterministic mutation matcher + two-stage resolve (pure, DI'd)

### LLM backends — DONE
- [x] `llm/local.py` — Ollama httpx: is_ready, classify (timeout-guarded) + summarize stream; pure prompt helpers
- [x] `llm/remote.py` — LiteLLM agent loop, build_tools (fetcher + rebuild), DI'd run_tool callback, history

### Reactive + safety — DONE
- [x] `safety.py` — gate (confirm, test-before-switch via SessionState) + helper-socket client
- [x] `context.py` — ContextProvider cache + TTL, system-prompt assembly (remote only)
- [x] `monitors.py` — poll loop + dbus-fast, interval floor, concurrency cap, event callback

### Daemon + client — DONE
- [x] `server.py` — unix socket server, hello, query dispatch, confirm/input, cancel, broadcast
- [x] `cli.py` — terminal client (spinner, streaming, confirm prompts)

### Packaging — DONE
- [x] `flake.nix` — buildPythonApplication (httpx/litellm/dbus-fast/structlog), devShell, nixosModules.default
- [x] `nix/nixadmin-helper.py` — privileged rebuild helper (recovered from v2, protocol matches safety.py)
- [x] `nix/module.nix` — NixOS module: nixadmin group, root helper service, user daemon service, linger
- [x] `nix build .#nixadmin` succeeds; built client runs; `nix flake show` clean

## DEPLOYED & LIVE (2026-06-13) ✅

v3 is built, integrated into nixlap, deployed, and **running in production** as a
systemd user service. Verified end-to-end against the deployed daemon: "how much
disk space do I have left?" → "You have approximately 733G available out of 951G
on the main filesystem /." (chain=local, qwen2.5:3b, grounded in real df output).

Active services: `nixadmin-helper` (root), `nixadmin-daemon` (user),
`nixadmin-ollama` + preload (user). Socket: `/run/user/1001/nixadmin.sock`.
The one-time v2→v3 helper bootstrap was completed via a direct `sudo nixos-rebuild
switch`; future `nixadmin-rebuild switch` works normally now.

### Writes — deterministic action tier (LIVE)
- Third routing tier: read · **known action** · open-ended change.
- `install <app>` / `remove <app>` work on the **local chain alone** (no frontier):
  parse → slot-extract → edit `home.packages` in an isolated **git worktree** →
  validate with `nix eval` → show diff + confirm → apply to real tree → `switch`
  via root helper → report the real result. Edit left uncommitted for review.
- **Verified live end-to-end:** `install hello` → "Hello, world!" runnable; then
  `remove hello` → config back to clean.
- Mutation routing fixed: interrogative phrasing ("can you install…?") is a
  question, not a write; remote is only "ready" with real credentials (no more
  auth-error leaks); writes with no usable remote get a plain limitation.
- **Deferred:** toggles (enable/disable a setting) — recognised but answered
  "not yet"; safe nested-Nix-option editing needs per-option templates.
- Needs `git` + `nix` on the daemon PATH (set in module.nix).

### Modules (10 live)
- **Built-in (core):** apps, network, disk, services.
- **External package `contrib/nixadmin-extras`** (via `nixadmin.modules` entry points,
  deployed through `services.nixadmin.extraModules`): system, power (low-battery
  monitor), performance, bluetooth, updates, security (`routing="local"`).
- Discovery proven end-to-end: deployed daemon loads all 10; live-verified answers
  for system / power / security (security stayed `[local]` per its privacy routing).
- This validated the plugin pathway — the architectural centerpiece — for real.

### Testing (enforced)
- `nix flake check` runs all gates in the sandbox: **pytest (53)**, **mypy --strict
  (clean)**, **ruff**, plus the NixOS module eval. One reproducible command.
- Dev loop: `nix develop` then `pytest -q` / `ruff check .` / `mypy src/nixadmin`.
- Coverage is **smoke-level + 1 daemon integration test**. Still thin on:
  llm network paths, the remote agent tool-loop, monitors, cli, safety socket.
  The "real harness" (fakes for Ollama/LiteLLM/D-Bus, dispatch-branch coverage)
  is the next testing investment.

### Observability / proactive (next real work)
- **Proactive detector (#2)** — TODO, **planned in detail**:
  [`proactive-detectors-plan.md`](proactive-detectors-plan.md). Cold-resumable.
  2a = two stateful core detectors in a new `detectors.py` (process-vanished via
  dynamic baseline → catches the silent panel death; new-coredump) wired into
  MonitorRunner, emitting `Event`s. No buffer/store — journald is the ring.
  2b (deferred) = desktop notifications + model-phrased diagnosis + offered fix.
  2c (deferred) = error-rate spike, OOM, baseline persistence.
- On-demand diagnosis (#1) — DONE via the `health` module (live journald queries).
- Audit trail — DONE: write-actions emit structured journald events
  (`journalctl --user -u nixadmin-daemon -o json | jq 'select(.event=="action")'`).

### Quality & robustness backlog (2026-06-17 review)

Grounded review of the privilege path. Tiered by stakes — for a tool that edits
config and runs `nixos-rebuild` as root, robustness = *the privilege path can't
lie or race*. File:line refs so this is cold-resumable.

**Tooling / process:**
- [ ] **Adopt [beads](https://github.com/steveyegge/beads) (`bd`) for task tracking.**
  This project is outgrowing a hand-maintained markdown checklist. Beads is a
  git-backed issue tracker (issues as JSON/JSONL in-repo, dependency graph, CLI +
  agent-friendly) — keeps tasks versioned alongside the code and survives across
  sessions without a context dump. Migrate this backlog + the proactive-detectors
  plan into `bd` issues with deps; keep PROGRESS.md as the narrative/north-star,
  let `bd` own the granular task state.

**Tier 1 — safety invariants (correctness bugs, do first):**
- [x] **Gate on the real exit code, not a string match.** *(done 2026-06-17)*
  `_run_helper` now returns `(output, exit_code)`; `rebuild` does
  `state.record_test(code == 0)`; `_looks_successful` removed. Also fixed a latent
  bug: `apply_switch` returned a string on failure, so the action tier's
  revert-on-rebuild-failure path never fired — it now raises `SafetyError` on a
  nonzero exit (matches the existing `test_action_revert_on_rebuild_failure`
  contract). Covered by 5 new gate tests in `test_safety_context_smoke.py`,
  including the two regressions (fail without the word "failed"; pass with "0
  failed" in output). `nix flake check` green.
- [x] **Serialize rebuilds in the helper + deadlock safeguard.** *(done 2026-06-17)*
  Module-level **in-memory** `_rebuild_lock` in `nixadmin-helper.py` held around
  `Popen`/`wait`, so only one rebuild runs at a time even if a second client
  connects directly (bypassing the daemon's per-session lock). A waiting client
  gets a "another rebuild is in progress; waiting…" stream line.
  - **Deadlock safety:** in-memory (not a lockfile) on purpose — a helper crash
    drops the lock and systemd restarts unlocked; a pidfile would leave a stale
    lock. The only uncovered case (a rebuild that *hangs* forever) is bounded by a
    `threading.Timer` watchdog that `proc.kill()`s any rebuild exceeding
    `NIXADMIN_REBUILD_TIMEOUT` (default 3600s), which releases the lock via
    `finally`. We never time-release or steal the lock mid-rebuild (that would
    re-introduce concurrent activation).
  - A dead client pipe stops writes (`_send` returns False) without orphaning the
    rebuild — output keeps draining so the child never blocks on a full pipe.
  - Validated live at next `nixos-rebuild` (the helper's flake8 lint runs at module
    build, not in `nix flake check`).
- [x] **Partial-switch honesty → automatic system rollback.** *(done 2026-06-17)*
  On a switch failure the action tier now decides by the **system profile symlink**
  (`_system_generation()`): if it *advanced*, the switch failed mid-activation, so
  we automatically `apply_revert()` (`switch --rollback`) to the last good
  generation AND revert the config edit (`outcome=failed_rolled_back`). If the
  profile is *unchanged*, it was a build-phase failure (system untouched) — revert
  the file only, **never** roll back (that would undo a healthy prior generation).
  If the rollback itself fails we say so plainly ("the system may be in a mixed
  state", `outcome=rollback_failed`). `apply_revert` added to `SafetyGate`; wired
  in `server._run_action`. 3 new tests cover all three branches. `nix flake check`
  green.

**Tier 2 — test the dangerous code (coverage is thin exactly where it matters):**
- [x] **Safety-gate protocol test** against a fake helper socket *(done 2026-06-17,
  with Tier 1 #1)*: `FakeHelper` speaks the newline-JSON helper protocol; tests
  cover test→switch enablement, failed-test blocks switch, and `apply_switch`
  raise/return on the real `_run_helper` socket path.
- [ ] **Property tests for `edit_packages`** (hypothesis): `add` idempotent;
  `add`+`remove` round-trips to original; `remove` of absent always raises;
  list delimiters survive. It's the one fn that rewrites the user's config.
- [ ] **Helper `revert → switch --rollback` mapping** (`nixadmin-helper.py:52-53`)
  untested + lives outside the package. Add a unit even as a script.

**Tier 3 — operational robustness:**
- [ ] **Self-healing worktrees.** SIGKILL mid-`_validate_in_worktree` leaks a
  worktree + tmpdir and trips the next `worktree add`. Run `git worktree prune`
  at daemon startup.
- [ ] **Daemon supervision.** systemd `Restart=on-failure` + backoff, and
  `WatchdogSec`/`sd_notify` so a *hung* daemon (wedged on a helper read) restarts.
- [ ] **Version the client↔daemon wire** in the hello handshake (module ABI has
  `spec_version`; the wire protocol does not) — stale client should fail loud.

**Tier 4 — when there's slack:**
- [ ] Helper read timeout in `_run_helper` (`safety.py:80`) so a stuck helper
  doesn't hang the daemon forever.
- [ ] Keep `litellm` pinned + lazy-imported so the local-only path never loads the
  heavy/remote-surface dep.

### Follow-ups (optional, not blocking — v3 fully works on the local chain)
- Remote chain needs a Hermes proxy / API base (Claude subscription) before use.
  `defaultChain="local"` so the system works without it today.
- machine-profile ContextProvider (interface ready, none registered).
- desktop-notification path when no client connected (events only broadcast now).
- the real test harness described above.
- CI: GitHub Actions running `nix flake check`.

## v1 build COMPLETE — earlier integration notes (now done)
1. **Wire into nixlap** — add this flake as an input in `~/workspace/nixlap/flake.nix`,
   import `nixadmin.nixosModules.default`, set `services.nixadmin` options (user=steve,
   flakeDir, hostname=laptop, local.model="qwen2.5:3b", defaultChain). Ollama container
   (from v2 `git show main:modules/nixos/nixadmin.nix`) is NOT in v3 yet — the daemon
   expects Ollama at localhost:11434. Either keep the v2 container module or add one.
2. **`nixadmin-apps` command** — the apps module fetcher calls it; it was defined in the
   v2 module. Re-provide it (script listing nix + flatpak packages).
3. **Live smoke** — `nixadmin-daemon` by hand, then `nixadmin`, ask "is my wifi working?".
4. **Real harness** (deferred per user): fakes for Ollama/LiteLLM/D-Bus, golden transcripts.

### Known gaps / TODO-later (not blocking)
- Remote readiness is optimistic (assumed ready if model configured); failures surface per-call.
- `Ready` message wired but `hello.ready` only reflects local; remote always advertised ready.
- Monitors' desktop-notification-when-no-clients path is not implemented (events only broadcast).
- Context providers: no built-in machine-profile provider yet (interface ready, none registered).
- History is NullHistory only; SessionState scratch works (test-before-switch enforced).

## Key decisions already locked (don't re-litigate)
- Two independent chains: local (classify→prefetch→summarize, no tools) / remote (tools).
- Routing never silent: any remote fallback is a `confirm`; privacy pinned-local needs explicit consent.
- Tools: no model-supplied shell strings. Fetcher-derived (zero-arg) or schema-validated enum.
- classify = local model, runs only when local present, 2s cold-start timeout.
- Mutation intent on local chain = deterministic matcher (not LLM); bypasses model so it can't fake "Done!".
- Modules = trusted code (entry points), lowercase `manifest` export, spec_version ABI gate.
- Daemon = systemd **user** service; privileged work via separate root helper socket.
- History keyed by `session`; v1 NullHistory. SessionState is separate always-present scratch.
