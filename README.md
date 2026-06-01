# nixadmin

> *"My WiFi isn't working."*
> *"The printer isn't printing."*
> *"My computer is slow."*
>
> — Your family, 11pm on a Friday, forever

You love open source. You run NixOS. You are the family IT department whether you signed up for it or not.

**nixadmin** is your out-of-office reply. A NixOS module that installs an AI system administrator directly on the machine — so the machine can explain itself, fix itself, and ideally stop bothering you.

---

## What it does

Adds a `nixadmin` command that drops you (or a slightly more patient family member) into a conversation with an AI that:

- **knows the machine** — reads your NixOS config, installed apps, network interfaces, and disk layout on first launch and keeps a profile
- **can apply changes** — runs `nixos-rebuild test` before any switch, commits to git, and can roll back if things go sideways
- **is appropriately paranoid** — never touches `hardware-configuration.nix`, always asks before touching boot/LUKS/PAM, stashes before editing

Because it runs on NixOS, every change is reproducible and reversible. Worst case: `nixadmin-rebuild revert`.

---

## Model tiers

Pick your poison:

| Tier | Who it's for | What runs |
|------|-------------|-----------|
| `cloud` | You trust Claude with your configs | Claude via Anthropic OAuth — no API key, just `/login` |
| `remote` | You have a beefy LAN server or VPS | Any OpenAI-compatible endpoint |
| `local` | Full privacy, no cloud, air-gapped households | Ollama on the machine itself (Vulkan GPU acceleration) |

All three are configured at once. Switch between them by changing one line in your NixOS config.

---

## Quickstart

```nix
# flake.nix
inputs.nixadmin.url = "github:hinstef/nixadmin";
inputs.nixadmin.inputs.nixpkgs.follows = "nixpkgs";

# In your NixOS modules list:
nixadmin.nixosModules.default
```

```nix
# hosts/<hostname>/default.nix
services.nixadmin = {
  enable   = true;
  user     = "alice";
  flakeDir = "/home/alice/nixos-config";
  hostname = "myhost";
  tier     = "cloud";           # "cloud" | "remote" | "local"

  # remote.baseUrl = "http://homeserver:11434/v1";
  # remote.model   = "llama3.3:70b";
  # local.model defaults to "qwen3-tool:latest"
};
```

For the `local` tier, pull the model after the first switch:

```bash
podman exec nixadmin-ollama ollama pull qwen3-tool:latest
```

For the `cloud` tier, run `nixadmin` once and type `/login`.

See [nixlap](https://github.com/hinstef/nixlap) for a full working example config.

> **Status:** Early release. Tested by the author. Not yet wife-tested. Getting there.

---

## Safety guardrails

- `nixos-rebuild test` always runs before `switch` — if it fails, nothing changes
- Every edit is git-stashed first; failed builds auto-restore
- The rebuild helper runs as root but is scoped to a Unix socket owned by the `nixadmin` group — no `sudo`, no `NOPASSWD` rules
- `hardware-configuration.nix` is off-limits
- Boot, LUKS, TPM, and PAM changes require explicit confirmation

---

## How the rebuild helper works

A small Python daemon runs as root and listens on `/run/nixadmin-helper.sock` (mode `0660`, group `nixadmin`). The AI calls `nixadmin-rebuild <action>`, which speaks JSON over that socket:

```
→ {"action": "test"}
← {"stream": "building the system configuration...\n"}
← {"exit": 0}
```

No polkit. No sudoers entries. Just a socket with tight group permissions.

---

## GPU requirements (local tier)

The Vulkan ICD path is hardcoded for AMD (Mesa RADV):
`/run/opengl-driver/share/vulkan/icd.d/radeon_icd.x86_64.json`

Works on any AMD iGPU or dGPU with `hardware.graphics` enabled (the default on most NixOS desktop setups). NVIDIA/Intel users: fork and adjust `VK_ICD_FILENAMES`.
