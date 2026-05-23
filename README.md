# nixadmin

A reusable NixOS module that runs a locally-hosted LLM as a system administrator.

- Ollama with Vulkan GPU acceleration (Mesa RADV — works on any AMD iGPU)
- Rootless Podman container, starts after the graphical session
- Privileged helper daemon for `nixos-rebuild` via a scoped Unix socket — no sudo required

## Usage

```nix
# flake.nix
inputs.nixadmin.url = "github:hinstef/nixadmin";
inputs.nixadmin.inputs.nixpkgs.follows = "nixpkgs";

# In your nixosSystem modules list:
nixadmin.nixosModules.default
```

```nix
# hosts/<hostname>/default.nix
services.nixadmin = {
  enable   = true;
  user     = "alice";
  flakeDir = "/home/alice/nixos-config";
  hostname = "myhost";
  # model defaults to "qwen2.5-coder:7b"
};
```

After first switch, pull the model:

```bash
podman exec nixadmin-ollama ollama pull qwen2.5-coder:7b
```

## How the rebuild helper works

A small Python daemon (`nixadmin-helper`) runs as root and listens on
`/run/nixadmin-helper.sock`. The socket is owned `root:nixadmin` mode `0660` —
only users in the `nixadmin` group (including `cfg.user`) can reach it.

Send JSON, receive streaming output:

```
→ {"action": "test"}
← {"stream": "building the system configuration...\n"}
← {"stream": "..."}
← {"exit": 0}
```

Allowed actions: `test`, `switch`, `revert`.

## GPU requirements

The Vulkan ICD path is currently hardcoded for AMD (Mesa RADV):
`/run/opengl-driver/share/vulkan/icd.d/radeon_icd.x86_64.json`

This works on NixOS with `hardware.graphics` enabled (the default on KDE/X11/Wayland setups).
NVIDIA or Intel users will need to fork the module and adjust `VK_ICD_FILENAMES`.
