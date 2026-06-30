# UX — design for silence

> Companion to [`vision.md`](vision.md). The vision says *why* (make compute
> human; the AI is an implementation detail). This says *how the product behaves*
> for the person — the interaction model, to be settled before any toolkit or
> pixel.

## Principle: design for silence

The UX corollary of "the AI is an implementation detail": foreground the
**problem** (a machine kept well), never the assistant. A chat window foregrounds
the AI; silence foregrounds the problem — by making it disappear.

It is **structurally anti-engagement** — the opposite of every product that
profits from attention. That is the moat: the giants' business *needs* the panel;
ours needs its absence. "Humane to the person, not the platform," made literal.

> **Test:** does this surface the assistant, or make the problem disappear? If the
> person becomes aware they are "using an AI," we have failed.

Consequences:
- No AI branding, avatar, or "thinking…". The assistant is anonymous
  infrastructure, like the kernel.
- A notification is **spent capital**, not a feature. Silence is the default;
  interrupting withdraws from a trust budget.
- The proactive detectors (see [`proactive-detectors-plan.md`](proactive-detectors-plan.md))
  are the **core**, not a nice-to-have — fixing things before the person notices
  is the only way to be silent.
- When it must speak, it speaks about the person's world, not itself:
  "Your dock is back," never "I detected and resolved an issue."

## Act, don't ask — reversibility licenses silence

The sharpest form of silence: when the dock crashes and we know a safe fix,
**just fix it** — don't interrupt to ask permission to do the obvious. "Want me to
restart it?" still forces machine-thinking on the person.

What licenses acting without asking is **reversibility**. On a substrate where any
change rolls back (Nix generations — the same property the vision leans on for
*safe*), the downside of acting is bounded, so the assistant can act far more
often than on an imperative OS. **Safe → silent.** The substrate that makes
machine-made changes safe is also what lets the machine stay quiet.

| Situation | Behaviour |
|---|---|
| Reversible · low-consequence · confident | **Act silently**, record in the ledger (restart a crashed UI component; reclaim cache) |
| Irreversible · high-consequence · uncertain | **Ask** — the one rare push |
| Can't act, but the person should know | **Inform** (pull, unless time-critical) |

For safe actions, **undo-after beats confirm-before**: act, then offer a one-tap
"put it back" — never block. This preserves silence *and* control. Confirm-before
is for the irreversible/consequential tail only.

**Consequence guard:** "low-consequence" includes *current user impact* — never
auto-act in a way that interrupts what the person is doing. A crashed dock is
already broken; restarting only helps. A flaky-but-working service is not.

## Silence accounted for — the "kept-well" ledger

Silence must not curdle into **opacity**: a system you never see act is one you
stop trusting and eventually forget. Borrowed from ADAS, where the goal was also
zero interventions, yet the system *unintrusively surfaced its track record*
("400 km, 0 interventions, 38 lights") to build trust and stay present.

The computer equivalent — **pull-only, never a popup:**

> **Looked after itself for 23 days.**
> Quietly: freed 8 GB last night · restarted your dock twice · 4 security updates.

- **Hero metric:** the intervention-free streak — days since you had to think in
  machine terms. The most honest single number; quietly satisfying (a closing
  ring, not a mascot).
- **Glance-moments, not interruptions:** lock screen / login is ideal — already a
  look, nothing actionable, one calm line at zero attention cost.
- **The data already exists:** the audit trail (journald `action`/remediation
  events) and the detectors produce it. Observability plumbing becomes the trust
  surface.
- **Honest over flattering:** never hide a real problem to protect a streak. The
  number serves trust; trust never serves the number. Fully optional, off-switch
  included — and no streak-anxiety (reassuring, never pressuring).

## Push vs pull — the Clippy line

Clippy and the trust ledger are the same axis:

- **Clippy = bad push:** frequent, unsolicited, presumptuous, reacting to what
  you're doing *now*. It initiates and it suggests.
- **Trust ledger = pull:** factual, ambient, about the system's own record, there
  when you glance, gone when you don't. It never initiates, never asks.

**Rule:** the system gets exactly **one** kind of push — rare, budgeted,
genuinely actionable, and only for the can't-safely-auto-fix tail (most failures
are auto-fixed silently per the section above). Everything else is pull. A trust
surface that grows a button or pops itself up has become Clippy.

## No home of its own (direction, not yet decided)

The most humane surface is the one that isn't there: rather than a new chat or
palette the person must learn, fold intelligence into the surfaces they already
use — the app that won't open grows a "fix it"; the troubled wifi icon offers the
fix on click. Higher integration cost, but it is the moat. Open question against a
single light **invoke** surface (summonable, ephemeral — not resident).

## Two users

A nerd sets this up *for someone they love*, so there is a second user — the
**caretaker** — who gets a richer pull view (a calm digest; the ability to reach
in). The loved one's surface stays dead-simple. A novel product *shape*, not just
a screen.

## Implementation implications (tracked, not yet built)

Two pieces fall directly out of "act, don't ask" — promote to beads issues when we
start. Neither is being built yet.

1. **Autofix engine (the 80%).** A systemd unit failure is *already* subscribed via
   D-Bus (`monitors.py` → builtin `services` → `JobRemoved`/`failed`), but is only
   **emitted as an event broadcast to connected clients** — with no client attached
   it goes nowhere. We notice and do nothing. The work: apply the act/ask decision
   matrix to those events — safe + reversible → auto-remediate (restart) with
   undo-after; the rest → the one rare push. This connects the monitor layer to the
   remediation tier we already have; the plumbing exists, the *policy* doesn't.
2. **The 20% (liveness poll).** Non-unit process exits (the dock) emit no signal
   anywhere — detectable only by polling liveness. Already specced as the
   process-vanished detector in
   [`proactive-detectors-plan.md`](proactive-detectors-plan.md) (#2a). Track,
   don't tackle yet — deliberately decide which process classes earn a poll.

## Deferred (decide after the model)

- **Toolkit:** libcosmic (iced) is native-correct for the locked COSMIC stack
  (ADR 0002); GTK is the fallback. The notification + ledger surfaces are
  toolkit-agnostic (D-Bus / glance points) and shippable first, so this can wait.
- **Voice** as the primary invoke for the non-technical user (text = the nerd's
  tool) — flips the usual "voice later" ordering.
- **Execution tooling** (e.g. `claude.ai/design`) for rendering layouts once the
  interaction model is fixed — a second-half tool, not a model-deciding one.
