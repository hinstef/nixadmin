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

### Observability / proactive

> **Open tasks live in beads** (`bd ready` / `bd list`). Below is the built +
> validated record and the locked decisions; design detail is in the linked docs.

- **Proactive detectors (#2)** — designed, not built:
  [`proactive-detectors-plan.md`](proactive-detectors-plan.md). Tracked in beads
  (labels `proactive`/`autofix`).
- On-demand diagnosis (#1) — **DONE + validated live (2026-06-26).** "Are there any
  errors?" now names the unit, quotes the real cause, and suggests a fix. Three
  fixes earned from live testing: adaptive answer prompt (status=1 sentence,
  problem=what/why/fix); failed-units-with-reason moved to builtin `services`
  (where errors classify) as shared `FAILED_UNITS_CMD`; removed a hardcoded
  keyword grep that was deleting the real error line. Two-channel output: full
  grounding + model answer logged to journald (`event=grounding`/`local answer`,
  query_id-correlated); user sees only the concise answer.
- **Remediation / "offer & act" (#3) — slice 1 DONE + validated live (2026-06-27).**
  New `remediation.py` tier (runtime fixes, distinct from `actions.py` config
  writes). Restart a failed *user* unit end-to-end: parse "restart/relaunch/reload
  X" (skips how-to questions) → resolve to a real unit by name/description match
  against live state (prefers failed) → confirm → `systemctl --user restart` →
  **verify** with `is-failed` → report the real result. Verified both branches:
  "healthy again" on success, and honestly "still failing — needs a real fix"
  with the journal tail when restart doesn't help (the disk-quota case). Handled
  before classify so it pays no model-warm cost. Audited to journald.
  - Next slices tracked in beads (label `remediation`).
- Audit trail — DONE: write-actions emit structured journald events
  (`journalctl --user -u nixadmin-daemon -o json | jq 'select(.event=="action")'`).
- **Persistent event store + web hub — DONE + validated live (2026-07-26).** New
  daemon-owned `store.py` (stdlib SQLite, `<stateDir>/events.db`, on by default)
  is the observability substrate: an append-only timeline of `failure_observed`/
  `failure_cleared` (on transition), `explanation`, `restart`, `journal_snapshot`,
  `monitor_event`. Daemon is the single writer; clients read it over a new wire
  message (`get_timeline`→`timeline`, protocol **v2**, additive). The web view is
  now a **hub**: a live "Now" section (whose refresh no longer wipes detail — the
  old "explanation/journal disappears after a few seconds" bug) plus a persistent
  "Timeline" that survives refreshes *and* daemon restarts. The **tray "Explain"**
  now deep-links into the hub (`?explain=<unit>`) and the daemon persists the
  answer, replacing the transient desktop notification (resolves `observations.md`).
  Distinct from conversation `history.py` (still NullHistory) — see
  [`adr/0003-event-store.md`](adr/0003-event-store.md). **Verified live:** failing
  transient unit → failure observed, journal, and a real qwen2.5:3b explanation all
  persisted; all 4 events survived a daemon kill+restart and rendered via
  `/api/timeline`. This is the substrate the **autofix** engine (`e7q`) reads/writes.

### Autofix engine — act on unit-failure events (2026-07-26)

- **The P1, built.** Closes the loop `docs/ux.md` is built around: a failed systemd
  unit is *already* seen (the `services` D-Bus `JobRemoved` monitor) — now the
  daemon acts. On a failure event, `_run_autofix` handles each newly-failed unit
  once per episode; a pure policy (`autofix.py::decide`) chooses **restart** (via
  the existing `remediation.restart_resolved` — user directly, system via the root
  helper), **inform**, or **skip**. It restarts, **verifies** (honest — a restart
  that doesn't stick says so), and records an `autofix` event to the timeline.
- **Restart-loop guard from the event store:** prior `autofix` restart events for
  the unit within the hour are counted; past `maxAttempts` (default 1) it stops
  restarting and informs ("keeps failing — needs a real fix") instead of looping.
- **Both scopes auto-heal by default** (`services.nixadmin.autofix.{enable,system,
  maxAttempts}`); system autonomy can be turned off. Pre-existing failures are
  seeded at startup so boot isn't a bulk-restart — we act on failures that *happen*.
- Deterministic (no LLM); reuses the remediation tier + safety gate (no new
  privilege path). Delivery is via `Event` + the persisted Timeline (🤖); desktop
  notifications + "want me to fix it?" offers stay 2b (`b43`).

### Invoke bar — talk to the agent from the web hub (2026-07-26)

- **Web invoke bar + streaming transport — built.** The hub grew a single
  summonable input ("What would you like?", `/` focuses it) — an ephemeral
  **task card**, not a resident chat panel (settles part of `nix-nixadmin-edx`
  toward the invoke surface; keeps `ux.md`'s design-for-silence). The web server
  gained a streaming transport it lacked: `web/session.py` holds a live daemon
  socket across a mid-query confirm, exposed as **SSE** (`GET /api/stream`) + a
  `POST /api/respond`/`/api/cancel` (CSP `connect-src 'self'` allows SSE; no
  WebSocket). This is what lets a person **install apps from the web** — the
  existing local action tier's diff-confirm now works in the browser. Invoke-bar
  activity persists to the Timeline (`ask`/`action` events).
- **Confirmed, redacted escalation — built.** The local model self-judges its
  competence (`local.assess_escalation`, biased to stay local); when it isn't
  confident it **offers** the frontier (never silent). Before anything leaves, a
  two-pass **redaction** (`redact.py`: deterministic scrub + local-model rewrite)
  runs and the redacted payload is shown **verbatim in the confirm**. Accepting
  with no remote configured gives an honest "not set up yet" (`b4h` flips it live,
  no UI change). See [`adr/0004-escalation-and-redaction.md`](adr/0004-escalation-and-redaction.md).
  *v1 redacts the query text; grounding/tool-output/history redaction is a tracked
  follow-up.*

### Diagnosis findings to fix (from live testing 2026-06-26)
- [x] **Cold-start false all-clear (SAFETY).** *(done 2026-06-27)* The model's
  cold load is ~6s but classify's timeout was 2s → on a cold model classify timed
  out → `[]` → false "everything is fine" from zero grounding. Fix: classify takes
  `timeout_s`; daemon passes `COLD_CLASSIFY_TIMEOUT` (60s) when the model isn't
  loaded so the classify request itself drives the on-demand load, and emits
  `Status("Warming up…")` so any frontend shows a loading state. Paired with lazy
  model loading: `services.nixadmin.local.keepAlive` (default 10m) + boot preload
  removed, so the model unloads when idle (reclaims ~2GB) and the cold first query
  is slow-but-correct (~9.5s, with the warming note). Verified live.
  *(Remaining open finding — prefetch command-echo noise — tracked in beads.)*

### Quality & robustness backlog (2026-06-17 review)

Grounded review of the privilege path. Tiered by stakes — for a tool that edits
config and runs `nixos-rebuild` as root, robustness = *the privilege path can't
lie or race*. File:line refs so this is cold-resumable.

**Tooling / process:**
- [x] **Adopt [beads](https://github.com/steveyegge/beads) (`bd`) for task
  tracking.** *(done 2026-06-30)* Initialized in this repo (`.beads/`, tracked via
  `.beads/issues.jsonl`); `bd` installed via nixlap home packages. The open backlog
  (autofix, remediation next-slices, proactive detectors, quality/robustness items,
  UX/UI, the exit-None bug) migrated into 22 issues, grouped by label. **`bd` now
  owns granular task state; this file stays the narrative/north-star.** `bd ready`
  / `bd list`. bd's default CLAUDE.md/AGENTS.md were trimmed — its mandatory-push
  and no-MEMORY.md directives were removed (pushes stay user-controlled; the
  harness memory system stays in use).

**Tier 1 — safety invariants (correctness bugs, do first):**
- [x] **Run `nixos-rebuild` in a detached cgroup, not the helper's.** *(done +
  validated live 2026-06-29)* The helper now launches the rebuild via `systemd-run`
  as a detached transient unit (`nixadmin-rebuild-<ts>-<pid>.service`,
  service-type=oneshot, RemainAfterExit) owned by PID 1 — not in
  `nixadmin-helper.service`'s cgroup. So when a helper-changing switch restarts the
  helper mid-activation, the rebuild runs to completion independently; only the
  live stream is lost. Streams via `journalctl --unit=… -f`, reads the real exit
  via `systemctl show -p ExecMainStatus`, cleans the unit up after; `_cleanup_stale`
  reaps finished leftovers at startup (running ones untouched). **Proof:** two
  helper-changing switches driven entirely through the socket both completed
  (`/run/current-system` advanced, collateral units healthy) where the old design
  died half-done; the first run's leftover unit was reaped by the second.
  Bootstrap note: installing the fix itself was a helper-changing switch, so it was
  applied once via direct `sudo nixos-rebuild`; everything after goes via the
  socket. Known wart: exactly one inert `active exited` rebuild unit lingers after
  each helper-changing socket deploy (reaped next deploy; gone on reboot).
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
- [x] **Helper pure logic now unit-tested.** *(done 2026-06-29)* Extracted the
  helper's decision logic into pure functions (`build_cmd` incl. revert→switch
  --rollback, `unit_is_finished`, `exit_code_from`, `is_reapable`, `_send`) and
  made the module import-clean (no env read at load). `tests/test_helper_smoke.py`
  loads it by path and covers them (22 cases) — notably `is_reapable` never
  reaping a *running* rebuild, and `exit_code_from` never reporting a failed unit
  as success. Detached systemd-run / restart-survival stay integration-validated
  live. Behavior-preserving refactor; deploy whenever (tests pass without it).

_Tier 2–4 open items (property tests, worktree prune, daemon supervision, wire
versioning, helper read timeout, lazy litellm) and the earlier follow-ups (real
test harness, machine-profile provider, CI, remote-chain enablement) are all
tracked in beads — `bd list -l quality`._

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

_Earlier "known gaps" — remote readiness / `hello.ready` accuracy, the
desktop-notification-when-no-client fallback, machine-profile provider, History
backend — are tracked in beads._

## Key decisions already locked (don't re-litigate)
- Two independent chains: local (classify→prefetch→summarize, no tools) / remote (tools).
- Routing never silent: any remote fallback is a `confirm`; privacy pinned-local needs explicit consent.
- Tools: no model-supplied shell strings. Fetcher-derived (zero-arg) or schema-validated enum.
- classify = local model, runs only when local present, 2s cold-start timeout.
- Mutation intent on local chain = deterministic matcher (not LLM); bypasses model so it can't fake "Done!".
- Modules = trusted code (entry points), lowercase `manifest` export, spec_version ABI gate.
- Daemon = systemd **user** service; privileged work via separate root helper socket.
- History keyed by `session`; v1 NullHistory. SessionState is separate always-present scratch.
