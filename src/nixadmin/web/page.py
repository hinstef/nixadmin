"""The single-page web view — the hub. Self-contained HTML/CSS/JS, no external
assets, no frameworks. The token is injected server-side into a page only ever
served on a valid token; the page then sends it as a header on every API call.

Two sections:

* **Now** — current health and the failed units, with per-unit Restart / Explain /
  Journal. This section refreshes on a timer; refreshing only ever re-renders
  *itself*, so it can no longer wipe out detail the way the old single-list page
  did (that was the "explanation disappears after a few seconds" bug).
* **Timeline** — the persisted event history (failures, explanations, restarts,
  journals, monitor events) read from ``/api/timeline``. It survives refreshes and
  daemon restarts because it comes from the on-disk event store. Explaining or
  restarting a unit lands there too, so those results *stick*.

The tray deep-links here with ``?explain=<unit>&scope=<scope>`` to run and show an
explanation in the hub instead of a transient desktop notification.
"""

from __future__ import annotations

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>nixadmin — system health</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0; background: #16181d; color: #e6e8ee; }
  header { padding: 20px 24px; border-bottom: 1px solid #262a33; position: sticky; top: 0; background: #16181d; z-index: 2; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  #status { margin-top: 6px; font-size: 14px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 7px; vertical-align: baseline; }
  .ok { background: #2ecc71; } .warn { background: #f39c12; } .down { background: #95a5a6; }
  main { padding: 16px 24px 40px; max-width: 860px; margin: 0 auto; }
  section { margin-top: 8px; }
  h2.sec { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: #6b7280; margin: 22px 0 10px; font-weight: 600; }
  .unit { border: 1px solid #262a33; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; background: #1b1e25; }
  .unit.focus { border-color: #f39c12; box-shadow: 0 0 0 1px #f39c1240; }
  .unit h3 { font-size: 15px; margin: 0 0 2px; font-family: ui-monospace, monospace; font-weight: 600; }
  .desc { color: #9aa0ac; font-size: 13px; }
  .scope { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #6b7280; margin-left: 6px; }
  .actions { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
  button { font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 6px; border: 1px solid #333844; background: #232733; color: #e6e8ee; cursor: pointer; }
  button:hover { background: #2b3040; } button:disabled { opacity: .5; cursor: default; }
  pre { background: #101216; border: 1px solid #262a33; border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 12px; color: #cfd3dc; margin: 8px 0 0; white-space: pre-wrap; word-break: break-word; }
  .explain { margin-top: 10px; padding: 10px 12px; border-left: 3px solid #f39c12; background: #1f2129; border-radius: 4px; font-size: 14px; white-space: pre-wrap; }
  .muted { color: #6b7280; }
  .allclear { color: #9aa0ac; padding: 8px 0; }
  /* timeline */
  ol.tl { list-style: none; margin: 0; padding: 0; }
  li.ev { display: grid; grid-template-columns: 72px 22px 1fr; gap: 8px; padding: 8px 0; border-top: 1px solid #21252e; align-items: baseline; }
  li.ev:first-child { border-top: none; }
  .ev-time { color: #6b7280; font-size: 12px; font-variant-numeric: tabular-nums; }
  .ev-icon { text-align: center; }
  .ev-body { min-width: 0; }
  .ev-unit { font-family: ui-monospace, monospace; font-size: 13px; color: #cbd0da; }
  .ev-kind { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #6b7280; margin-left: 6px; }
  .ev-text { font-size: 13.5px; color: #d7dbe4; white-space: pre-wrap; word-break: break-word; margin-top: 2px; }
  details.ev-more > summary { cursor: pointer; color: #8b93a3; font-size: 12px; }
  details.ev-more pre { margin-top: 6px; }
</style>
</head>
<body>
<header>
  <h1>nixadmin — system health</h1>
  <div id="status"><span class="dot down"></span>connecting…</div>
</header>
<main>
  <section>
    <h2 class="sec">Now</h2>
    <div id="units"></div>
  </section>
  <section>
    <h2 class="sec">Timeline</h2>
    <ol class="tl" id="timeline"></ol>
  </section>
</main>
<script>
const TOKEN = "__NIXADMIN_TOKEN__";
const PARAMS = new URLSearchParams(location.search);
const FOCUS_UNIT = PARAMS.get("explain") || PARAMS.get("unit") || null;

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Nixadmin-Token": TOKEN }, opts.headers || {});
  return fetch(path, opts);
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

// ---- Now: current health + failed-unit cards -------------------------------
async function refresh() {
  let data;
  try { data = await (await api("/api/state")).json(); }
  catch (e) { data = { connected: false, units: [] }; }
  const status = document.getElementById("status");
  const units = document.getElementById("units");
  units.textContent = "";  // only this section — never the timeline
  if (!data.connected) {
    status.innerHTML = '<span class="dot down"></span>daemon unreachable';
    return;
  }
  if (!data.units.length) {
    status.innerHTML = '<span class="dot ok"></span>all services healthy';
    units.appendChild(el("div", "allclear", "Nothing is failing right now."));
    return;
  }
  status.innerHTML = '<span class="dot warn"></span>' + data.units.length + ' service(s) failed';
  for (const u of data.units) units.appendChild(unitCard(u));
}

function unitCard(u) {
  const card = el("div", "unit");
  card.dataset.unit = u.unit;
  if (u.unit === FOCUS_UNIT) card.classList.add("focus");
  const h = el("h3"); h.textContent = u.unit;
  h.appendChild(el("span", "scope", u.scope));
  card.appendChild(h);
  if (u.description) card.appendChild(el("div", "desc", u.description));
  const actions = el("div", "actions");
  const bRestart = el("button", null, "Restart");
  const bExplain = el("button", null, "Explain");
  const bLog = el("button", null, "Show journal");
  actions.append(bRestart, bExplain, bLog);
  card.appendChild(actions);
  const out = el("div"); card.appendChild(out);

  bRestart.onclick = async () => {
    bRestart.disabled = true; bRestart.textContent = "Restarting…";
    const r = await (await api("/api/restart", { method: "POST", body: JSON.stringify({ unit: u.unit, scope: u.scope }) })).json();
    out.textContent = ""; out.appendChild(el("div", "explain", r.result || "done"));
    loadTimeline();
    setTimeout(refresh, 800);
  };
  bExplain.onclick = () => runExplain(u.unit, u.scope, out, bExplain);
  bLog.onclick = async () => {
    bLog.disabled = true; bLog.textContent = "Loading…";
    const r = await (await api("/api/journal?unit=" + encodeURIComponent(u.unit) + "&scope=" + encodeURIComponent(u.scope))).json();
    out.textContent = ""; const pre = el("pre"); pre.textContent = r.text || "(no journal)"; out.appendChild(pre);
    bLog.disabled = false; bLog.textContent = "Show journal";
    loadTimeline();
  };
  return card;
}

async function runExplain(unit, scope, out, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Thinking…"; }
  out.textContent = "";
  out.appendChild(el("div", "explain muted", "Looking into " + unit + "…"));
  const r = await (await api("/api/explain", { method: "POST", body: JSON.stringify({ unit: unit, scope: scope }) })).json();
  out.textContent = ""; out.appendChild(el("div", "explain", r.text || "(no explanation)"));
  if (btn) { btn.disabled = false; btn.textContent = "Explain"; }
  loadTimeline();  // the explanation is now persisted — reflect it below
}

// ---- Timeline: persisted event history -------------------------------------
const ICONS = {
  failure_observed: "\\u26a0\\ufe0f", failure_cleared: "\\u2705", explanation: "\\ud83d\\udcac",
  restart: "\\ud83d\\udd27", journal_snapshot: "\\ud83d\\udcc4", monitor_event: "\\ud83d\\udd14", autofix: "\\ud83e\\udd16",
};
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
async function loadTimeline() {
  let events;
  try { events = (await (await api("/api/timeline?limit=100")).json()).events || []; }
  catch (e) { events = []; }
  const tl = document.getElementById("timeline");
  tl.textContent = "";
  if (!events.length) { tl.appendChild(el("li", "muted", "No events recorded yet.")); return; }
  for (const ev of events) tl.appendChild(eventRow(ev));
}
function eventRow(ev) {
  const li = el("li", "ev");
  li.appendChild(el("div", "ev-time", fmtTime(ev.ts)));
  li.appendChild(el("div", "ev-icon", ICONS[ev.kind] || "\\u2022"));
  const body = el("div", "ev-body");
  const line = el("div");
  if (ev.unit) line.appendChild(el("span", "ev-unit", ev.unit));
  line.appendChild(el("span", "ev-kind", ev.kind.replace(/_/g, " ")));
  body.appendChild(line);
  const text = ev.text || "";
  if (text.length > 240 || ev.kind === "journal_snapshot") {
    const d = el("details", "ev-more");
    d.appendChild(el("summary", null, text.split("\\n")[0].slice(0, 120) + " — show more"));
    const pre = el("pre"); pre.textContent = text; d.appendChild(pre);
    body.appendChild(d);
  } else if (text) {
    body.appendChild(el("div", "ev-text", text));
  }
  li.appendChild(body);
  return li;
}

// ---- boot ------------------------------------------------------------------
async function boot() {
  await refresh();
  await loadTimeline();
  // Tray deep-link: run + show the explanation right here (persisted, not a bubble).
  if (PARAMS.get("explain")) {
    const unit = PARAMS.get("explain");
    const scope = PARAMS.get("scope") || "system";
    let card = document.querySelector('.unit[data-unit="' + (window.CSS && CSS.escape ? CSS.escape(unit) : unit) + '"]');
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      const out = card.querySelector("div:last-child");
      const btn = Array.from(card.querySelectorAll("button")).find(b => b.textContent === "Explain");
      runExplain(unit, scope, out, btn);
    }
  }
}
boot();
setInterval(refresh, 15000);
setInterval(loadTimeline, 20000);
</script>
</body>
</html>
"""


def render(token: str) -> str:
    """The page HTML with the session token embedded."""
    return _PAGE.replace("__NIXADMIN_TOKEN__", token)
