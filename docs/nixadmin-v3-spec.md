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
│  context assembly  (system prompt built at startup) │
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

---

## Socket Protocol

Newline-delimited JSON over a Unix socket at `$XDG_RUNTIME_DIR/nixadmin.sock`.

### Handshake

On connect the daemon immediately sends:

```json
{"type": "hello", "version": 1, "chains": ["local", "remote"],
 "modules": ["apps", "network", "disk", "services"], "default_chain": "remote"}
```

Client must check `version`. If unsupported, disconnect.

### Client → Daemon

```json
{"type": "query",   "id": "abc", "text": "is my wifi working?"}
{"type": "query",   "id": "abc", "text": "...", "chain": "local"}
{"type": "cancel",  "id": "abc"}
{"type": "respond", "id": "abc", "confirmed": true}
{"type": "respond", "id": "abc", "value": "firefox"}
```

- `chain` is optional. Omit to use daemon default or module hint.
- `respond` answers a pending `confirm` or `input` request by `id`.
  - For `confirm`: use `"confirmed": bool`
  - For `input`: use `"value": string`

### Daemon → Client

```json
{"type": "delta",   "id": "abc", "text": "Yes, your WiFi is connected."}
{"type": "done",    "id": "abc"}
{"type": "error",   "id": "abc", "text": "backend unavailable"}
{"type": "confirm", "id": "abc", "text": "Apply config change?"}
{"type": "input",   "id": "abc", "prompt": "Package name:"}
{"type": "event",   "source": "monitor.service-failed",
                    "severity": "error", "text": "nginx stopped — port 80 in use"}
```

- `delta` / `done` / `error` / `confirm` / `input` are scoped to a query `id`.
- `event` is unsolicited, broadcast to all connected clients.
- Severity: `"info"` | `"warning"` | `"error"`

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
    name:             str
    description:      str         # used by classifier — be descriptive
    fetchers:         list[Fetcher]          = field(default_factory=list)
    monitors:         list[Monitor]          = field(default_factory=list)
    context_provider: ContextProvider | None = None
    routing:          Literal["local", "remote", "auto"] = "auto"
```

### Discovery

```python
from importlib.metadata import entry_points

def load_modules() -> list[Module]:
    return [ep.load() for ep in entry_points(group="nixadmin.modules")]
```

Built-in modules (apps, network, disk, services) are always loaded.
External modules are appended. No ordering guarantees.

---

## Routing — Three Levels

```
1. Query field  {"chain": "local"}          highest priority, client explicit
2. Module hint  module.routing = "local"    privacy-sensitive modules
3. Daemon default  config.default_chain     "remote" out of the box
```

Level 1 always wins. Level 2 applies when no explicit override. Level 3 is the
fallback.

A user or client can always escalate: sending `"chain": "remote"` on a module
that declares `routing: "local"` is allowed — the user is making an informed
choice.

### Routing collision — multiple modules match

When a query matches modules with different routing hints, the rule is:

```
local > auto > remote
```

Privacy intent is never silently overridden. A module declaring `local` wins
over any `auto` or `remote` hint from other matched modules. The user can
override via `"chain": "remote"` in the query (level 1).

**Important:** routing only controls which LLM processes the query. Prefetch
always runs regardless — fetcher commands execute locally on the machine. The
augmented query (with live data injected) then goes to whichever chain won.

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

---

## Remote Call Chain

For capable models (cloud or self-hosted). Full agentic loop with tools.

```
query
  │
  ▼
assemble messages:
  [system prompt + context]
  + history.recent(20)
  + [{"role": "user", "content": query}]
  │
  ▼
LiteLLM acompletion(stream=True, tools=exposed_tools)
  │
  ├── text delta → stream to client
  │
  └── tool_call → safety_gate → execute → append result → loop
```

### Tool exposure

Only fetchers with `expose_as_tool=True` are offered to the remote agent.
The safety gate in the daemon enforces this — the LLM cannot call arbitrary
commands.

Built-in privileged tools (always available to remote chain):
- `nixadmin_rebuild(action)` — routes through helper socket, requires confirm
- `edit_nix_file(path, old, new)` — requires git stash + test + confirm

### Safety gate (baked in, not bypassable)

Every action passes through the gate before execution:

1. `git stash push` before any file edit
2. `nixadmin_rebuild test` before any `switch`
3. `confirm` sent to client, wait for `respond: true`
4. Execute
5. On failure: `git stash pop`, report error

The gate is in daemon code, not in the LLM's system prompt.

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

**Poll monitors** — `asyncio` sleep loop, run command, call trigger.

**D-Bus monitors** — `dbus-fast` async subscription. System bus for systemd/NM,
session bus for desktop events.

On trigger: broadcast `EventMsg` to all connected clients.
If no clients connected: send desktop notification via
`org.freedesktop.Notifications` D-Bus interface.

---

## History

v1: `NullHistory` — stateless, no persistence.

Interface (defined now, implemented later):

```python
class HistoryBackend(Protocol):
    async def append(self, role: str, content: str) -> None: ...
    async def recent(self, n: int) -> list[dict]: ...
```

Configured via NixOS module option:

```nix
services.nixadmin.history = "null";   # default
# services.nixadmin.history = "sqlite";  # future
```

Implementations are built-in (not pip entry points).
History is per-daemon-session in v1 (cleared on restart).

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
independently — clients are informed via the `hello` message:

```json
{"type": "hello", "version": 1, "chains": ["local", "remote"],
 "ready": {"local": false, "remote": true}, "modules": [...]}
```

**Local chain** depends on Ollama being up and the model loaded. The daemon
polls `GET /api/ps` with exponential backoff until the model appears. Queries
on the local chain are queued until ready.

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
