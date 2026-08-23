# Security model

`nixadmin` separates model reasoning from system authority. This document states
the guarantees the current implementation provides and the trust it still assumes.

## Enforced boundaries

- Models receive no shell and cannot submit arbitrary command strings.
- Privileged requests use a separate Unix socket and a fixed action set: `test`,
  `switch`, `boot`, `revert`, and a validated systemd unit restart.
- Configuration edits happen in isolated git worktrees and must pass evaluation
  before the user sees and confirms the diff.
- A switch requires a successful test in the same session. Real helper exit status,
  not model output or string matching, determines success.
- Failed activation is distinguished from a failed build; partial activation
  triggers system rollback as well as file rollback.
- Runtime remediation is checked against live state before success is reported.
- Remote escalation is never implicit. The reviewed query is redacted before it
  leaves the machine; locally fetched tool results are deterministically scrubbed.
- Network frames, HTTP work, subprocesses, model context, and shutdown waits are
  bounded to limit resource exhaustion.

## Trust assumptions and residual risk

- Capability modules are trusted Python code loaded into the daemon process. They
  can execute as the daemon user and are not suitable for an unreviewed plugin
  ecosystem. See [ADR 0001](docs/adr/0001-module-trust-model.md).
- The daemon currently runs as the login user, who belongs to the `nixadmin`
  helper group. Any process already executing as that user can contact the helper
  directly and bypass the daemon's confirmation policy. The helper still restricts
  callers to its fixed actions and configured flake directory.
- Local redaction reduces known secret and identifier shapes; it is not a formal
  guarantee that arbitrary text contains no identifying information.
- The web interface is loopback-only and protected by Host, Origin, and random-token
  checks. A process with access to the user's runtime directory remains inside the
  local-user trust boundary.

The project does not claim isolation from malicious code already running as the
login user. Moving the daemon and module execution to a restricted account is the
primary outstanding hardening step.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Include
the affected boundary, a minimal reproduction, and whether the issue permits
arbitrary user execution, helper access, or unintended data disclosure. Avoid
opening a public issue for an unpatched privilege or disclosure vulnerability.
