# Working with an AI on a NixOS Project — Approach & Log

> **Historical.** Written while nixadmin lived inside the
> [nixlap](https://github.com/hinstef/nixlap) config repo as
> `modules/nixos/`, covering the v1 Python CLI and the v2 container design
> (May 2026). Every module path it names has since moved or been deleted.
>
> Current equivalents: [`../PROGRESS.md`](../PROGRESS.md) for status,
> [`../adr/`](../adr/) for decisions. Kept for the decision log below — notably
> why ROCm was abandoned for Vulkan on the Radeon 780M (gfx1103), which is not
> recorded anywhere else.

This document captures how this project is developed collaboratively with Claude Code (claude-sonnet-4-6 via the CLI). It is intended to be readable by someone unfamiliar with the project who wants to understand the workflow.

---

## The Core Idea

An opinionated, flake-based NixOS laptop config that manages itself with a locally-running LLM assistant. The assistant can read the config, propose changes, run test builds, and apply them — all with user confirmation and git-backed rollback.

The project demonstrates a tight loop between:
1. Declarative infrastructure (NixOS modules, git history as ground truth)
2. An AI agent with bounded tools (read/edit files, run builds, read logs)
3. A safety model (change tiers, human confirmation, sandbox)

---

## Collaboration Style

### How sessions work

Each session starts with Claude reading the current git state and memory files. The memory system (`/home/<user>/.claude/projects/.../memory/`) stores facts about the project that persist across conversations — things like conventions, open problems, and architectural decisions — so Claude doesn't need to re-derive them from scratch each time.

Within a session, the loop is:
1. **Discuss** — describe a goal or problem in plain language
2. **Analyze** — Claude reads the relevant files before suggesting anything
3. **Spec** — for larger changes, write a spec first and align on the approach
4. **Implement** — write the code, in small targeted steps
5. **Iterate** — refine based on what breaks or what changes in requirements

### What works well

- **Spec-first for non-trivial changes.** Writing the spec before any code forces both human and AI to agree on architecture before getting into implementation details. When requirements change mid-spec (e.g. "bundle Ollama inside the container"), updating the spec is quick and the resulting code is cleaner.
- **Incremental phases.** Breaking work into phases (GPU first, then logs, then sandbox, then module cleanup) means each phase can be tested before the next begins. It also makes it easy to stop and re-prioritize.
- **Small edits over full rewrites.** Claude uses `Edit` (exact-string replacement) rather than rewriting whole files. This produces minimal diffs, preserves comments, and makes review easy.
- **Memory for cross-session continuity.** Conventions ("user packages go in `home.packages`", "never edit hardware-configuration.nix") are saved to memory files so they don't need to be re-stated each session.

### What to watch out for

- **The AI reads before it writes.** If you ask Claude to modify something without reading it first, push back — guessing file contents leads to broken edits. The workflow is always: read → understand → edit minimally.
- **Specs drift.** As implementation reveals new constraints, update the spec. Otherwise the spec becomes misleading.
- **Don't over-specify phases.** Phase 1 originally said "build the container image" but that was getting ahead of itself — the right Phase 1 goal is "confirm GPU offload works", however that's achieved.

---

## Project Timeline & Decision Log

### Initial state (before this work)

- Basic NixOS flake config: KDE Plasma 6, zen kernel, LUKS encryption, TPM unlock, Btrfs
- v1 AI admin module (`ai-sysadmin.nix` + `nixadmin.py`): Python CLI that talks to Ollama, edits nix files, runs `nixos-rebuild test`, commits to git
- Problem: Ollama's stock ROCm build doesn't target `gfx1103` (Radeon 780M iGPU)
- Workaround attempt: `ollama-experiment.nix` — a second Ollama instance on port 11435 with `rocmGpuTargets = ["gfx1103"]` and `clr.icd` on the host

### Spec session (2026-05-08)

**Goal:** Redesign the AI admin for production use.

**Decisions made:**

| Decision | Rationale |
|----------|-----------|
| OCI container (Podman) over Flatpak | Flatpak is designed for GUI apps; journald socket access, bind-mounts, and daemon lifecycle are all awkward. Podman + `oci-containers` maps cleanly onto systemd. |
| Bundle Ollama inside the container | Makes the module self-contained — drop it into any NixOS config and it brings its own inference engine. No host-level Ollama service to manage. |
| Privileged helper daemon for `nixos-rebuild` | `nixos-rebuild switch` needs root. Rather than give the LLM container any sudo, a tiny privileged systemd service listens on a Unix socket and accepts only `test|switch|revert` commands. |
| Model weights in a named Podman volume | A 7B Q4 model is ~4 GB. Baking it into the OCI image would mean rebuilding 4 GB on every `nixos-rebuild switch`. The named volume persists across image rebuilds. |
| `rocmGpuTargets = ["gfx1103"]` + `clr.icd` + `HSA_OVERRIDE_GFX_VERSION` | Three things must all be true simultaneously for ROCm to use the 780M: the binary must be compiled for the right GFX target, the ICD loader must be present on the host, and the runtime must be told the GFX version. |

Full spec: [`docs/nixadmin-v2-spec.md`](nixadmin-v2-spec.md)

### Phase 1 implementation (2026-05-08)

Created `modules/nixos/nixadmin.nix`:
- `pkgs.ollama-rocm.override { rocmGpuTargets = ["gfx1103"]; }` — targeted Ollama build
- `pkgs.dockerTools.buildLayeredImage` — deterministic OCI image, no Dockerfile
- `virtualisation.oci-containers.containers.nixadmin-ollama` — systemd-managed Podman container
- `hardware.graphics.extraPackages = [rocmPackages.clr.icd]` — host-side ICD
- `HSA_OVERRIDE_GFX_VERSION = "11.0.3"` + `HSA_ENABLE_SDMA = "0"` — runtime overrides
- `--device=/dev/kfd --device=/dev/dri` — GPU device passthrough into container
- Named volume `nixadmin-models:/models` — persistent model weights

Replaced `ollama-experiment.nix` in `hosts/laptop/default.nix`.

**Next step:** Run `sudo nixos-rebuild switch --flake .#laptop`, pull a model, and verify GPU offload:
```bash
podman exec nixadmin-ollama ollama pull qwen2.5-coder:7b
podman logs nixadmin-ollama | grep -iE "gpu|offload|gfx|layer"
```

### Session 2 (2026-05-22–23)

**Goals:** Activate the container, verify GPU, harden the setup, and settle on a TUI.

**Decisions made:**

| Decision | Rationale |
|----------|-----------|
| Switch from ROCm to Vulkan (`pkgs.ollama-vulkan`) | ROCm on gfx1103 requires overriding GPU targets, ICD loader, and HSA env vars — and still failed to initialize. Vulkan via Mesa RADV works out of the box with a mount of `/run/opengl-driver` and `/nix/store`, accepts a small performance penalty. |
| Rootless Podman (`systemd.user.services`) | Removes need for `sudo podman exec`. Container runs as `steve` under the user systemd slice. |
| Start after `graphical-session.target` | Keeps the slow container start off the boot critical path. |
| Image load cached via stamp file | `podman load` from the Nix store path only re-runs after `nixos-rebuild switch` changes the image, not on every container restart. |
| NOPASSWD sudoers for `nixos-rebuild` and `podman exec` | Lets Claude Code run rebuilds autonomously without fingerprint/password prompts. `nixadmin` system user reserved for future Phase 3 helper daemon. |
| pi.dev as TUI instead of custom nixadmin.py | Avoids building a TUI from scratch. pi.dev supports Ollama via `models.json`. Custom tools will be written as pi.dev extensions (TypeScript). |
| `nix-pi` flake (`github:hinstef/nix-pi`) | Packages `@earendil-works/pi-coding-agent` declaratively. Needed because it's not in nixpkgs. Works around two bugs in the published shrinkwrap (missing integrity hashes, missing devDep entries). |

**GPU verification result:**
```
inference compute: library=Vulkan name=Vulkan0
description="AMD Radeon 780M Graphics (RADV PHOENIX)"
type=iGPU  total="19.6 GiB"  available="18.1 GiB"
```
Vulkan offload confirmed working. Phase 1 complete.

---

## Pending Phases

The spec's Phase 4 (custom Python TUI) is replaced by pi.dev. Phases 3 and 5 are unchanged.

| Phase | What | Status |
|-------|------|--------|
| 1 | GPU acceleration — Ollama in container with Vulkan | **Done** |
| 2 | Configure pi.dev — point at Ollama container, test end-to-end | Next |
| 3 | Privileged helper — nixos-rebuild over Unix socket (replaces NOPASSWD sudoers long-term) | Not started |
| 4 | ~~Custom TUI~~ → pi.dev extensions for NixOS tools | Not started |
| 5 | Module cleanup — remove remaining hardcoded values | Not started |

---

## Key Files

```
flake.nix                          Entry point
hosts/laptop/default.nix           Host config — what's enabled and with what options
modules/nixos/nixadmin.nix         AI admin module (v2, active): Vulkan Ollama, rootless Podman, sudoers
modules/nixos/ai-sysadmin.nix      v1 module (disabled, kept for reference)
modules/nixos/ai-sysadmin/nixadmin.py   v1 agent code
docs/nixadmin-v2-spec.md           Full architecture spec (Phase 4 superseded by pi.dev)
docs/approach.md                   This file
github:hinstef/nix-pi              Nix flake packaging pi-coding-agent
```
