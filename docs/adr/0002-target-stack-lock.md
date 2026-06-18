# ADR 0002 — Lock the target stack: NixOS + systemd + COSMIC

- **Status:** Accepted
- **Date:** 2026-06-17
- **Related:** [`vision.md`](../vision.md), [ADR 0001](0001-module-trust-model.md)

## Context

The product is "a computer you can give to someone you love" — which requires it
to actually *fix real problems* on the person's machine, not give generic advice.
Generic, cross-distro, cross-desktop coverage produces shallow answers. Depth —
the thing that makes it trustworthy — requires a known stack.

This was proven concretely: when the COSMIC bar + dock vanished, the diagnosis
required knowing (a) COSMIC's topology — the panel renders both bar and dock and
spawns applets, (b) systemd's scope model, and (c) journald. The decisive signal
was that `cosmic-panel` was **absent**, not **failed** — `systemctl --failed` was
empty. Only "here is what a healthy COSMIC session looks like" surfaces that, and
that knowledge only exists once a desktop is locked.

## Decision

Lock the v1 target stack:

- **NixOS** — the substrate. Provides safe writes (worktree eval, atomic switch,
  rollback) and config-as-state. (Load-bearing; see vision.)
- **systemd + journald** — the canonical **state and error sources**. Units
  (system + user), scopes, `coredumpctl`, the journal. All health/diagnosis reads
  from here.
- **COSMIC** — the first-class **desktop domain**. We encode its topology and
  failure modes (panel/dock/applets/session/greeter, EGL-after-rebuild skew, the
  relogin fix).

Everything may assume this stack. We do **not** abstract for other distros,
inits, or desktops in v1.

## First-class, not only-possible

COSMIC is pre-1.0 and churns. Lock it as the first-class target but keep the
module boundary clean so KDE/GNOME *could* be added later. Bet on one desktop and
go deep; do not spread across three.

## Consequences

- **State/error model becomes concrete:** a defined telemetry layer over systemd +
  journald + coredumpctl, including the **"expected-running vs actual"** health
  model (catch absent processes, not just failed units).
- **COSMIC becomes encoded expertise:** a module that knows the session topology
  and common breakages (the panel/EGL case is the seed).
- **Diagnosis + fix actions get concrete:** restart panel, prompt relogin after a
  graphics-stack rebuild, roll back a generation.
- **Cost:** tied to COSMIC's churn; revisit if COSMIC's internals shift or a second
  desktop becomes worth the spread.

## Revisit trigger

- A second desktop environment becomes a real requirement (then formalise the
  desktop-domain interface that COSMIC currently fills informally).
- COSMIC ships 1.0 / changes its session model materially.
