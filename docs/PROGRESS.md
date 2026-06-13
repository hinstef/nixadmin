# Build Progress — nixadmin v3

Living checklist for the greenfield build on `feat/v3-daemon`. Update as layers
land. Source of truth for *what* to build is [`nixadmin-v3-spec.md`](nixadmin-v3-spec.md).

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

## DEPLOY STATUS (2026-06-13)

v3 is fully built, integrated into nixlap, and **proven working live** (daemon
answered "is my wifi working?" against real qwen2.5:3b). The laptop config builds.
nixlap changes made: flake input → `github:hinstef/nixadmin/feat/v3-daemon`,
`services.nixadmin` updated to v3 schema (`defaultChain="local"`).

**BLOCKED on one privileged step.** Switching from v2→v3 changes the
`nixadmin-helper` derivation, so `switch-to-configuration` restarts the helper
mid-switch — severing the connection running the switch (the helper *is* the
privilege path). The switch was interrupted; system is cleanly still on the v2
generation, helper now inactive. This is a one-time bootstrap problem.

**To finish (user runs once, authenticates with fingerprint):**
```bash
cd ~/workspace/nixlap
! sudo nixos-rebuild switch --flake .#laptop
```
Running it directly via sudo (not the helper) avoids the self-restart problem.
After this first switch lands v3's helper, future `nixadmin-rebuild switch` works
again. Then verify: `systemctl --user status nixadmin-daemon` and run `nixadmin`.

Rollback if needed: `! sudo nixos-rebuild switch --rollback`.

## v1 build COMPLETE — what's left before it runs on the laptop
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
