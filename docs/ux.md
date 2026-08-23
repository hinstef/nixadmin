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

## North star: context-aware, in-the-moment help

Design-for-silence in its vivid form: the assistant notices a failure the person
*just personally hit* and quietly offers to fix it, right then — then goes silent
again. Canonical example — **printing** (the universally-hated "it never works"):
they try to print, the CUPS job fails, and because they were *just in the print
dialog*, the tray offers "Printing didn't go through — want me to look?".

The mechanic is a **conjunction**, and it's what separates this from Clippy:

- a **system failure** signal (e.g. a failed CUPS job) — alone this might be a
  background job; don't surface.
- an **in-the-moment intent** signal (print dialog open / that app just focused) —
  alone this is just activity; nothing's wrong.
- **failure ∧ intent** = the one legitimate moment to speak.

> Clippy *had* context and used it to push unwanted help. The rule here:
> **surface only on a failure the person just personally hit — never on a state
> merely noticed.** Same knowledge, opposite timing.

Context feeds the act/ask matrix as the *current-user-impact / confidence* input:
someone actively trying to print clearly cares now — so auto-fix the safe/reversible
cases silently, offer the rest, and if it's truly stuck, that offer becomes the
escalation-to-the-specialist consent moment (which is also the loved one's natural
"this seems off" ask, and the point where local → frontier is legitimate).

**Hard privacy line — the whole brand in one mechanic.** Context-awareness is the
same capability as Big Tech surveillance (knowing what you're doing), so it must be
inverted: **local-only, ephemeral, never logged, never sent.** Read to help you in
the moment, then forgotten. They watch to monetize; this watches to help and forget.

Not a PoC target. The PoC is the 80% (systemd unit failures) with a hands-on
"fix it" button — *supervised mode*, where you approve each fix and watch what the
autonomous version would do before trusting it to act silently. This is where it's
going.

## Silence accounted for — the "kept-well" ledger

**BUILT (2026-07-26, `9ep`)** — a first cut. `nixadmin.ledger` folds the event
store into a streak + quiet tally (pure, tested); the daemon serves it over the
`get_ledger` wire message and the web hub renders one calm line at the top (pull
only, no button). Honest by construction: the *live* failed-unit count is passed
in, so anything broken right now reads "needs a hand," never a flattering streak.
The lock-screen / login glance-moment is still the ideal surface (toolkit work,
deferred); the web line is the shippable version of the same pull-only idea.

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

## No home of its own (direction, partially settled)

The most humane surface is the one that isn't there: rather than a new chat or
palette the person must learn, fold intelligence into the surfaces they already
use — the app that won't open grows a "fix it"; the troubled wifi icon offers the
fix on click. Higher integration cost, but it is the moat. Open question against a
single light **invoke** surface (summonable, ephemeral — not resident).

> **Update (2026-07-26):** for the web hub we chose the **invoke surface** — a
> single "What would you like?" bar whose results are ephemeral **task cards**, not
> a resident chat transcript (a chat panel was explicitly rejected per "design for
> silence"). It's on the web page for now, for rapid iteration; folding into native
> surfaces stays the longer-term moat. Tracked in `nix-nixadmin-edx`.

> **Update (2026-08-13) — the invoke surface absorbs the app store.** Non-technical
> users asked for **one place for admin tasks, installing included**: given a
> natural-language way in, they did not want to go to a "store" at all. That is the
> invoke surface earning its keep — *installing is a sentence, not a place* — and it
> removes a whole destination rather than adding one, which is the same move as
> design-for-silence applied to navigation. The web hub is now laid out as that
> single pane: prompt first and focused, **common actions** as chips under it,
> replies below, machine status under those.
>
> Two rules held while building it:
>
> - **A chip only types for you.** Common actions are seeded prompts that take the
>   same route to the daemon as anything typed, so a button can never reach an
>   action you could not have asked for, and never skips a confirm. There is one
>   path in, and it is the one that is gated.
> - **Cards stack, but stay capped and dismissable.** Enough of a working record
>   that a slow install stays visible while you ask something else — deliberately
>   short of a transcript, which would foreground the assistant again.
>
> Still a prototype of the surface, not its shape. The intended form is a
> **summonable spotlight-style overlay** (plausibly this page, wrapped): summoned,
> used, gone — which is what "invoke, not resident" actually looks like, and what
> the web page can only imitate. Tracked in `nix-nixadmin-5rf`.

## Two users

A nerd sets this up *for someone they love*, so there is a second user — the
**caretaker** — who gets a richer pull view (a calm digest; the ability to reach
in). The loved one's surface stays dead-simple. A novel product *shape*, not just
a screen.

## Implementation implications (tracked, not yet built)

Two pieces fall directly out of "act, don't ask" — promote to beads issues when we
start. Neither is being built yet.

1. **Autofix engine (the 80%). — BUILT (2026-07-26).** The act/ask matrix is now
   applied to unit-failure events: safe/reversible → auto-restart + verify + record
   (`autofix.py` policy + the daemon engine, `src/nixadmin/server.py`); a restart
   that keeps not sticking → inform (the one push). Restart-loop guard reads the
   event-store history. Both scopes by default; system autonomy is configurable.
   Delivery is `Event` + the Timeline for now — desktop notifications and
   model-phrased "want me to fix it?" offers remain 2b (see below / `b43`).
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
