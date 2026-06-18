# nixadmin

> **A computer you can give to someone you love.**
> The goal is making compute *human* — a machine that adapts to the person, not
> the other way around. NixOS and the LLM are implementation details; the product
> is the safe, private, explainable loop between human intent and machine state.
> See [`docs/vision.md`](docs/vision.md).

Ambient system intelligence for NixOS — a daemon that knows your machine's state,
explains it in plain language, and (with consent) fixes it. Not a chatbot: a
personal observability layer with a conversational interface on top.

> **Status:** v3 rewrite in progress on `feat/v3-daemon`. Greenfield from
> [`docs/nixadmin-v3-spec.md`](docs/nixadmin-v3-spec.md) — the spec is the source
> of truth; this README is a map.

## How it works

```
clients (terminal · GTK · web)
        │  Unix socket, newline-delimited JSON  (src/nixadmin/protocol.py)
        ▼
   nixadmin daemon
        ├── modules        capability bundles, discovered via entry points (sdk.py)
        ├── local chain    cheap on-device model: classify → prefetch → summarize
        ├── remote chain   capable model via LiteLLM: full tool-calling agent
        ├── router         two-stage; never silently changes where a query runs
        ├── safety gate    privileged actions gated in code, not in prompts
        └── monitors       D-Bus + poll watches → proactive events
```

Two LLM tiers, fully independent:

| Chain  | Model              | Mechanism                         | Tools |
|--------|--------------------|-----------------------------------|-------|
| local  | small Ollama model | classify + prefetch + summarize   | none  |
| remote | LiteLLM (Hermes subscription, API keys, OpenRouter…) | native tool calling | yes |

## Writing a module

A module teaches nixadmin about one domain. It depends only on
[`nixadmin.sdk`](src/nixadmin/sdk.py) (stdlib-only — no daemon deps needed):

```python
from nixadmin.sdk import Module, Fetcher, SPEC_VERSION

manifest = Module(
    spec_version=SPEC_VERSION,
    name="docker",
    description="containers, images, docker, compose",
    fetchers=[Fetcher(name="ps", cmd="docker ps", description="Running containers")],
)
```

Register it via an entry point and the daemon discovers it:

```toml
[project.entry-points."nixadmin.modules"]
docker = "nixadmin_docker:manifest"
```

## Development

```bash
# run the smoke tests
nix shell --impure --expr 'let p = import <nixpkgs> {}; in
  p.python313.withPackages (ps: [ ps.pytest ps.pytest-asyncio ps.structlog ])' \
  --command python -m pytest -q
```

A proper dev shell and the NixOS module land with the daemon itself.

## License

MIT — see [LICENSE](LICENSE).
