# nixadmin v2 — Specification

> **Historical — not the current design.** Superseded by
> [`../nixadmin-v3-spec.md`](../nixadmin-v3-spec.md).
>
> v2 put the agent and Ollama inside a rootless Podman container talking to a
> privileged helper over a Unix socket. v3 dropped the container for a daemon.
> The helper socket is the one idea that carried over; the GPU-in-a-container
> work below is dead, though the ROCm-to-Vulkan findings for gfx1103 are still
> the reason that path was abandoned.

**Status:** Superseded (was: Draft)
**Date:** 2026-05-08

---

## 1. Goals

| # | Goal |
|---|------|
| G1 | LLM runs **locally** on the Radeon 780M iGPU with ROCm acceleration |
| G2 | Agent has **read access to the NixOS config repo** and can propose+apply edits |
| G3 | Agent can **read systemd journal logs** to help debug system issues |
| G4 | Agent is packaged as a **reusable NixOS module** (no hardcoded username/path) |
| G5 | Agent process runs in a **sandbox** that limits what it can touch on the host |

---

## 2. Current State & Problems

### What exists (v1)

- `modules/nixos/ai-sysadmin.nix` — NixOS module enabling Ollama + the CLI agent
- `modules/nixos/ai-sysadmin/nixadmin.py` — Python REPL: tool-calling LLM chat, tiered change confirmation, git commit/snapshot workflow
- `modules/nixos/ollama-experiment.nix` — isolated second Ollama instance used to debug GPU offload

### Open problems

| # | Problem | Notes |
|---|---------|-------|
| P1 | **GPU not offloading** | Stock `ollama-rocm` doesn't target `gfx1103` (780M); workaround in `ollama-experiment.nix` exists but hasn't confirmed success yet |
| P2 | **No sandbox** | `nixadmin` runs as the normal user with full home directory access |
| P3 | **Hardcoded identity** | `configPath`, `user`, hostname baked in — not reusable on another machine |
| P4 | **No log access** | Agent can only edit files; can't read `journalctl` output to help debug |
| P5 | **Sudo for rebuild** | `nixos-rebuild switch` requires `sudo`; hard to restrict in a container |

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Host (NixOS)                                               │
│                                                             │
│  ┌────────────────────────────────────────────────────┐    │
│  │  OCI container (Podman, rootless)                  │    │
│  │                                                    │    │
│  │  ollama serve  ←── bundled binary + model weights  │    │
│  │       ↑  localhost:11434                           │    │
│  │  nixadmin.py                                       │    │
│  │    ├─ reads/writes  /config  (bind-mount, rw)     │    │
│  │    ├─ reads         /run/log/journal  (ro)         │    │
│  │    └─ HTTP          localhost:11434                │    │
│  │                                                    │    │
│  │  devices: /dev/kfd  /dev/dri/renderD*  (ro)       │    │
│  └──────────────────────┬─────────────────────────────┘    │
│                         │ Unix socket                       │
│  ┌──────────────────────▼─────────────────────────────┐    │
│  │  nixadmin-helper.service  (small privileged svc)   │    │
│  │    • accepts: test | switch | revert               │    │
│  │    • runs: nixos-rebuild --flake <configPath>      │    │
│  │    • streams output back over socket               │    │
│  └────────────────────────────────────────────────────┘    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

- **OCI container (Podman rootless) over Flatpak.** Flatpak is designed for desktop GUI apps; accessing journald sockets, bind-mounting a repo, and running a daemon are all awkward. Podman rootless containers map cleanly onto systemd services via `virtualisation.oci-containers` and give precise bind-mount and device control.
- **Ollama bundled inside the container.** The container ships both the Ollama binary (compiled for `gfx1103`) and the recommended model weights as a named Podman volume. No `ollama.service` on the host. GPU access is granted via device passthrough (`/dev/kfd`, `/dev/dri/renderD*`). This makes the module fully self-contained — drop it into any NixOS config and it works.
- **Privileged helper daemon** separates the only action that needs root (`nixos-rebuild`) from the untrusted LLM process. The helper validates the command before execution and streams back output. The container has zero sudo access.

---

## 4. GPU Acceleration (P1 fix)

The 780M (Navi 33 iGPU, `gfx1103`) is not in ROCm's default allowlist. Two things must be true simultaneously:

Because Ollama runs inside the container, all GPU configuration is handled at image build time and container launch time — no `services.ollama` on the host needed.

### 4a. Compile Ollama against gfx1103 (inside the image)

```nix
ollamaRocmGfx1103 = pkgs.ollama-rocm.override {
  rocmGpuTargets = [ "gfx1103" ];
};

nixadminImage = pkgs.dockerTools.buildLayeredImage {
  name = "nixadmin";
  contents = [
    ollamaRocmGfx1103
    # … agent deps
  ];
};
```

This recompiles only Ollama's HIP/ggml backend against `gfx1103` — not the whole ROCm stack.

### 4b. OpenCL ICD on the host + device passthrough into the container

The ROCm runtime needs the ICD loader installed on the **host** (so `/dev/kfd` and `/dev/dri` are available), but the Ollama binary and its ROCm libs live **inside the container**:

```nix
# host-level, in modules/nixos/nixadmin.nix
hardware.graphics.extraPackages = with pkgs; [
  rocmPackages.clr.icd   # platform discovery for /dev/kfd
];
```

Container gets device access:

```nix
extraOptions = [
  "--device=/dev/kfd"
  "--device=/dev/dri"
  "--group-add=render"
  "--group-add=video"
];
```

Runtime env vars injected into the container:

```nix
environment = {
  HSA_OVERRIDE_GFX_VERSION = cfg.rocmOverrideGfx;   # "11.0.3"
  HSA_ENABLE_SDMA          = "0";
  OLLAMA_HOST              = "127.0.0.1:11434";
};
```

### 4c. Verification

```bash
# Tail the container log during first inference
podman logs -f nixadmin 2>&1 | grep -iE "gpu|layer|offload|gfx|error"
# Expected: "offload 32/32 layers to GPU" or similar
```

---

## 5. Sandbox: OCI Container

### 5a. What the container gets

| Resource | Access | How |
|----------|--------|-----|
| Config repo | Read-write | `--volume <configPath>:/config:rw` |
| systemd journal | Read-only | `--volume /run/log/journal:/run/log/journal:ro` + `/etc/machine-id:/etc/machine-id:ro` |
| Rebuild socket | Write | `--volume /run/nixadmin-helper.sock:/run/nixadmin-helper.sock` |
| GPU | Device passthrough | `--device=/dev/kfd --device=/dev/dri` |
| Model weights | Persistent volume | `podman volume: nixadmin-models` → `/models` |
| Internet | None | `--network=none` except loopback; Ollama runs inside the container |
| `/home`, `/etc`, `/nix/store` | None | Not mounted |

### 5b. Container image

Built with `pkgs.dockerTools.buildLayeredImage` — deterministic, from the Nix store, no Dockerfile, no external registries:

```nix
nixadminImage = pkgs.dockerTools.buildLayeredImage {
  name    = "nixadmin";
  tag     = "latest";
  contents = [
    ollamaRocmGfx1103                                              # bundled inference engine
    (pkgs.python3.withPackages (ps: with ps; [
      httpx rich prompt-toolkit
    ]))
    pkgs.git
    pkgs.systemd                                                   # journalctl for log reading
  ];
  config = {
    Cmd = [ "${nixadminScript}/bin/nixadmin" ];
    Env = [
      "OLLAMA_MODELS=/models"
      "OLLAMA_HOST=127.0.0.1:11434"
    ];
  };
};
```

### 5c. Model weights — persistent named volume

Model weights (~4 GB for a 7B Q4 model) are **not** baked into the image. They live in a named Podman volume that survives image rebuilds:

```
podman volume: nixadmin-models  →  /models  inside the container
```

On first start the container's entrypoint pulls the configured model if it isn't present:

```bash
#!/bin/sh
# entrypoint.sh (wrapped by nixadmin shell script)
ollama serve &
OLLAMA_PID=$!
sleep 2
ollama pull "${NIXADMIN_MODEL:-qwen2.5-coder:7b}"
exec python3 /nixadmin.py
```

Subsequent starts skip the pull (weights already in `/models`). Upgrading the model is as simple as changing `cfg.model` and running `nixos-rebuild switch` — the old weights stay in the volume until manually pruned with `podman volume prune` or `ollama rm`.

### 5d. NixOS module wiring (oci-containers)

```nix
virtualisation.oci-containers.containers.nixadmin = {
  image       = "nixadmin:latest";
  imageStream = nixadminImage;          # built by Nix, loaded automatically
  volumes = [
    "${cfg.configPath}:/config:rw"
    "/run/log/journal:/run/log/journal:ro"
    "/etc/machine-id:/etc/machine-id:ro"
    "/run/nixadmin-helper.sock:/run/nixadmin-helper.sock"
    "nixadmin-models:/models"
  ];
  environment = {
    NIXADMIN_MODEL       = cfg.model;
    NIXADMIN_CONFIG_PATH = "/config";
    NIXADMIN_HOSTNAME    = cfg.hostname;
    HSA_OVERRIDE_GFX_VERSION = cfg.rocmOverrideGfx;
    HSA_ENABLE_SDMA      = "0";
  };
  extraOptions = [
    "--device=/dev/kfd"
    "--device=/dev/dri"
    "--group-add=render"
    "--group-add=video"
    "--security-opt=no-new-privileges"
    "--network=none"
  ];
};
```

---

## 6. Privileged Helper Service

A minimal Python or shell daemon that:

1. Listens on a Unix socket at `/run/nixadmin-helper.sock`
2. Accepts JSON messages: `{"action": "test"|"switch"|"revert", "target": "<ref>"}`
3. Validates the action is in the allowed set (no arbitrary commands)
4. Runs `nixos-rebuild <action> --flake <configPath>#<hostname>` as root
5. Streams stdout/stderr back line by line over the socket
6. Closes connection when the subprocess exits

The helper is a systemd service with:
```nix
systemd.services.nixadmin-helper = {
  serviceConfig = {
    User = "root";
    ExecStart = "${nixadminHelper}/bin/nixadmin-helper";
    RuntimeDirectory = "nixadmin-helper";
    # Harden everything except what's needed for nixos-rebuild
    ProtectHome = "read-only";
    ProtectSystem = "full";
  };
};
```

---

## 7. Log Reading Tool

New tool added to the agent's tool set:

```python
def tool_read_logs(unit: str = "", lines: int = 100, since: str = "1h ago") -> str:
    """Read systemd journal. unit="" reads system-wide recent errors."""
    args = ["journalctl", "--no-pager", f"-n {lines}", f"--since={since}"]
    if unit:
        args += ["-u", unit]
    else:
        args += ["-p", "err"]   # errors and above when no unit specified
    result = subprocess.run(args, capture_output=True, text=True)
    return result.stdout or result.stderr
```

Added to `TOOL_DEFINITIONS` so the LLM can call it when asked to diagnose problems.

**Scope limits:** The tool only reads — it cannot write to the journal, restart services, or kill processes.

---

## 8. Reusable Module Design (P3 fix)

All identity removed from the module. New option set:

```nix
options.services.nixadmin = {
  enable          = lib.mkEnableOption "nixadmin AI sysadmin";
  configPath      = lib.mkOption { type = lib.types.path; };   # required, no default
  hostname        = lib.mkOption { type = lib.types.str; };    # flake target, e.g. "laptop"
  user            = lib.mkOption { type = lib.types.str; };    # who runs the container
  model           = lib.mkOption { type = lib.types.str; default = "qwen2.5-coder:7b"; };
  # ollamaUrl is internal to the container (127.0.0.1:11434) — not exposed as an option
  rocmOverrideGfx = lib.mkOption { type = lib.types.str; default = "11.0.3"; };
  enableAutoSnapshots = lib.mkOption { type = lib.types.bool; default = true; };
};
```

Usage in `hosts/laptop/default.nix`:

```nix
services.nixadmin = {
  enable     = true;
  configPath = "/home/<user>/workspace/nixlap";
  hostname   = "laptop";
  user       = "steve";
  model      = "qwen2.5-coder:7b";
};
```

---

## 9. Tool Set Summary (v2)

| Tool | What it does | Sandbox boundary |
|------|-------------|-----------------|
| `read_file` | Read any `.nix` file in config repo | Container `/config` (ro read) |
| `edit_file` | Replace exact string in a config file | Container `/config` (rw) |
| `list_files` | List `.nix` files in config repo | Container `/config` |
| `read_logs` | Read journald output by unit/priority | Container `/run/log/journal` (ro) |
| `nixos_rebuild_test` | Dry-run rebuild via helper socket | Unix socket → privileged helper |
| `nixos_rebuild_switch` | Apply and activate via helper socket | Unix socket → privileged helper |

`nixos_rebuild_switch` is separated from `test` so the LLM cannot silently apply changes. The Python layer still runs the tiered confirmation workflow before calling it.

---

## 10. Safety Model (unchanged from v1, carried forward)

- `hardware-configuration.nix` is blocked from editing
- Changes are classified Tier 1 / 2 / 3 by content patterns
- Tier 3 (boot, luks, PAM, root) requires typing `"YES I UNDERSTAND"`
- All changes are committed to git before `switch`; auto-snapshot tags after success
- `git restore .` on any denied or failed change

---

## 11. Implementation Phases

### Phase 1 — Fix GPU acceleration
- Build the container image with `ollamaRocmGfx1103` (`rocmGpuTargets = ["gfx1103"]`)
- Add `rocmPackages.clr.icd` to `hardware.graphics.extraPackages` on host
- Pass `--device=/dev/kfd --device=/dev/dri` + HSA env vars to container
- Confirm GPU offload before proceeding

### Phase 2 — Log reading
- Add `read_logs` tool to existing `nixadmin.py` (no infra change needed)
- Test with real journal on the current unsandboxed setup

### Phase 3 — Privileged helper
- Write `nixadmin-helper` (Python or shell, ~100 lines)
- Expose as systemd service with Unix socket
- Update `nixadmin.py` to call the socket instead of running `sudo nixos-rebuild` directly

### Phase 4 — Container sandbox
- Build OCI image with `dockerTools.buildLayeredImage`
- Wire up `virtualisation.oci-containers` service
- Test bind-mount access to config repo and journal socket

### Phase 5 — Reusable module
- Parameterise all hardcoded values
- Split into `modules/nixos/ollama.nix` + `modules/nixos/nixadmin.nix`
- Update `hosts/laptop/default.nix` to use the new module options

---

## 12. Open Questions

| # | Question | Notes |
|---|----------|-------|
| Q1 | **Model choice:** `qwen2.5-coder:7b` vs `qwen2.5:14b` vs `gemma3:12b` | 7B fits comfortably in 780M's shared VRAM; 14B may need partial offload — evaluate after GPU confirmed working |
| Q2 | **Model pull on first start requires internet** | The container has `--network=none`; the initial `ollama pull` needs network access. Options: (a) lift `--network=none` only for the pull phase, (b) pre-seed the volume on host before first start, (c) use a separate one-shot systemd service with network that pre-populates the volume |
| Q3 | **Rebuild socket auth:** should the helper verify the caller is the container user? | Nice-to-have; Unix socket ownership + `--security-opt=no-new-privileges` is sufficient for the threat model |
| Q4 | **Interactive terminal inside container:** `prompt_toolkit` needs a PTY | `podman run -it` works; verify `oci-containers` systemd unit supports TTY — may need a wrapper script that re-attaches to the running container |
