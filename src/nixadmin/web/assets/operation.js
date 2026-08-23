import { control, streamUrl } from "./api.js";

const MAX_CARDS = 6;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function friendlyPhase(text) {
  const value = text.toLowerCase();
  if (value.includes("confirm") || value.includes("approval")) return "Waiting for approval";
  if (value.includes("verif") || value.includes("check")) return "Verifying the result";
  if (value.includes("build") || value.includes("switch") || value.includes("apply")) return "Applying the change";
  if (value.includes("warm") || value.includes("model")) return "Getting ready";
  return "Working on it";
}

function trimCards(container) {
  [...container.children].slice(MAX_CARDS).forEach(card => {
    if (!card.dataset.running) card.remove();
  });
}

export function startOperation(text, { onDone } = {}) {
  const container = document.getElementById("invoke-result");
  const card = el("article", "operation");
  card.dataset.running = "1";
  card.dataset.phase = "running";
  const indicator = el("span", "operation-indicator");
  indicator.setAttribute("aria-hidden", "true");
  const title = el("div", "operation-title", text);
  const phase = el("div", "phase", "Working on it");
  phase.setAttribute("role", "status");
  const answer = el("div", "answer");
  const prompt = el("div");
  const details = el("details");
  details.append(el("summary", null, "Details"), el("pre"));
  const raw = details.querySelector("pre");
  const foot = el("div", "operation-foot");
  card.append(indicator, title, phase, answer, prompt, details, foot);
  container.prepend(card);
  trimCards(container);

  const qid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const session = window.nixadminSession;
  const stream = new EventSource(streamUrl(qid, session, text));
  let finished = false;
  const stopButton = el("button", null, "Stop");
  stopButton.onclick = () => {
    stopButton.disabled = true;
    phase.textContent = "Stopping";
    control("/api/cancel", { qid });
  };
  foot.appendChild(stopButton);

  function finish(kind) {
    finished = true;
    stream.close();
    delete card.dataset.running;
    card.dataset.phase = kind;
    foot.textContent = "";
    const dismiss = el("button", null, "Dismiss");
    dismiss.onclick = () => card.remove();
    foot.appendChild(dismiss);
    trimCards(container);
    if (onDone) onDone();
  }

  stream.addEventListener("status", event => {
    const status = JSON.parse(event.data).text;
    phase.textContent = friendlyPhase(status);
    raw.textContent += `${status}\n`;
  });
  stream.addEventListener("delta", event => {
    const delta = JSON.parse(event.data).text;
    answer.textContent += delta;
    raw.textContent += delta;
  });
  stream.addEventListener("confirm", event => {
    const message = JSON.parse(event.data).text;
    phase.textContent = "Waiting for approval";
    raw.textContent += `${message}\n`;
    prompt.textContent = "";
    prompt.appendChild(el("div", null, message));
    const choices = el("div", "choices");
    const no = el("button", null, "No");
    const yes = el("button", null, "Yes");
    no.onclick = () => { choices.remove(); phase.textContent = "Cancelling"; control("/api/respond", { qid, confirmed: false }); };
    yes.onclick = () => { choices.remove(); phase.textContent = "Applying the change"; control("/api/respond", { qid, confirmed: true }); };
    choices.append(no, yes); prompt.appendChild(choices);
  });
  stream.addEventListener("input", event => {
    const message = JSON.parse(event.data).prompt;
    phase.textContent = "Needs more information";
    prompt.textContent = "";
    prompt.appendChild(el("div", null, message));
    const choices = el("div", "choices");
    const input = el("input");
    const send = el("button", null, "Send");
    const submit = () => { choices.remove(); phase.textContent = "Working on it"; control("/api/respond", { qid, value: input.value }); };
    send.onclick = submit;
    input.onkeydown = event => { if (event.key === "Enter") { event.preventDefault(); submit(); } };
    choices.append(input, send); prompt.appendChild(choices); input.focus();
  });
  stream.addEventListener("done", () => {
    prompt.textContent = "";
    phase.textContent = stopButton.disabled ? "Stopped" : "Complete";
    if (!answer.textContent) answer.textContent = stopButton.disabled ? "Nothing was changed." : "Done.";
    finish("complete");
  });
  stream.addEventListener("failed", event => {
    prompt.textContent = "";
    phase.textContent = "Couldn’t complete that";
    answer.textContent = JSON.parse(event.data).text || "Something went wrong.";
    details.open = true;
    finish("failed");
  });
  stream.addEventListener("error", () => {
    if (finished) return;
    phase.textContent = "Connection lost";
    if (!answer.textContent) answer.textContent = "The connection to nixadmin was interrupted.";
    details.open = true;
    finish("failed");
  });
}
