const TOKEN = document.querySelector('meta[name="nixadmin-token"]').content;

export async function api(path, options = {}) {
  const headers = { "X-Nixadmin-Token": TOKEN, ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response;
}

export function control(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

export function streamUrl(qid, session, text) {
  const params = new URLSearchParams({ token: TOKEN, qid, session, text });
  return `/api/stream?${params}`;
}
