"""The single-page web view. Self-contained HTML/CSS/JS — no external assets, no
frameworks. The token is injected server-side into a page only ever served on a
valid token; the page then sends it as a header on every API call."""

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
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0; background: #16181d; color: #e6e8ee; }
  header { padding: 20px 24px; border-bottom: 1px solid #262a33; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  #status { margin-top: 6px; font-size: 14px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 7px; vertical-align: baseline; }
  .ok { background: #2ecc71; } .warn { background: #f39c12; } .down { background: #95a5a6; }
  main { padding: 16px 24px; max-width: 820px; }
  .unit { border: 1px solid #262a33; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; background: #1b1e25; }
  .unit h2 { font-size: 15px; margin: 0 0 2px; font-family: ui-monospace, monospace; }
  .desc { color: #9aa0ac; font-size: 13px; }
  .scope { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #6b7280; margin-left: 6px; }
  .actions { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
  button { font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 6px; border: 1px solid #333844; background: #232733; color: #e6e8ee; cursor: pointer; }
  button:hover { background: #2b3040; } button:disabled { opacity: .5; cursor: default; }
  pre { background: #101216; border: 1px solid #262a33; border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 12px; color: #cfd3dc; margin-top: 10px; white-space: pre-wrap; }
  .explain { margin-top: 10px; padding: 10px 12px; border-left: 3px solid #f39c12; background: #1f2129; border-radius: 4px; font-size: 14px; }
  .muted { color: #6b7280; }
</style>
</head>
<body>
<header>
  <h1>nixadmin — system health</h1>
  <div id="status"><span class="dot down"></span>connecting…</div>
</header>
<main id="units"></main>
<script>
const TOKEN = "__NIXADMIN_TOKEN__";
async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Nixadmin-Token": TOKEN }, opts.headers || {});
  return fetch(path, opts);
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

async function refresh() {
  let data;
  try { data = await (await api("/api/state")).json(); }
  catch (e) { data = { connected: false, units: [] }; }
  const status = document.getElementById("status");
  const units = document.getElementById("units");
  units.textContent = "";
  if (!data.connected) {
    status.innerHTML = '<span class="dot down"></span>daemon unreachable';
    return;
  }
  if (!data.units.length) {
    status.innerHTML = '<span class="dot ok"></span>all services healthy';
    return;
  }
  status.innerHTML = '<span class="dot warn"></span>' + data.units.length + ' service(s) failed';
  for (const u of data.units) units.appendChild(unitCard(u));
}

function unitCard(u) {
  const card = el("div", "unit");
  const h = el("h2"); h.textContent = u.unit;
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
    out.textContent = ""; const p = el("div", "explain", r.result || "done"); out.appendChild(p);
    setTimeout(refresh, 800);
  };
  bExplain.onclick = async () => {
    bExplain.disabled = true; bExplain.textContent = "Thinking…";
    const r = await (await api("/api/explain", { method: "POST", body: JSON.stringify({ unit: u.unit, scope: u.scope }) })).json();
    out.textContent = ""; out.appendChild(el("div", "explain", r.text || "(no explanation)"));
    bExplain.disabled = false; bExplain.textContent = "Explain";
  };
  bLog.onclick = async () => {
    const r = await (await api("/api/journal?unit=" + encodeURIComponent(u.unit) + "&scope=" + encodeURIComponent(u.scope))).json();
    out.textContent = ""; const pre = el("pre"); pre.textContent = r.text || "(no journal)"; out.appendChild(pre);
  };
  return card;
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


def render(token: str) -> str:
    """The page HTML with the session token embedded."""
    return _PAGE.replace("__NIXADMIN_TOKEN__", token)
