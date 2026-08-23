import { api } from "./api.js";
import { startOperation, startTask } from "./operation.js";
import { loadTimeline, wireTimelineNavigation } from "./timeline.js";

window.nixadminSession = `web-${Math.random().toString(36).slice(2, 10)}`;
const params = new URLSearchParams(location.search);
if (params.get("surface") === "overlay") document.body.classList.add("overlay");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function refresh() {
  const status = document.getElementById("status");
  const units = document.getElementById("units");
  let data;
  try { data = await (await api("/api/state")).json(); }
  catch { data = { connected: false, units: [] }; }
  units.textContent = "";
  if (!data.connected) {
    status.innerHTML = '<span class="dot down"></span>daemon unreachable';
    units.appendChild(el("div", "muted", "Waiting for nixadmin…"));
    return;
  }
  if (!data.units.length) {
    status.innerHTML = '<span class="dot ok"></span>all services healthy';
    units.appendChild(el("div", "muted", "Nothing needs your attention."));
    return;
  }
  status.innerHTML = `<span class="dot warn"></span>${data.units.length} service(s) need attention`;
  data.units.forEach(unit => units.appendChild(unitCard(unit)));
}

function unitCard(unit) {
  const card = el("div", "unit");
  card.dataset.unit = unit.unit;
  card.appendChild(el("div", "unit-name", unit.unit));
  if (unit.description) card.appendChild(el("div", "description", unit.description));
  const actions = el("div", "actions");
  const restart = el("button", null, "Restart");
  const explain = el("button", null, "Explain");
  const journal = el("button", null, "Journal");
  restart.onclick = async () => {
    restart.disabled = true;
    await startTask(`Restart ${unit.unit}`, {
      initialPhase: "Restarting the service",
      run: async ({ setPhase }) => {
        const result = await (await api("/api/restart", {
          method: "POST", body: JSON.stringify({ unit: unit.unit, scope: unit.scope }),
        })).json();
        setPhase("Verifying the result");
        return { answer: result.result || "Restarted.", ok: result.ok };
      },
      onDone: () => { loadTimeline({ reset: true }); setTimeout(refresh, 800); },
    });
    restart.disabled = false;
  };
  explain.onclick = () => runExplain(unit.unit, unit.scope, explain);
  journal.onclick = async () => {
    journal.disabled = true;
    await startTask(`Journal for ${unit.unit}`, {
      initialPhase: "Loading technical details",
      run: async () => {
        const result = await (await api(`/api/journal?unit=${encodeURIComponent(unit.unit)}&scope=${encodeURIComponent(unit.scope)}`)).json();
        return {
          answer: result.text ? "Journal details are ready." : "No recent journal entries were found.",
          detail: result.text || "No journal entries.",
        };
      },
      onDone: () => loadTimeline({ reset: true }),
    });
    journal.disabled = false;
  };
  actions.append(restart, explain, journal); card.appendChild(actions);
  return card;
}

async function runExplain(unit, scope, button) {
  if (button) button.disabled = true;
  await startTask(`Explain ${unit}`, {
    initialPhase: "Looking into the failure",
    run: async ({ setPhase }) => {
      const result = await (await api("/api/explain", {
        method: "POST", body: JSON.stringify({ unit, scope }),
      })).json();
      setPhase("Putting it into plain language");
      return { answer: result.text || "No explanation available.", ok: result.ok };
    },
    onDone: () => loadTimeline({ reset: true }),
  });
  if (button) button.disabled = false;
}

async function loadLedger() {
  let ledger;
  try { ledger = (await (await api("/api/ledger")).json()).ledger; }
  catch { ledger = null; }
  const section = document.getElementById("kept-sec");
  if (!ledger || ledger.since_ts === null) { section.hidden = true; return; }
  const box = document.getElementById("kept");
  box.textContent = "";
  box.appendChild(el("div", "kept-head", ledger.headline || ""));
  if (ledger.tally?.length) box.appendChild(el("div", "kept-tally", `Quietly: ${ledger.tally.join(" · ")}`));
  section.hidden = false;
}

const chips = [
  { label: "Install an app…", fill: "install " },
  { label: "Remove an app…", fill: "remove " },
  { label: "Is anything broken?", run: "is anything broken?" },
  { label: "Disk space", run: "how is my disk space?" },
  { label: "Wifi", run: "how is my wifi?" },
];

function wireInvoke() {
  const form = document.getElementById("ask");
  const input = document.getElementById("ask-input");
  const chipBox = document.getElementById("chips");
  const submit = text => startOperation(text, { onDone: () => loadTimeline({ reset: true }) });
  chips.forEach(chip => {
    const button = el("button", "chip", chip.label); button.type = "button";
    button.onclick = () => {
      if (chip.run) submit(chip.run);
      else { input.value = chip.fill; input.focus(); }
    };
    chipBox.appendChild(button);
  });
  form.onsubmit = event => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = ""; submit(text);
  };
  document.addEventListener("keydown", event => {
    const typing = document.activeElement?.tagName === "INPUT";
    if ((event.ctrlKey || event.metaKey) && event.key === "k") { event.preventDefault(); input.focus(); input.select(); }
    else if (event.key === "/" && !typing) { event.preventDefault(); input.focus(); }
  });
}

async function boot() {
  wireInvoke(); wireTimelineNavigation();
  if (!params.get("explain")) document.getElementById("ask-input").focus();
  await Promise.all([refresh(), loadLedger(), loadTimeline()]);
  if (params.get("explain")) {
    const unit = params.get("explain");
    const card = [...document.querySelectorAll(".unit")].find(node => node.dataset.unit === unit);
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      const button = [...card.querySelectorAll("button")].find(node => node.textContent === "Explain");
      runExplain(unit, params.get("scope") || "system", button);
    }
  }
}

boot();
setInterval(refresh, 15_000);
setInterval(() => loadTimeline({ background: true }), 20_000);
setInterval(loadLedger, 30_000);
