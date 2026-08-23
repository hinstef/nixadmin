# Portfolio Improvement Plan

## Goal

Make `nixadmin` easy for a recruiter or engineering leader to understand and evaluate in five to ten minutes.

The repository should demonstrate the combination relevant to a Software Engineering Manager application:

- hands-on distributed-systems and software-architecture ability;
- deliberate product judgment and scope control;
- security, reliability, and operational thinking;
- disciplined engineering practices and technical communication;
- the ability to turn an ambiguous idea into a working, coherent system.

The objective is not to make the project look larger or more mature than it is. The objective is to make the existing work legible, verifiable, and easy to discuss in an interview.

## Current Strengths

The project already has unusually strong substance for a personal AI project:

- a deployed, local-first NixOS systems agent rather than a prompt demonstration;
- a daemon with terminal, web, and tray clients over a defined Unix-socket protocol;
- local and optional remote model routing;
- deterministic safety gates around privileged actions;
- confirmation, redaction, validation, rollback, and post-action verification;
- proactive monitoring and automated remediation;
- an append-only SQLite event store and user-facing operational history;
- a plugin SDK with entry-point discovery and a separate reference module package;
- NixOS packaging and reproducible pytest, mypy, ruff, and module-evaluation checks;
- extensive design documentation and explicit architectural decisions;
- real deployment through the companion `nixlap` repository.

The main gap is presentation. A reviewer currently has to read several long documents and reconcile some stale status information before understanding what works.

## Phase 1 — Make the Repository Immediately Legible

### 1. Rewrite the top of the README

The first screen should answer four questions:

1. What is it?
2. What can it do today?
3. What makes its architecture interesting?
4. How can I see or run it?

Lead with the working system, not the development branch or long-term vision. Keep the vision, but move the deeper product narrative below the concrete overview.

Suggested opening direction:

> `nixadmin` is a local-first systems agent for NixOS. It monitors machine state, explains problems in plain language, and safely performs selected remediation through deterministic, auditable control paths. Models interpret intent; ordinary code owns actions, privilege, confirmation, and verification.

Immediately follow this with a compact “Working today” list:

- inspect and explain live system state using a local model;
- install or remove applications through validated Nix changes;
- detect and remediate failed services;
- record observations and actions in a persistent timeline;
- escalate difficult questions to a remote model only with explicit consent and redaction;
- expose the same daemon through terminal, tray, and web clients.

### 2. Correct stale status information

- Remove or update the claim that active development happens on `feat/v3-daemon` if `main` is now authoritative.
- Reconcile the documented test count with the current suite.
- Clearly distinguish shipped, experimental, planned, and deliberately unsupported capabilities.
- Ensure `README.md`, `docs/PROGRESS.md`, and the package version do not contradict one another.

### 3. Add one architecture diagram

Create a compact diagram showing:

```text
CLI / tray / web
        │
   Unix-socket protocol
        │
      daemon
  ┌─────┼──────────────┐
local model   remote model   deterministic actions
  │          consent +       validation → confirmation
grounding     redaction       → privileged helper → verify
        │
 monitors + event store + plugin modules
```

The final version should make the trust boundaries explicit, especially the separation between model reasoning, deterministic actions, and the root helper.

### 4. Add visual proof

Include one short screen recording or a small set of screenshots showing a complete scenario:

1. a service fails;
2. `nixadmin` detects and explains it;
3. remediation is proposed or performed according to policy;
4. the result is verified and recorded in the timeline.

Prefer one coherent scenario over a gallery of disconnected UI screens. Remove machine names, usernames, addresses, journal contents, tokens, and other identifying information before publishing.

### 5. Add a concise “Why this architecture?” section

Summarize the key judgment in a few sentences:

- models are good at interpreting intent and evidence;
- models are not trusted to invent commands or control privilege;
- NixOS provides validation, atomic activation, and rollback;
- explicit protocol and privilege boundaries make behavior testable independently of prompts or model choice.

Link to the ADRs for readers who want the detailed reasoning.

## Phase 2 — Strengthen Verifiable Engineering Signals

### 6. Add GitHub Actions CI

Run the same gates already defined by `nix flake check`:

- pytest;
- mypy in strict mode;
- ruff;
- NixOS module evaluation or the broadest check practical on hosted runners.

Add the CI badge to the README only after the workflow is reliably green. Avoid a badge collection; one build badge and an optional license badge are enough.

### 7. Publish an initial release

Create a `v0.1.0` release when `main` represents a coherent, reproducible snapshot.

The release notes should state:

- what is implemented;
- the supported platform and assumptions;
- the safety model and its limits;
- installation or demonstration steps;
- known gaps.

Do not imply general end-user readiness. “Working personal-system deployment” or “developer preview” is more credible than “production ready.”

### 8. Improve the quick-start path

Provide the shortest reproducible route for a technical reviewer:

- run the test and static-analysis suite;
- start the daemon with safe or mocked dependencies;
- invoke one read-only query;
- optionally run a contained demonstration of an action.

If installation depends heavily on the companion laptop configuration, add a demo or development mode that does not require access to private flake inputs.

### 9. Add focused test reporting

The raw test count is supporting evidence, not the story. Document which risks the suite covers:

- wire-protocol compatibility;
- routing behavior;
- confirmation and safety gates;
- redaction;
- action rollback on validation or rebuild failure;
- remediation verification and restart-loop prevention;
- persistence and daemon/client behavior.

Add coverage measurement only if it leads to useful testing decisions. Do not optimize for an arbitrary percentage.

### 10. Document one end-to-end failure case

Write a short engineering case study based on a real defect or operational discovery. A strong example would include:

- the observed symptom;
- the misleading initial signal;
- the actual root cause;
- the architectural or test change made in response;
- how the new behavior was verified.

The existing cold-start false all-clear and helper-restart-during-activation problems are good candidates. They demonstrate operational judgment more effectively than a feature list.

## Phase 3 — Make the Project Useful in Applications

### 11. Add a portfolio summary to the repository

Include a small “Engineering highlights” section with facts a reviewer can verify in the code:

- Python daemon with multiple protocol clients;
- local/remote model routing with explicit escalation;
- deterministic privileged-action boundary;
- plugin architecture;
- persistent event-driven remediation loop;
- strict typing, linting, tests, and Nix-based reproducibility;
- deployed integration with `nixlap`.

Avoid résumé claims such as “enterprise-grade,” “production-scale,” or “revolutionary.”

### 12. Connect `nixlap` as deployment evidence

Keep `nixadmin` as the featured project and use `nixlap` as corroboration:

- link to `nixlap` from a “Real deployment” section;
- explain that it supplies the declarative machine configuration managed by the agent;
- describe the public/private configuration split and secrets boundary briefly;
- link back to `nixadmin` from `nixlap`.

Do not give `nixlap` equal billing in the CV. Its value is proving integration and dogfooding.

### 13. Prepare an interview story map

Keep a private document mapping repository evidence to likely interview themes:

| Interview theme | Project evidence |
|---|---|
| Architecture | Daemon/client protocol, module system, event store, routing tiers |
| Security | Privilege separation, confirmation gates, redaction, trust-model ADR |
| Reliability | Verification after remediation, restart-loop guard, rollback behavior |
| Product judgment | Local-first vision, design-for-silence, deliberately constrained action set |
| Constructive dissent | Choosing deterministic control boundaries instead of unrestricted agent access |
| Execution | Greenfield design through deployment on a real NixOS system |
| Technical leadership | ADRs, staged roadmap, explicit tradeoffs and revisit triggers |

For each story, be ready to explain alternatives considered, tradeoffs, mistakes, and what remains incomplete.

## Recommended CV and LinkedIn Positioning

### CV

Use one compact line:

> Built `nixadmin`, a local-first NixOS systems agent with model routing, deterministic safety gates, automated remediation, and a plugin SDK.

The GitHub profile in the CV header provides the discovery path. A direct repository link can be added later if applications show that reviewers are not finding it.

### LinkedIn project entry

> Built a local-first systems agent for NixOS that monitors machine state, explains problems, and safely performs or proposes remediation. Designed deterministic safety and privilege boundaries around local and remote LLMs, with explicit consent, redaction, rollback, and verification. Packaged as a Python daemon with terminal, web, and tray clients, a plugin SDK, persistent event history, and an automated test suite.

Do not position this as professional production AI experience. Position it as current, demonstrable evidence of systems design, technical curiosity, responsible AI integration, and hands-on implementation.

## Non-Goals

Avoid work that adds little hiring signal:

- inflating the feature set before documenting what already works;
- adding fashionable frameworks solely for keyword coverage;
- replacing the deterministic core with a more autonomous agent loop;
- chasing test-count or coverage numbers without risk-based justification;
- presenting `datenhafen`, which currently has no meaningful public implementation;
- hiding known limitations or overstating deployment scale;
- spending weeks on branding before the README, demo, CI, and release are credible.

## Definition of Done

The portfolio pass is complete when a reviewer can:

1. understand the project and its differentiator from the first README screen;
2. see a diagram of the architecture and trust boundaries;
3. watch one end-to-end scenario without installing the project;
4. verify green automated checks;
5. reproduce a basic read-only demonstration from documented steps;
6. identify what is working, experimental, and planned;
7. inspect a tagged release;
8. follow the connection to the live `nixlap` deployment;
9. find concise ADRs explaining the most consequential decisions.

## Recommended Order

1. Correct stale README and progress information.
2. Rewrite the README opening and add the architecture diagram.
3. Capture one redacted end-to-end demonstration.
4. Add CI and make it reliably green.
5. Improve the reviewer quick start.
6. Publish `v0.1.0` with honest release notes.
7. Add the case study and private interview story map.

