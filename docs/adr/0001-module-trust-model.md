# ADR 0001 — Module trust model

- **Status:** Accepted (code-based plugins for now; revisit before third-party distribution)
- **Date:** 2026-06-13
- **Deciders:** Steffen
- **Related:** [`nixadmin-v3-spec.md`](../nixadmin-v3-spec.md) §"Modules are code, not config"

## Context

nixadmin modules are Python objects discovered via the `nixadmin.modules` entry
point. Installing a module therefore means **executing the author's code** inside
the daemon. This is powerful and Pythonic — `trigger`/`filter` callables, context
providers as async functions, arbitrary `cmd` strings — but it is a hard trust
boundary, and the daemon is an unusually dangerous host for untrusted code:

- **Arbitrary code at import.** `ep.load()` runs module code at daemon startup,
  every boot, before any query.
- **Arbitrary shell as the user.** Every fetcher/monitor `cmd` runs as the daemon
  user (e.g. `steve`) — can read SSH keys, browser data, and exfiltrate over the
  network (httpx is in-process). Monitors provide a persistent timer to do so.
- **A line of sight to root.** The daemon user is in the `nixadmin` group, so
  module code can open `/run/nixadmin-helper.sock` directly and speak the helper
  protocol — bypassing the in-process safety gate and triggering a privileged
  `nixos-rebuild`. The gate lives in the same process as the module, so a
  malicious module is *inside* the trust boundary it would need to cross.

Net: this is "as risky as `pip install`" **plus** a long-running, boot-time
process wired to root. That is acceptable when the only module author is the
operator. It is **not** acceptable for the stated long-term goal — an ecosystem of
publishable modules installed by non-technical users who cannot audit code.

## Decision

**Keep the code-based, entry-point module model as-is for now.**

The current reality is single-author: the operator writes and installs only their
own modules (and the reference `nixadmin-extras` package). Under self-trust, the
arbitrary-code property is a feature (flexibility) and the risk is nil. We
explicitly accept it rather than pay for hardening we don't yet need.

We **do not** open nixadmin to third-party / multi-author module distribution
until the hardening below is in place.

### Revisit trigger

Re-open this ADR when **any** of these becomes true:

- modules are sourced from outside the operator's own control (a public registry,
  PyPI installs by end users, "install this module" UX for non-technical users);
- nixadmin is packaged for general distribution (nixpkgs, a product);
- a module would run on a machine whose owner did not author it (e.g. the wife's
  laptop running a community module).

## Future direction (the hardening, when triggered)

Roughly in order of impact:

1. **Modules become data, not code.** Manifests as JSON/TOML — `name`,
   `description`, command list, monitor specs with *declarative* triggers
   (`"capacity < 20"` not a lambda). Eliminates import-time execution and
   arbitrary callables. Residual risk (command strings) becomes reviewable data.
2. **Sandboxed command execution.** Run `cmd`s under bubblewrap / `systemd-run`
   with `NoNewPrivileges`, read-only FS, and **no network by default**. NixOS
   makes this natural. Network/extra access must be *declared* and approved.
3. **Privilege isolation.** Module-supplied commands must not run in a context
   that can reach `/run/nixadmin-helper.sock`. Only the daemon core mediates
   privileged actions. (Severs the worst escalation path — see "cheap win".)
4. **Capabilities + provenance.** Modules declare what they touch
   (`commands: [df, lsblk]`, `network: none`, `dbus: systemd`); the daemon
   enforces an allowlist. Plus signing, a curated registry, and sourcing via
   nixpkgs (review + pinned hashes) rather than raw PyPI.

The tension to weigh then: code-based is flexible; declarative is safer but less
expressive (no arbitrary logic in a trigger). For the non-technical-user goal,
declarative + sandboxed + privilege-isolated is the right destination.

### Cheap win available now (independent of the redesign)

**Privilege isolation (#3)** is small and worth doing even under the current
model: ensure module-run commands cannot reach the helper socket, so even buggy
(not yet malicious) module code can't accidentally trigger a rebuild. Tracked
separately; does not require the full data-model shift.

## Consequences

- **Positive:** maximum flexibility today; no premature security machinery; the
  reference module pattern stays trivially simple to write.
- **Negative:** the module system is unsafe for untrusted authors. This is a
  blocking prerequisite for the ecosystem vision and must not be forgotten — hence
  this ADR and its explicit revisit trigger.
- The spec's "Modules are code, not config" section now points here for the full
  reasoning and the conditions under which the decision flips.

## Current status — 2026-08-23

The trusted-code decision still applies. The daemon remains a login-user service,
so privilege isolation is not yet complete; this is the highest-priority security
item rather than an implied property of the module SDK.
