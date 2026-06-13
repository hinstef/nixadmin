# nixadmin v3 — Architecture Spec

## Vision

Ambient system intelligence layer for NixOS. The machine knows its own state,
understands it in context, and can explain or fix it in plain language. Not a
chatbot — a personal observability layer with a conversational interface on top.

UX layers (terminal, GTK app, web) are thin clients. All intelligence lives in
the daemon.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   nixadmin daemon                    │
│                                                      │
│  module registry   (entry_points: nixadmin.modules) │
│  context assembly  (lazy, cached, refreshed on TTL) │
│  interceptor       (classify → prefetch → augment)  │
│  router            (local | remote, 3-level)        │
│  safety gate       (baked in, not bypassable)       │
│  monitor loop      (poll + D-Bus reactive events)   │
│  history           (NullHistory v1; sqlite later)   │
└────────────────┬────────────────────────────────────┘
                 │  Unix socket  (newline-delimited JSON)
     ┌───────────┼───────────┐
     ▼           ▼           ▼
 [terminal]  [GTK app]   [web UI]
```

**LLM backends:**

```
Local    Ollama direct HTTP    (classify → prefetch → summarize, no tools)
Remote   LiteLLM               (full agent, tool calling, safety-gated)
             ├── Hermes proxy  (Claude Pro / ChatGPT Pro subscription)
             ├── API keys      (Anthropic, OpenAI, OpenRouter, Gemini…)
             └── self-hosted   (remote Ollama, vLLM, etc.)
```

The local model is used **only** by the local chain (for both classify and
summarize). The remote chain never invokes it. The two chains share no runtime
dependency — either can be down without affecting the other.

---

## Socket Protocol

Newline-delimited JSON over a Unix socket at `$XDG_RUNTIME_DIR/nixadmin.sock`.

### Handshake

On connect the daemon immediately sends a single `hello`:

```json
{"type": "hello", "version": 1,
 "chains": ["local", "remote"],
 "ready": {"local": false, "remote": true},
 "default_chain": "remote",
 "modules": ["apps", "network", "disk", "services"]}
```

- `version` — protocol version. Client must check it; disconnect if unsupported.
- `chains` — which chains this daemon is configured for.
- `ready` — per-chain readiness at connect time. A chain may still be warming up
  (Ollama loading, remote endpoint unreachable). The client receives a `ready`
  push when a not-yet-ready chain comes up (see *Daemon Startup & Chain Readiness*).
- `default_chain` — used when a query omits `chain`.
- `modules` — loaded module names, for display/debugging.

### Client → Daemon

```json
{"type": "query",   "id": "abc", "session": "s1", "text": "is my wifi working?"}
{"type": "query",   "id": "abc", "session": "s1", "text": "...", "chain": "local"}
{"type": "cancel",  "id": "abc"}
{"type": "respond", "id": "abc", "confirmed": true}
{"type": "respond", "id": "abc", "value": "firefox"}
```

- `id` scopes a single request/response exchange (query → deltas → done).
- `session` scopes a *conversation* — history is keyed by it. A client picks a
  stable session id (terminal uses one per launch; GTK could keep one per window).
  **v1 ignores `session` entirely** (NullHistory), but it is part of the protocol
  now so that adding history later is not a breaking change. Omitting it is
  allowed; the daemon treats a missing `session` as `"default"`.
- `chain` is optional. Omit to use daemon default or module hint.
- `respond` answers a pending `confirm` or `input` request by `id`.
  - For `confirm`: use `"confirmed": bool`
  - For `input`: use `"value": string`
- `cancel` aborts an in-flight query by `id`:
  - closes the LLM stream, stops the agent loop, emits a final `done`.
  - if a `confirm`/`input` is pending for that `id`, its future resolves as
    cancelled (treated as `confirmed=false`) so nothing leaks.
  - **privileged actions are non-cancellable once started** — a
    `nixadmin_rebuild switch` already executing runs to completion; cancel
    applies only before the action begins.

### Daemon → Client

```json
{"type": "delta",   "id": "abc", "text": "Yes, your WiFi is connected."}
{"type": "status",  "id": "abc", "text": "local model warming up…"}
{"type": "done",    "id": "abc"}
{"type": "error",   "id": "abc", "text": "backend unavailable"}
{"type": "confirm", "id": "abc", "text": "Local AI is starting. Use remote instead?"}
{"type": "input",   "id": "abc", "prompt": "Package name:"}
{"type": "event",   "source": "monitor.service-failed",
                    "severity": "error", "text": "nginx stopped — port 80 in use"}
```

- `delta` / `status` / `done` / `error` / `confirm` / `input` are scoped to a query `id`.
- `status` is a **non-blocking** informational push (no response expected) — the UI
  shows it while waiting (warming up, escalating, etc.). `confirm` is blocking.
- `event` is unsolicited, broadcast to all connected clients.
- Severity: `"info"` | `"warning"` | `"error"`
- **Ordering:** while a `confirm` or `input` is pending for an `id`, the daemon
  sends no further `delta` for that `id` until the matching `respond` arrives.
  The stream pauses at the prompt and resumes after. (`event` messages are not
  tied to an `id` and may still arrive in between.)

---

## Module Interface

Single entry point group: **`nixadmin.modules`**

```toml
# pyproject.toml of any third-party module package
[project.entry-points."nixadmin.modules"]
network = "nixadmin_network:MODULE"
```

### Module dataclass

```python
@dataclass
class Fetcher:
    cmd:            str               # shell command
    timeout:        int  = 15         # seconds
    expose_as_tool: bool = False      # offer to remote agent as a callable tool

@dataclass
class Monitor:
    name:      str
    source:    Literal["poll", "dbus"]
    severity:  Literal["info", "warning", "error"] = "warning"
    # poll
    cmd:       str           = ""
    interval:  int           = 60
    trigger:   Callable      = None   # fn(output: str) -> bool
    # dbus (uses dbus-fast)
    bus:       Literal["system", "session"] = "system"
    interface: str           = ""
    signal:    str           = ""
    filter:    Callable      = None   # fn(*signal_args) -> bool

@dataclass
class ContextProvider:
    name:             str
    get:              Callable[[], Awaitable[str]]   # async
    chain:            Literal["remote", "both"] = "remote"
    refresh_interval: int | None = None   # seconds; None = once per daemon lifetime

@dataclass
class Module:
    spec_version:     int         # ABI version this module was built against
    name:             str
    description:      str         # used by classifier — be descriptive
    fetchers:         list[Fetcher]          = field(default_factory=list)
    monitors:         list[Monitor]          = field(default_factory=list)
    context_provider: ContextProvider | None = None
    routing:          Literal["local", "remote", "auto"] = "auto"
```

### Modules are code, not config

A module is Python that the daemon **executes**: `ep.load()` imports it, and
`trigger` / `filter` are live callables invoked at runtime. Installing a module
is therefore exactly as much trust as installing any pip/Nix package — arbitrary
code runs as the daemon user. There is no sandbox and none is planned; the trust
boundary is package installation, same as the rest of the system. This is a
deliberate choice, stated so nobody designs against an imagined sandbox.

### Discovery & ABI

```python
from importlib.metadata import entry_points

SPEC_VERSION = 1

def load_modules() -> list[Module]:
    out = []
    for ep in entry_points(group="nixadmin.modules"):
        try:
            m = ep.load()
        except Exception as e:
            log.warning("module %s failed to load: %s", ep.name, e)
            continue
        if m.spec_version != SPEC_VERSION:
            log.warning("module %s built for spec v%d, daemon is v%d — skipped",
                        ep.name, m.spec_version, SPEC_VERSION)
            continue
        out.append(m)
    return out
```

A module built against an older `Module` shape is skipped with a warning, not a
crash. Built-in modules (apps, network, disk, services) are always loaded first;
external modules are appended. No ordering guarantees among external modules.

---

## Routing

Routing decides which chain answers a query. It is only a real decision on a
machine that has **both** chains — which is exactly a machine that has a local
model. No local model → remote-only (see below), and there is nothing to route.

### Remote-only machines — a product decision, stated to the user

If no local model is configured there is no local chain. Every query — including
privacy-flagged ones — goes to the remote provider. This is **not** a silent
fallback: on first run, and in the UI, the daemon says so plainly in one honest
sentence ("This machine has no on-device AI, so everything is processed by the
remote provider"). Users who want on-device privacy install a local model;
everyone else gets a working assistant and knows where their data goes.

This dissolves the otherwise-thorny "privacy query but nowhere local to run it"
case: we answer it with product copy, not a config flag.

### Two-stage resolution (machines with both chains)

**Stage 1 — desired chain:**

```
1. explicit  {"chain": "local"|"remote"}   query field, highest priority
2. module hint  routing: "local"           privacy-sensitive matched module
3. daemon default  defaultChain
```

**Stage 2 — reconcile with availability.** Core principle: **the daemon never
silently changes where a query runs.** Any deviation toward remote is a `confirm`
first. A query is **pinned local** if it got there via explicit `{"chain":
"local"}` or a privacy module hint; otherwise it is **soft local** (just the
default).

```
desired remote, remote ready              → remote
desired remote, remote down, local ready  → confirm "use local instead?" → local
desired local,  local ready               → local
desired local,  local warming up:
    status "local model warming up…"
    pinned (explicit/privacy)             → confirm "use remote (leaves device)?"
                                               yes → remote ;  no → wait for local
    soft  (default only)                  → confirm "use remote instead, or wait?"
                                               yes → remote ;  no → wait for local
```

Privacy never leaks by accident: a pinned-local query can only reach remote
through an explicit yes to a confirm that says the data leaves the device. If the
user declines, the daemon waits for local (showing `status`), and errors only if
local never comes up.

> **Future:** repeated confirms are annoying. A later *remember-my-choice* policy
> (per-session or persisted) can suppress them. This is a UX optimization only —
> it does not change the protocol; the daemon still emits the same `confirm`, the
> client just auto-answers from remembered policy.

### Module matching uses the local model — and that's fine

Matched modules (for both routing hints and prefetch) come from `classify`, which
runs on the **local model**. This is not a hidden re-coupling of the chains:
classify only runs when a local model exists, which is the only situation where
routing-by-hint is even possible. A remote-only machine never calls classify.

Cold-start guard: `classify` has a short timeout (~2s). If the local model is
still loading, module-hint routing is skipped for that query and Stage 1 falls
back to explicit-chain-or-default. A warming Ollama therefore never hangs a
query — it only means "no privacy auto-detection this once."

### Routing collision — multiple modules match

`local > auto > remote`. A module declaring `local` wins over any `auto`/`remote`
hint from other matched modules — privacy intent is never silently overridden.
The user can always override with the `{"chain"}` field (Stage 1, level 1).

### What each chain does once chosen

- **local chain** → classify + prefetch (run fetcher commands, inject live data,
  summarize). The cheap model never calls tools.
- **remote chain** → native tool calling. The capable model grounds itself via
  `expose_as_tool` fetchers and built-in tools. No classify, no prefetch — so a
  remote query never *depends* on the local model finishing (only Stage-1
  hint-routing consults classify, and that is timeout-guarded).

---

## Local Call Chain

For small on-device models. No tool calls. Classify → prefetch → summarize.

```
query
  │
  ▼
classify(query, modules)          direct Ollama HTTP, ~20 tokens, temp=0
  │ matched modules
  ▼
prefetch(matched)                 run fetcher commands in parallel (asyncio.to_thread)
  │ context string
  ▼
augment(query, context)           inject live data into user message
  │
  ▼
Ollama /api/chat                  httpx async, streaming
  │ minimal system prompt (one sentence rule, no source mention)
  ▼
stream deltas to client
```

**Minimal system prompt (local):**
```
You are a sysadmin assistant. The user is non-technical.
STRICT: ONE sentence. No lists, no caveats.
Use the inline system data to answer. Never mention the data source.
Never make changes unless explicitly asked.
```

### Action requests on the local chain

The local chain is **read-only** — it has no tools and cannot make changes.
classify therefore also flags *mutation intent* ("install firefox", "fix my
wifi", "turn on bluetooth"). When detected, the local chain does **not** answer:

- remote chain available → the daemon transparently escalates the query to the
  remote chain (and says so: "This needs the full assistant — switching over.").
- remote chain unavailable → the daemon replies plainly that changes can't be
  made right now, rather than letting the cheap model hallucinate a fake "Done!".

This is the read-side mirror of the safety gate: the daemon, not the prompt,
guarantees the local model never pretends to have changed something.

### Grounding guard (no ungrounded answers)

If a query matched a module but prefetch returned no usable data (e.g. the
command failed, or classify matched but Ollama hiccuped), the local chain returns
"I couldn't check that right now" instead of answering from the model's
imagination. A classified-but-empty prefetch must never become a confident guess.

---

## Remote Call Chain

For capable models (cloud or self-hosted). Full agentic loop with tools.
**No classify, no prefetch** — the model grounds itself by calling tools. This
keeps the remote chain independent of the local model / Ollama.

```
query
  │
  ▼
assemble messages:
  [system prompt + context]
  + history.recent(session, 20)        # [] in v1 (NullHistory)
  + [{"role": "user", "content": query}]
  │
  ▼
LiteLLM acompletion(stream=True, tools=exposed_tools)
  │
  ├── text delta → stream to client
  │
  └── tool_call → safety_gate → execute → append result → loop
```

### Tool exposure & the argument rule

**The model never supplies a shell string.** There is no `run_command(cmd)` tool.
This is the core of the tool security model — it closes the arbitrary-execution
hole that a free-form command tool would open.

Tools come in exactly two shapes:

1. **Fetcher-derived tools** — a fetcher with `expose_as_tool=True` becomes a
   zero-argument tool. The command is fixed at module-definition time. The model
   chooses *which* tool to invoke, never *what command* it runs.

   ```
   fetcher  cmd="nmcli -f active,ssid,signal,state dev wifi"  expose_as_tool=True
   →  tool  name="network_wifi_status"  parameters={}   # no args
   ```

2. **Built-in structured tools** — fixed name, typed/enum parameters validated
   against a schema by the daemon before execution. Never free-form.

   ```
   nixadmin_rebuild(action: "test" | "switch" | "boot" | "revert")
   ```

   The daemon rejects any argument outside the schema. A model that emits
   `action: "rm -rf /"` gets a validation error, not an execution.

Write tools (file mutation) are deferred to v2 — see "What's Left Out".

### Safety gate (baked in, not bypassable)

Every privileged tool call passes through the gate in daemon code — not the LLM's
system prompt. In v1 the only privileged tool is `nixadmin_rebuild`:

1. `switch` / `boot` require a `confirm` → wait for `respond: confirmed=true`
2. `switch` is refused unless a `test` succeeded earlier in the same session
3. Execute via the helper socket
4. On failure: report the error verbatim, never auto-retry

`test` and `revert` are non-destructive and run without confirm.

Write tools (file mutation) and their `git stash` / worktree safety flow are
deferred to v2 (see "What's Left Out"). The gate is designed so that adding a
write tool means adding a gate rule, not threading safety through prompts.

---

## Context Providers

Contribute to the remote system prompt. Lazy — called on first query, cached,
re-fetched on `refresh_interval`. Async.

```python
async def machine_profile() -> str:
    # runs nixadmin-apps, lscpu, ip link, df -h
    # calls remote LLM once to summarise into ~200 words
    ...

MACHINE_PROFILE = ContextProvider(
    name="machine-profile",
    get=machine_profile,
    chain="remote",
    refresh_interval=3600,   # re-generate every hour
)
```

The built-in `MachineProfileProvider` also watches the flake dir for git changes
(`git diff HEAD`) — if the config changed since last generation, it refreshes
immediately rather than waiting for the interval.

Future: switch from polling to inotify on the flake dir for instant invalidation.

System prompt assembly (remote chain):
```
[base system prompt]
[context_provider_1 output]
[context_provider_2 output]
...
```

Local chain always uses the minimal hardcoded prompt — no context providers.

---

## Monitors

Declared inside modules. Daemon starts all monitors on boot.

**Poll monitors** — `asyncio` sleep loop, run command, call trigger. Monitor
commands are third-party shell running on a loop as the daemon user, so the
daemon enforces guards: `interval` is clamped to a floor (≥10s), and a global
semaphore caps how many monitor commands run concurrently. A misbehaving module
(`interval=1`, heavy command) cannot hose the machine.

**D-Bus monitors** — `dbus-fast` async subscription. System bus for systemd/NM,
session bus for desktop events.

On trigger: broadcast `EventMsg` to all connected clients.
If no clients connected: send desktop notification via
`org.freedesktop.Notifications` D-Bus interface.

---

## History

v1: `NullHistory` — stateless, no persistence.

Interface (defined now, implemented later). Keyed by `session` so concurrent
clients (terminal + GTK) don't interleave into one conversation:

```python
class HistoryBackend(Protocol):
    async def append(self, session: str, role: str, content: str) -> None: ...
    async def recent(self, session: str, n: int) -> list[dict]: ...
```

`NullHistory` implements both as no-ops / empty lists, so the remote chain's
`history.recent(session, 20)` is safe to call in v1 and simply returns `[]`.

Configured via NixOS module option:

```nix
services.nixadmin.history = "null";   # default
# services.nixadmin.history = "sqlite";  # future
```

Implementations are built-in (not pip entry points).

---

## Dependencies

| Package       | Purpose                        | In nixpkgs? |
|---------------|--------------------------------|-------------|
| `litellm`     | remote provider abstraction    | yes (1.86)  |
| `httpx`       | async HTTP (Ollama calls)      | yes         |
| `dbus-fast`   | async D-Bus for monitors       | check       |
| `dbus-python` | NOT used (sync, blocks loop)   | —           |

---

## NixOS Module Options

```nix
services.nixadmin = {
  enable       = true;
  user         = "steve";
  flakeDir     = "/home/steve/workspace/nixlap";
  hostname     = "laptop";

  # LLM chains
  local.model  = "qwen2.5:3b";
  local.url    = "http://localhost:11434";

  remote.model = "claude-sonnet-4-5";
  remote.base  = "http://localhost:4000";  # Hermes or direct API

  defaultChain = "remote";                # "local" | "remote"

  # History backend
  history = "null";                       # "null" | "sqlite" (future)

  # Extra modules (pip packages with nixadmin.modules entry point)
  modules = [];
};
```

---

## What Is NOT Extensible

- **Safety gate** — hardcoded, auditable. No plugin overrides.
- **Routing core logic** — three levels only, no custom routers.
- **Authentication** — handled by Hermes or API keys externally.
- **LLM backends** — LiteLLM covers this; no `nixadmin.backends` entry point.
- **History implementations** — built-in only; not a pip entry point.

---

## Daemon Startup & Chain Readiness

The daemon starts and accepts client connections immediately. Chains become ready
independently — the `hello` message carries the initial `ready` map (see *Socket
Protocol → Handshake*), and a `ready` push follows when a chain later comes up.

**Local chain** depends on Ollama being up and the model loaded. The daemon
polls `GET /api/ps` with exponential backoff until the model appears. A query
that lands on a not-yet-ready local chain is **not** silently queued — it follows
the Stage-2 reconcile flow (*Routing*): the client gets a `status` ("warming
up…") and a `confirm` offering remote-vs-wait, with pinned-local queries requiring
explicit consent before any remote fallback.

**Remote chain** depends on the configured backend (Hermes proxy or direct API)
being reachable. Same polling approach. If a remote API key is set but the
endpoint is unreachable, the chain stays unready and the client is told.

Clients receive a `{"type": "ready", "chain": "local"}` push when a chain
becomes available.

---

## What's Left Out of v1

- Conversation history persistence (NullHistory only)
- Write tools / file mutation tools (edit_nix_file etc.) — safety design deferred
- Desktop notification fallback when no client connected (stub only)
- Module hot-reload (restart daemon after nixos-rebuild switch)
- Web UI / remote socket access
- Voice input / screen capture modules
- GTK client
