# nixadmin — Product Direction

> **Historical — not the current direction.** Superseded by
> [`../vision.md`](../vision.md) and [`../ux.md`](../ux.md).
>
> The through-line survived: an assistant for someone who does not know what a
> service or a journal log is. The surfaces did not — the phased plan below ends
> at a KDE plasmoid, written before the desktop moved to COSMIC and before the
> current GTK and voice direction.

## Vision

An AI sysadmin accessible to non-technical users (DAU). Not a CLI tool — an assistant that manages the computer on your behalf, surfaces problems before the user notices them, and proposes fixes in plain language.

Target user: someone who uses LLMs but doesn't know what a service or a journal log is.

---

## Interaction Model (MVP)

- **Propose + confirm** — never act autonomously, always show details and wait for user approval
- **Verbose debug output** — during dogfooding phase, show full context (journal tail, error output, proposed command)
- Trust model: TBD — needs scoping (see Trust Model section below)

---

## UI Surface

### Phase 1 — Notification-only (MVP)
- `libnotify` desktop notification with action button
- Clicking opens a nixadmin terminal session pre-loaded with event context
- Zero custom UI code

### Phase 2 — System tray app
- Python + PyQt6/PySide6 (Qt-native, integrates cleanly with KDE)
- `QSystemTrayIcon` + chat window
- Event history, status indicator

### Phase 3 — KDE Plasmoid
- QML widget, fully integrated into KDE panel
- Overkill until Phase 2 is validated

---

## Architecture

```
Event monitor (systemd user service)
  │  watches: journald, disk, NetworkManager D-Bus, systemd D-Bus
  │  detects events, builds context (journal tail, stats)
  ▼
libnotify notification
  │  "bluetooth.service crashed — click to diagnose"
  ▼
pi session with pre-loaded context
  │  system prompt includes event details
  │  proposes fix in plain language, waits for confirmation
  ▼
nixadmin helper socket (existing)
  │  executes approved action
```

---

## Events to Monitor

### MVP (dogfooding)
| Event | Signal | Notes |
|---|---|---|
| systemd service crash | journald / D-Bus `org.freedesktop.systemd1` | Most actionable, clear signal |
| Disk usage > 85% | poll `df` or inotify on `/proc/mounts` | Silent killer, users never notice |
| Wifi drops / fails to connect | NetworkManager D-Bus | Most emotionally frustrating for DAU |
| System updates overdue (> 2 weeks) | check flake.lock mtime | Proactive value |

### Later
- Flatpak updates available
- Bluetooth device failed to connect
- High memory / swap usage (system feels slow)
- Failed login attempts

### Skip for now
- CPU temperature (too noisy)
- Network latency (hard to attribute to local machine)

---

## Trust Model

Needs proper scoping. Key dimensions to decide:

### Action categories (draft)
| Category | Examples | Proposed trust level |
|---|---|---|
| Read-only diagnostics | journal tail, disk usage, service status | Always allowed, no confirmation |
| Safe reversible actions | restart a service, clear a cache | Propose + one-click confirm |
| Config changes | edit NixOS modules, rebuild | Propose + show diff + explicit confirm |
| Destructive / irreversible | delete files, remove packages | Propose + require typed confirmation? |
| Never allowed | touching LUKS/TPM/PAM, boot config | Hard blocked regardless of user input |

### Core principle: capability proxy

The rebuild socket pattern generalises. The model never talks to the OS directly — every action goes through a proxy layer:

```
model generates intent
      │
      ▼
harness (pi) translates to tool call
      │
      ▼
capability proxy  ← policy lives here
  │  is this action in the allowed set?
  │  are the arguments safe?
  │  executes as restricted Unix user / via specific socket
      │
      ▼
kernel enforces the rest (Unix permissions)
```

Security story: "Unix permissions", not "we trust the AI". The model can generate `rm -rf /` — the kernel says no.

### Proxy action tiers (draft)
| Tier | Examples | Mechanism |
|---|---|---|
| Read-only | journalctl, df, systemctl status | Run directly as nixadmin user |
| Controlled writes | systemctl restart \<service\> | Socket, whitelist of allowed services |
| Config changes | nixos-rebuild | Existing helper socket |
| Never proxied | rm, dd, passwd, anything touching LUKS/TPM | Not in proxy, structurally impossible |

### Open questions on trust
- Per-event trust levels? (wifi fix = low risk, disk cleanup = medium risk)
- How does the proxy handle requests outside the allowed set — reject silently or surface to user?

---

## Open Questions
- How does pi receive event context? Pre-populated system prompt? Named pipe?
- Should the event monitor be part of nix-nixadmin or nixlap?
- Notification action: open terminal or embedded chat UI?
