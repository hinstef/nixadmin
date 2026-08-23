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

function createCard(titleText) {
  const container = document.getElementById("invoke-result");
  const card = el("article", "operation");
  card.dataset.running = "1";
  card.dataset.phase = "running";
  const indicator = el("span", "operation-indicator");
  indicator.setAttribute("aria-hidden", "true");
  const title = el("div", "operation-title", titleText);
  const phase = el("div", "phase", "Working on it");
  phase.setAttribute("role", "status");
  const answer = el("div", "answer");
  const prompt = el("div");
  const details = el("details");
  details.hidden = true;
  details.append(el("summary", null, "Details"), el("pre"));
  const raw = details.querySelector("pre");
  const foot = el("div", "operation-foot");
  card.append(indicator, title, phase, answer, prompt, details, foot);
  container.prepend(card);
  trimCards(container);
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });

  function addDetail(text) {
    if (!text) return;
    details.hidden = false;
    raw.textContent += `${text}${text.endsWith("\n") ? "" : "\n"}`;
  }

  function finish(kind, onDone) {
    delete card.dataset.running;
    card.dataset.phase = kind;
    foot.textContent = "";
    const dismiss = el("button", null, "Dismiss");
    dismiss.onclick = () => card.remove();
    foot.appendChild(dismiss);
    trimCards(container);
    if (onDone) onDone();
  }

  return { card, phase, answer, prompt, details, foot, addDetail, finish };
}

export async function startTask(title, { initialPhase = "Working on it", run, onDone } = {}) {
  const view = createCard(title);
  view.phase.textContent = initialPhase;
  try {
    const result = await run({
      setPhase: text => { view.phase.textContent = text; },
      addDetail: view.addDetail,
    });
    view.answer.textContent = result.answer || "Done.";
    if (result.detail) view.addDetail(result.detail);
    if (result.ok === false) {
      view.phase.textContent = result.phase || "Couldn’t complete that";
      view.details.open = true;
      view.finish("failed", onDone);
    } else {
      view.phase.textContent = result.phase || "Complete";
      view.finish("complete", onDone);
    }
  } catch (error) {
    view.phase.textContent = "Couldn’t complete that";
    view.answer.textContent = "Nixadmin could not finish this operation.";
    view.addDetail(error instanceof Error ? error.message : String(error));
    view.details.open = true;
    view.finish("failed", onDone);
  }
}

export function startOperation(text, { onDone } = {}) {
  const view = createCard(text);
  const qid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const stream = new EventSource(streamUrl(qid, window.nixadminSession, text));
  let finished = false;
  const stopButton = el("button", null, "Stop");
  stopButton.onclick = () => {
    stopButton.disabled = true;
    view.phase.textContent = "Stopping";
    control("/api/cancel", { qid });
  };
  view.foot.appendChild(stopButton);

  function finish(kind) {
    finished = true;
    stream.close();
    view.finish(kind, onDone);
  }

  stream.addEventListener("status", event => {
    const status = JSON.parse(event.data).text;
    view.phase.textContent = friendlyPhase(status);
    view.addDetail(status);
  });
  stream.addEventListener("delta", event => {
    // The answer belongs in the primary surface. Details are supporting evidence,
    // so do not duplicate streamed answer text there.
    view.answer.textContent += JSON.parse(event.data).text;
  });
  stream.addEventListener("confirm", event => {
    const message = JSON.parse(event.data).text;
    view.phase.textContent = "Waiting for approval";
    view.addDetail(`Confirmation requested: ${message}`);
    view.prompt.textContent = "";
    view.prompt.appendChild(el("div", null, message));
    const choices = el("div", "choices");
    const no = el("button", null, "No");
    const yes = el("button", null, "Yes");
    no.onclick = () => { choices.remove(); view.phase.textContent = "Cancelling"; control("/api/respond", { qid, confirmed: false }); };
    yes.onclick = () => { choices.remove(); view.phase.textContent = "Applying the change"; control("/api/respond", { qid, confirmed: true }); };
    choices.append(no, yes); view.prompt.appendChild(choices);
  });
  stream.addEventListener("input", event => {
    const message = JSON.parse(event.data).prompt;
    view.phase.textContent = "Needs more information";
    view.prompt.textContent = "";
    view.prompt.appendChild(el("div", null, message));
    const choices = el("div", "choices");
    const input = el("input");
    const send = el("button", null, "Send");
    const submit = () => { choices.remove(); view.phase.textContent = "Working on it"; control("/api/respond", { qid, value: input.value }); };
    send.onclick = submit;
    input.onkeydown = event => { if (event.key === "Enter") { event.preventDefault(); submit(); } };
    choices.append(input, send); view.prompt.appendChild(choices); input.focus();
  });
  stream.addEventListener("done", () => {
    view.prompt.textContent = "";
    view.phase.textContent = stopButton.disabled ? "Stopped" : "Complete";
    if (!view.answer.textContent) view.answer.textContent = stopButton.disabled ? "Nothing was changed." : "Done.";
    finish("complete");
  });
  stream.addEventListener("failed", event => {
    view.prompt.textContent = "";
    view.phase.textContent = "Couldn’t complete that";
    view.answer.textContent = JSON.parse(event.data).text || "Something went wrong.";
    view.details.open = true;
    finish("failed");
  });
  stream.addEventListener("error", () => {
    if (finished) return;
    view.phase.textContent = "Connection lost";
    if (!view.answer.textContent) view.answer.textContent = "The connection to nixadmin was interrupted.";
    view.addDetail("The event stream closed before nixadmin reported completion.");
    view.details.open = true;
    finish("failed");
  });
}
