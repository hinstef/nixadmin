# Current status

`nixadmin` is deployed as a daily-use developer preview on the author's NixOS
laptop. This page records current reality; git and Beads retain the detailed
history and backlog.

## Shipped

- Local, grounded system questions and optional consented remote escalation
- Deterministic application install/remove with evaluation, confirmation, switch,
  verification, and rollback
- Failed-unit detection, verified remediation, and restart-loop refusal
- Persistent event timeline, kept-well ledger, and operational health endpoint
- CLI, tray, web hub, application launcher, and COSMIC overlay
- Typed module SDK with a separately packaged reference extension
- Reproducible Nix quality gate: tests, strict typing, lint, package and module eval

## Deliberate limits

- NixOS, systemd, and COSMIC are the only first-class target stack.
- Modules are trusted Python code, not sandboxed third-party plugins.
- The daemon currently runs as the login user; membership of the helper group is a
  residual privilege path described in [`SECURITY.md`](../SECURITY.md).
- The project is operated on one real machine, not claimed as production support
  software for arbitrary installations.

## Near-term priorities

- Separate the daemon from the login user and helper group.
- Add proactive detectors only where they lead to a bounded, useful response.
- Continue simplifying the presentation layer and invoke experience.
- Decide the long-term COSMIC-native UI toolkit after the interaction model settles.

The live task list is Beads: run `bd ready` in the repository.
