import { api } from "./api.js";

const PAGE_SIZE = 5;
const ICONS = {
  failure_observed: "⚠️", failure_cleared: "✓", explanation: "💬",
  restart: "🔧", journal_snapshot: "📄", monitor_event: "🔔",
  ask: "›", action: "◆", autofix: "✦",
};

const state = { cursors: [null], page: 0, next: null, newestId: null };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function timeLabel(ts) {
  const date = new Date(ts * 1000);
  const age = Date.now() - date.getTime();
  if (age < 60_000) return "now";
  if (age < 3_600_000) return `${Math.floor(age / 60_000)}m`;
  if (age < 86_400_000) return `${Math.floor(age / 3_600_000)}h`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function eventRow(event) {
  const row = el("li", "event");
  const time = el("time", "event-time", timeLabel(event.ts));
  time.dateTime = new Date(event.ts * 1000).toISOString();
  time.title = new Date(event.ts * 1000).toLocaleString();
  row.append(time, el("div", "event-icon", ICONS[event.kind] || "·"));
  const body = el("div");
  const title = event.unit ? `${event.unit} · ${event.kind.replaceAll("_", " ")}` : event.kind.replaceAll("_", " ");
  body.appendChild(el("div", "event-title", title));
  const text = event.text || "";
  const answer = event.kind === "ask" && event.meta ? event.meta.answer : "";
  const detail = [text, answer].filter(Boolean).join("\n\n");
  if (detail) {
    const disclosure = el("details");
    disclosure.appendChild(el("summary", null, detail.split("\n")[0].slice(0, 110)));
    disclosure.appendChild(el("pre", null, detail));
    // Failures should be visible, while routine diagnostic material stays quiet.
    disclosure.open = event.severity === "error" || event.severity === "critical";
    body.appendChild(disclosure);
  }
  row.appendChild(body);
  return row;
}

export async function loadTimeline({ reset = false, background = false } = {}) {
  if (reset) { state.cursors = [null]; state.page = 0; }
  // Poll the head while browsing history; never refresh the older page in place
  // or move the reader. A badge offers an explicit return to new activity.
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
  else events.forEach(event => list.appendChild(eventRow(event)));
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
