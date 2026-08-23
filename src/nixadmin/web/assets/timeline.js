import { api } from "./api.js";

const PAGE_SIZE = 5;
const EPISODE_WINDOW_S = 15 * 60;
const LIFECYCLE_KINDS = new Set(["failure_observed", "failure_cleared", "restart", "autofix"]);
const ICONS = {
  episode: "✓", failure_observed: "!", failure_cleared: "✓", explanation: "?",
  restart: "↻", journal_snapshot: "≡", monitor_event: "·", ask: "›", action: "◆", autofix: "↻",
};
const state = { cursors: [null], page: 0, next: null, newestId: null };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function serviceName(unit) {
  const base = (unit || "service").replace(/\.(service|socket|timer|target)$/, "");
  const words = base.replaceAll("-", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function timeLabel(ts) {
  const date = new Date(ts * 1000);
  const age = Date.now() - date.getTime();
  if (age < 60_000) return "now";
  if (age < 3_600_000) return `${Math.floor(age / 60_000)}m`;
  if (age < 86_400_000) return `${Math.floor(age / 3_600_000)}h`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function shortText(text, max = 150) {
  const line = (text || "").split("\n").find(part => part.trim()) || "";
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function groupEpisodes(events) {
  const groups = [];
  for (const event of events) {
    const previous = groups.at(-1);
    const canJoin = event.unit && LIFECYCLE_KINDS.has(event.kind) && previous?.episode &&
      previous.unit === event.unit && previous.scope === event.scope &&
      Math.abs(previous.events.at(-1).ts - event.ts) <= EPISODE_WINDOW_S;
    if (canJoin) previous.events.push(event);
    else if (event.unit && LIFECYCLE_KINDS.has(event.kind)) {
      groups.push({ episode: true, unit: event.unit, scope: event.scope, ts: event.ts, events: [event] });
    } else groups.push({ episode: false, unit: event.unit, scope: event.scope, ts: event.ts, events: [event] });
  }
  return groups;
}

function episodePresentation(group) {
  const kinds = new Set(group.events.map(event => event.kind));
  const name = serviceName(group.unit);
  const attempted = kinds.has("restart") || kinds.has("autofix");
  if (kinds.has("failure_cleared") && attempted) {
    return { icon: "episode", title: `${name} recovered`, summary: "Nixadmin restarted it and verified that it came back." };
  }
  if (kinds.has("failure_cleared")) return { icon: "episode", title: `${name} is working again`, summary: "The service recovered." };
  const successful = group.events.some(event => event.meta?.ok === true || event.meta?.outcome === "healthy");
  if (attempted && successful) return { icon: "episode", title: `${name} was restored`, summary: "Nixadmin restarted it successfully." };
  if (attempted) return { icon: "failure_observed", title: `${name} still needs attention`, summary: "A restart was attempted, but the problem may remain." };
  return { icon: "failure_observed", title: `${name} needs attention`, summary: shortText(group.events[0].text) || "The service stopped unexpectedly." };
}

function eventPresentation(event) {
  const name = serviceName(event.unit);
  if (event.kind === "explanation") return { title: `Explained why ${name} stopped`, summary: shortText(event.text) };
  if (event.kind === "journal_snapshot") return { title: `Viewed technical details for ${name}`, summary: "Journal entries were collected for inspection." };
  if (event.kind === "ask") return { title: `Asked “${shortText(event.text, 90)}”`, summary: shortText(event.meta?.answer) };
  if (event.kind === "action") {
    const request = event.meta?.request;
    return { title: request ? `Requested “${shortText(request, 90)}”` : "Changed the system", summary: shortText(event.text) };
  }
  if (event.kind === "monitor_event") return { title: shortText(event.text) || "System activity detected", summary: "" };
  return { title: event.unit ? `${name} changed` : "System activity", summary: shortText(event.text) };
}

function rawEvidence(events) {
  return events.map(event => {
    const exact = new Date(event.ts * 1000).toLocaleString();
    const header = `${exact} · ${event.kind}${event.unit ? ` · ${event.unit}` : ""}`;
    const parts = [header];
    if (event.text) parts.push(event.text);
    if (event.meta && Object.keys(event.meta).length) parts.push(`metadata: ${JSON.stringify(event.meta, null, 2)}`);
    return parts.join("\n");
  }).join("\n\n");
}

function eventRow(group) {
  const presentation = group.episode ? episodePresentation(group) : eventPresentation(group.events[0]);
  const row = el("li", "event");
  const time = el("time", "event-time", timeLabel(group.ts));
  time.dateTime = new Date(group.ts * 1000).toISOString();
  time.title = new Date(group.ts * 1000).toLocaleString();
  row.append(time, el("div", "event-icon", ICONS[presentation.icon || group.events[0].kind] || "·"));
  const body = el("div");
  body.appendChild(el("div", "event-title", presentation.title));
  if (presentation.summary) body.appendChild(el("div", "event-text", presentation.summary));
  const disclosure = el("details");
  disclosure.append(el("summary", null, "Details"), el("pre", null, rawEvidence(group.events)));
  disclosure.open = group.events.some(event => ["error", "critical"].includes(event.severity));
  body.appendChild(disclosure);
  row.appendChild(body);
  return row;
}

export async function loadTimeline({ reset = false, background = false } = {}) {
  if (reset) { state.cursors = [null]; state.page = 0; }
  const cursor = background && state.page > 0 ? null : state.cursors[state.page];
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (cursor !== null) query.set("before", String(cursor));
  let data;
  try { data = await (await api(`/api/timeline?${query}`)).json(); }
  catch { data = { events: [], next_cursor: null }; }

  const events = data.events || [];
  if (background && state.page > 0) {
    const latest = events.length ? events[0].id : null;
    if (latest !== state.newestId) document.getElementById("new-activity").hidden = false;
    return;
  }
  if (state.page === 0) state.newestId = events.length ? events[0].id : null;
  state.next = data.next_cursor;
  const list = document.getElementById("timeline");
  list.textContent = "";
  if (!events.length) list.appendChild(el("li", "muted", "No activity recorded yet."));
  else groupEpisodes(events).forEach(group => list.appendChild(eventRow(group)));
  document.getElementById("newer").disabled = state.page === 0;
  document.getElementById("older").disabled = state.next === null;
  document.getElementById("page-label").textContent = state.page === 0 ? "Latest" : `Page ${state.page + 1}`;
}

export function wireTimelineNavigation() {
  document.getElementById("older").onclick = () => {
    if (state.next === null) return;
    state.cursors[state.page + 1] = state.next;
    state.page += 1;
    loadTimeline();
  };
  document.getElementById("newer").onclick = () => { if (state.page > 0) { state.page -= 1; loadTimeline(); } };
  document.getElementById("new-activity").onclick = event => {
    event.currentTarget.hidden = true;
    loadTimeline({ reset: true });
  };
}
