# Plan — Proactive detectors (#2)

Status: **planned, not built.** Pick this up cold from here.

## Why

The COSMIC bar + dock vanished and **nothing flagged it**: `cosmic-panel` isn't a
systemd unit (it's a child of `cosmic-session`), so `systemctl --failed` was empty
and there was no coredump — it just *exited* on an EGL error. Our existing monitors
(dbus `JobRemoved` for failed units; poll for disk/battery) structurally cannot see
this. Proactive detection must cover the class **"a session process died without a
unit failure."**

## Principles this must honor (don't re-litigate)

- **Derive, don't hardcode** — no hardcoded "expected session = [list]". Use a
  *dynamic baseline* (what's actually running now).
- **journald is the store** — no buffer, no on-disk dump. Detect → fire → query
  journald *live* for context.
- **Model infers, deterministic code detects** — detection is cheap and always-on
  (no tokens); the model only runs on-demand ("what happened?") or in 2b.
- **Reuse `MonitorRunner`** — detectors are background async tasks, same lifecycle
  (`start()` / `aclose()`).
- **Scoped to the locked stack** (ADR 0002): COSMIC + systemd + journald.

## Architectural decision

These detectors are **stateful** (they remember a baseline / last-seen coredump).
The module `Monitor` dataclass is deliberately **stateless** (declarative `cmd` +
`trigger(output)->bool`). So they do **not** belong as module monitors.

→ New file **`src/nixadmin/detectors.py`** with core daemon detectors, started by
`MonitorRunner` alongside the declarative module monitors. Keeps the module API
simple; puts stateful core logic in core.

## Build now (2a): two detectors → emit `Event`s

### 1. process-vanished (dynamic baseline) — THE one (catches the panel)

Algorithm (poll ~30s):
```
each tick:
  current = { comm for proc in `pgrep -fa cosmic-` }      # scoped to COSMIC
  session_up = "cosmic-comp" in current                   # logout ≠ crash
  if not session_up: baseline = current; continue          # don't alert on logout

  # a component is "baselined" once seen for >=2 consecutive ticks (ignore churn)
  for c in current: seen_count[c] += 1
  baseline = { c for c,n in seen_count if n >= 2 }

  vanished = baseline - current
  for c in vanished:
     missing_ticks[c] += 1
     if missing_ticks[c] == 2:        # absent ~60s → real, not a restart blip
        emit(Event(source="detector.process-vanished", severity="error",
                   text=f"A desktop component stopped unexpectedly: {c}."))
  for c in current: missing_ticks[c] = 0   # reappeared → retract
```
- **Pure, testable core:** factor the set-diff into `vanished(baseline, current, session_up) -> list[str]`; unit-test it. The pgrep/poll is the I/O shell.
- **Identity key:** process `comm` (e.g. `cosmic-panel`). Good enough for session infra.
- **Known tuning risk:** closing a *cosmic app* (e.g. `cosmic-files`) could false-fire.
  Mitigation later: only baseline components present shortly after session start, or
  a derived "core session" subset. Accept for 2a, tune live.

### 2. new-coredump — catches real crashes

Algorithm (poll ~60s):
```
entries = `coredumpctl list -o json --no-pager`   (or --since=@<last_ts>)
newest_ts = max(entry.timestamp)
if newest_ts > last_seen_ts:
   for e in entries newer than last_seen_ts:
       emit(Event(source="detector.coredump", severity="error",
                  text=f"{e.exe} crashed ({e.signal})."))
   last_seen_ts = newest_ts
```
- **Pure, testable core:** `new_dumps(entries, last_seen_ts) -> list`.
- Seed `last_seen_ts` = now at startup (don't alert on historical dumps).

### Delivery (2a)
- `emit` → daemon `_broadcast` → `Event` to connected clients. The CLI already
  renders `Event`. That's enough to validate 2a (no desktop notification yet).

### Tests (2a)
- pure `vanished(...)` — baseline forms after 2 ticks; fires after 2 missing ticks;
  retracts on reappear; no fire when `session_up` is False.
- pure `new_dumps(...)` — fires only on entries newer than last_seen.
- (I/O pollers exercised live.)

## Deferred (log only, don't build in 2a)

- **2b — make proactive real for non-technical users:**
  - **Desktop notifications** via `org.freedesktop.Notifications` (session-bus dbus,
    dbus-fast). Without this, proactive is invisible to someone with no terminal.
  - **Model-phrased diagnosis + offered fix** on fire: "Your dock stopped — want me
    to restart it?" (the north-star moment). Model runs on fire (rare). Pulls
    journald context live; offers a deterministic fix action (e.g. relaunch panel,
    or prompt relogin if `rebuild_skew`).
- **2c:** error-rate spike (needs statistical baseline — fuzzy, false-positive
  prone), OOM detector, persisting the liveness baseline across restarts.

## How to resume / verify

1. `nix develop` (or the test shell command in PROGRESS.md How-to-resume).
2. Build `detectors.py`, wire into `MonitorRunner.start()`.
3. Unit-test the two pure cores; `nix flake check`.
4. Live test: deploy, then kill `cosmic-panel` (or wait for a real crash) → confirm
   an `Event` reaches the CLI within ~90s. (Killing the panel is the repeatable
   repro of the original incident.)

## Files touched (expected)
- `src/nixadmin/detectors.py` (new) — ProcessLivenessDetector, CoredumpDetector + pure cores
- `src/nixadmin/monitors.py` or `server.py` — start detectors in `MonitorRunner`
- `tests/test_detectors_smoke.py` (new) — the two pure cores
