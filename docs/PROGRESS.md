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

### Daemon + client — TODO
- [ ] `server.py` — unix socket server, hello, query dispatch, confirm/input, cancel, broadcast
- [ ] `cli.py` — terminal client (spinner, streaming, confirm prompts)

### Packaging — TODO
- [ ] `flake.nix` — package the app, devShell, NixOS module (user service + root helper)
- [ ] root `nixadmin-helper` (privileged rebuild) — re-derive from v2 history (`git show main:modules/nixos/helper/nixadmin-helper.py`)

## Key decisions already locked (don't re-litigate)
- Two independent chains: local (classify→prefetch→summarize, no tools) / remote (tools).
- Routing never silent: any remote fallback is a `confirm`; privacy pinned-local needs explicit consent.
- Tools: no model-supplied shell strings. Fetcher-derived (zero-arg) or schema-validated enum.
- classify = local model, runs only when local present, 2s cold-start timeout.
- Mutation intent on local chain = deterministic matcher (not LLM); bypasses model so it can't fake "Done!".
- Modules = trusted code (entry points), lowercase `manifest` export, spec_version ABI gate.
- Daemon = systemd **user** service; privileged work via separate root helper socket.
- History keyed by `session`; v1 NullHistory. SessionState is separate always-present scratch.
