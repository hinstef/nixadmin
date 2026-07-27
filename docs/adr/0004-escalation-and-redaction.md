# ADR 0004 — Confirmed, redacted escalation to the frontier

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Steffen
- **Related:** [`vision.md`](../vision.md) (local-first, never-silent routing),
  [`ux.md`](../ux.md) ("No home of its own vs invoke surface"), `routing.py`,
  the invoke bar (`web/page.py`), `nix-nixadmin-b4h` (remote enablement)

## Context

A person needs to *tell the agent what they want* from the web hub ("install
spotify"). The daemon already has the full `Query`/confirm pipeline, but the web
had no way to reach it. Exposing it raises the real question: **when the on-device
model can't do the job, what happens?**

The project's core principle: the local agent should **always be able to escalate
to the frontier model — but never silently.** And escalation must not quietly ship
the person's private data off the box. So two things have to be true at the
local→remote boundary: the person **consents**, and what leaves is **redacted**.

The prior code only escalated on *mutation detection* (`detect_mutation`), and sent
the raw query. That's neither a real competence judgment nor a privacy boundary.

## Decision

**The local model is both the uncertainty judge and the privacy filter.**

1. **Self-judged escalation (`local.assess_escalation`).** For a query that would
   otherwise run locally, the local model judges its own competence ("can I handle
   this on-device, or does it need the cloud assistant?"). It biases to **LOCAL**
   on anything ambiguous or on any probe failure — an unreachable/slow model can
   never *push* work off the device by accident. Open-ended *changes* always offer
   (the deterministic install/remove action tier still runs locally with no
   escalation — it's confidently local).

2. **Never silent — offer + confirm.** When escalation is warranted the daemon
   emits a `Confirm`. It is declinable: on "no" it falls back to a best-effort
   local answer (a read) or a plain acknowledgement (a change it can't make).

3. **Redact before it leaves, and show it (`redact.py`).** Two passes:
   deterministic `scrub` (API-key/token shapes, emails, IPs, home paths → typed
   placeholders) **then** a local-model rewrite for contextual PII. The result is
   shown **verbatim in the confirm** — the person sees exactly what would be sent.
   The model pass only ever sees already-scrubbed text, so a model failure degrades
   to the deterministic result, never to the raw input. What actually goes to the
   frontier (and what we record) is the redacted payload, not `query.text`.

4. **Honest when the frontier isn't wired.** Remote execution needs credentials
   (`b4h`); until then, accepting the offer yields a plain "the fuller assistant
   isn't set up on this machine yet." The whole offer→confirm→redact UX is built
   and testable now; enabling remote flips the send step with **no UI change**.

## Consequences

- The invoke bar can talk to the agent for real: local answers and local
  install/remove today; a safe, consented path to the frontier for the rest.
- The redaction confirm makes "this will leave your device" **legible**, not just
  asserted — the privacy brand as a concrete mechanic (`ux.md`).
- Cost: `assess_escalation` adds one cheap local generate per read query. Accepted
  for the "never silent" guarantee; can be folded into classify later if it bites.
- **Scope of v1 redaction:** the user's *query text* only — the part most likely to
  hold a secret. Grounding context, tool outputs, and history on escalated queries
  are **not yet** redacted (tracked follow-up). The remote agent's tools run
  locally and can pull system data into the frontier conversation — closing that
  is the next privacy increment.
- **Known tradeoff — fidelity vs privacy on *changes*.** Redaction can strip
  values that are part of an actionable request (an IP, a path), so an escalated
  *change* reaches the frontier as a slightly lossier request. We accept this: the
  privacy line is non-negotiable, and the remote agent has no file-editing tool
  (only `nixadmin_rebuild`), so it advises rather than acting on the literal text.
  A param-preserving redaction (or a "send exact wording" opt-in for changes) is a
  tracked follow-up, to revisit when the frontier is actually wired (`b4h`).
- **Escalation is gated on a reachable frontier.** When remote isn't configured we
  neither run the self-judge nor prompt — a moot offer to send data nowhere would
  only add latency and a confusing consent step. The honest limitation is immediate.
- No new trust in the frontier: routing still refuses to send silently, and the
  deterministic core still owns every privileged write.
