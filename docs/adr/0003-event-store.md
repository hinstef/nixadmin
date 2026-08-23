# ADR 0003 — Persistent event store (observability substrate)

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Steffen
- **Related:** [`vision.md`](../vision.md) ("explain · notice & offer"), the
  autofix epic (`bd show nix-nixadmin-e7q`), the web hub (`src/nixadmin/web`)

## Context

Until now nothing the daemon observed or did was written down. `history.py` is
`NullHistory`; monitor events were broadcast to live clients and then gone;
explanations and restart outcomes were computed on demand and discarded. Two
concrete costs:

- **UX.** Clicking "Explain" produced a desktop notification that vanished, and
  even the web view lost the explanation on its next 15 s refresh (it rebuilt the
  unit list from scratch). There was no place a result could *stay*. Recorded in
  `observations.md`.
- **No memory to build on.** Autofix (act on a unit-failure event) needs to know
  what already failed, what was already tried, and whether it worked — i.e. it
  needs a history to reason over and to append its own actions to. Without a
  store, every layer is amnesiac.

## Decision

Add a **daemon-owned, append-only event store** — a single timeline of system
events (`nixadmin/store.py`), backed by **SQLite via the stdlib `sqlite3`**.

- **Daemon is the single writer.** Monitors, explanations, restarts and failure
  transitions all already flow through the daemon; it appends there. Clients
  (web, tray, cli) **read** the timeline over the existing Unix-socket protocol
  (`get_timeline` → `timeline`), never touching the DB themselves — the web
  process stays a thin, stateless client (consistent with its own docstring).
- **SQLite, not JSONL.** It is stdlib (no new runtime dependency, preserving the
  protocol/client stdlib discipline) and *queryable* — by unit, by kind, by time
  — which is what observability and autofix actually need. A flat log would push
  that filtering into ad-hoc Python.
- **Separate from conversation history.** `history.py` stores *conversation turns
  keyed by session*; this stores *system events on one timeline*. Different
  shape, lifetime and readers, so they stay distinct modules. `history` remains
  `NullHistory` for now; this ADR does not change it.
- **On by default.** `services.nixadmin.events = "sqlite"`, stored at
  `<stateDir>/events.db` (default `~/.local/state/nixadmin`, XDG-derived at
  runtime). `"null"` opts out. Persistence *is* the feature, so it is not
  opt-in.
- **Never breaks the caller.** `append`/`recent` swallow SQLite errors and
  degrade to a no-op — a persistence failure must not take down a query or an
  explanation. Writes run in a worker thread so the event loop never blocks.

Event kinds written today: `failure_observed`, `failure_cleared`, `explanation`,
`restart`, `journal_snapshot`, `monitor_event` (plus a reserved `autofix`). Kinds
are open strings, so a module or the autofix tier can add its own without a
schema migration.

## Consequences

- The web view becomes a real **hub**: a live "Now" section plus a persistent
  "Timeline" that survives refreshes *and* daemon restarts. The tray deep-links
  into it (`?explain=<unit>`) instead of firing a transient notification.
- Autofix has a substrate to read (what failed / what was tried) and to write its
  own actions into — the natural next consumer of `store.py`.
- The DB grows unbounded for now. Retention/pruning is deferred until it matters
  (a `since`-filtered read and a future `DELETE ... WHERE ts <` are enough); a
  laptop's event rate makes this a non-issue in the near term.
- Wire protocol went to **v2** (additive: the two timeline messages). Older
  clients simply never ask for them.

## Current status — 2026-08-23

The original decision remains in force. Subsequent work added schema versioning,
age-based retention and pruning, cursor pagination, the kept-well ledger, and the
autofix consumer anticipated above. The additive wire protocol is now v4. These
changes extend the store; they do not alter the single-writer or failure-isolation
decision.
