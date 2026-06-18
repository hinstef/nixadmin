# Vision

> **A computer you can give to someone you love.**

## The goal: making compute human

Computing's whole history is humans adapting to machines — learn the syntax, the
menus, the filesystem, the terminal. The goal here is the **inversion**: the
person states intent in their own terms, and the machine handles the how.

That promise is 40 years old and unfulfilled because the missing piece was
natural-language intent. The LLM finally supplies it — but a **chatbot doesn't
make compute human; it just adds a chat window.** The "human" part isn't the
talking. It's that the machine *acts safely, explains itself, and the person
never has to think in machine terms.*

**NixOS and the LLM are implementation details to this vision.** The durable
asset is the loop:

```
human intent → understand → act SAFELY → explain → (notice & offer)
            (model: swappable)  (substrate: swappable)  (model)  (ambient)
```

Deterministic core, model only at the edges, safety as infrastructure. It is
OS-agnostic and model-agnostic by design.

## Core principle: derive, don't hardcode

Knowledge lives in **live state we can gather** (the system, nixpkgs, docs we can
pull) — never baked into the codebase. The model's job is to **infer and judge over
that real state**, not to store facts or recall them. (We proved the model is bad
at recall and good at judgment: it fabricates package names, but correctly picks
`steam` from real candidates, and diagnoses a dead panel from real process state.)

This has been learned the hard way, repeatedly — every time, the fix was the same:

| Hardcoded (wrong) | Derived + inferred (right) |
|---|---|
| `COMMON_APPS` list for typos | difflib over real `attrNames` → model judges |
| `ALIASES` (chrome→google-chrome) | model maps the phrase to real candidates |
| "a healthy COSMIC session = [list]" | dump live process/unit state → model infers from the symptom |

The test for any new feature: **am I encoding knowledge, or gathering state and
letting the model interpret it?** If the former, stop — derive it instead. Future
grounding sources (pulled docs, nixpkgs metadata, man pages) extend this, never
replace it with baked-in facts.

## The honest catch

NixOS is an implementation detail to the *vision* but **load-bearing for the
*promise* today.** The reason "make compute human" doesn't already exist isn't a
lack of LLMs — it's that on an imperative OS, letting an AI change your system is
reckless. NixOS makes machine-made changes *safe*: a bad edit fails `nix eval`
before it touches anything; changes are tested in an isolated worktree, applied
atomically, and roll back to the previous generation. **No imperative OS gives an
AI agent that.**

So: keep the vision substrate-agnostic, but don't discard the substrate that
delivers the "safe" before rebuilding that property elsewhere.

## Differentiation — a *values* definition of "human"

Apple/Google/Microsoft are all now pitching "human" AI computing. We will never
out-feature them. But they are structurally incapable of the parts that make it
*genuinely* humane:

| Defensible (our lane) | Commoditizable (don't lean here) |
|---|---|
| Safe **writes** (Nix atomicity + confirm + rollback) | Reading / explaining system state |
| **Local-first**, privacy, never-silent routing | General chat about Linux |
| **Deterministic-core** trust model | The specific LLM used |
| **Ambient** / proactive monitoring | |

The giants want cloud, lock-in, telemetry. Our "human" is humane **to the person,
not the platform**: on your device, reversible, yours, no surveillance, honest
("this will leave your device"). That's a lane they can't follow into.

## Go to market: caring distribution

The first person who sets this up is a nerd. That's the **bootstrap, not the
ceiling** — Linux, the web, git, the PC all started nerd-for-nerd and crossed
over.

- **Caring distribution.** The nerd installs it for the people they love — "I set
  up my mum's laptop." That is a real, powerful distribution channel (and a
  brutal usability test: a loved one can't be fooled by a clever demo).
- **The substrate goes invisible.** The mainstream future doesn't require normal
  people to "get" NixOS — nobody "installs Linux" to use Android or a Steam Deck.
  Someone ships a thing where the safe substrate is preinstalled and unseen. The
  substrate-agnostic loop is what survives that transition.

**Discipline:** don't build for the hypothetical mainstream user yet. Build for
the **nerd-and-their-household** — a real person with a real unmet need today.
The bar is *"a year later, never burned."* Nail that and the rest is distribution.

## The one-liner test

Good names here encode the *relationship* (care, trust, handing it to someone),
not the *capability* (smart, fast, AI). Every giant competes on capability; nobody
competes on *"you can trust this with your mum."*

- **"A computer you can give to someone you love."** — the north-star promise.
- **"Caring distribution."** — the project/community name and the channel.
- **"Computing that's humane to the person, not the platform."** — for the nerds
  who feel the surveillance/lock-in angle.

The value, in one line: **the first computer you can hand to someone who doesn't
understand computers, that won't betray them — to the machine's complexity, or to
a platform.**
