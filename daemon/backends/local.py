"""
Local backend — Ollama direct HTTP.
Classify → prefetch → augment → summarize. No tool calls, no agentic loop.
"""

import json
import subprocess
import threading
import urllib.request
from typing import AsyncGenerator


SYSTEM_PROMPT = (
    "You are a sysadmin assistant. The user is non-technical.\n"
    "STRICT: ONE sentence. Stop after the period. No lists, no caveats.\n"
    "Good: 'Yes, your WiFi is connected and the internet is working.'\n"
    "Use the inline system data to answer. Never mention where the data came from.\n"
    "Never make changes unless explicitly asked."
)


def classify(query: str, model: str, ollama_url: str, modules) -> list:
    descriptions = "\n".join(f"- {m.name}: {m.description}" for m in modules)
    prompt = (
        "Which categories match this question? "
        "Reply with ONLY a comma-separated list of matching names, or the word 'none'.\n\n"
        f"Categories:\n{descriptions}\n\nQuestion: {query}"
    )
    try:
        data = json.dumps({
            "model": model, "prompt": prompt, "stream": False,
            "options": {"num_predict": 20, "temperature": 0},
        }).encode()
        req = urllib.request.Request(
            ollama_url + "/api/generate", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            reply = json.loads(r.read())["response"].strip().lower()
        return [m for m in modules if m.name in reply]
    except Exception:
        return []


def _run_cmd(cmd: str, timeout: int = 15) -> str:
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT, timeout=timeout, text=True
        ).strip()
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip() or f"(exit {e.returncode})"
    except Exception as e:
        return f"(error: {e})"


def prefetch(matched_modules) -> str:
    if not matched_modules:
        return ""
    fetchers = [f for m in matched_modules for f in m.fetchers]
    results = {}
    lock = threading.Lock()

    def run(f):
        out = _run_cmd(f.cmd, f.timeout)
        with lock:
            results[f.cmd] = out

    threads = [threading.Thread(target=run, args=(f,)) for f in fetchers]
    for t in threads: t.start()
    for t in threads: t.join(timeout=20)

    return "\n\n".join(f"$ {f.cmd}\n{results.get(f.cmd, '(timeout)')}" for f in fetchers)


def augment(query: str, context: str) -> str:
    if not context:
        return query
    return (
        query + "\n\n"
        "[Live system data — use this to answer directly, do not re-run these commands:]\n"
        + context
    )


async def call(query: str, model: str, ollama_url: str, modules) -> AsyncGenerator[str, None]:
    matched = classify(query, model, ollama_url, modules)
    context = prefetch(matched)
    message = augment(query, context)

    data = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": message},
        ],
        "stream": True,
    }).encode()

    req = urllib.request.Request(
        ollama_url + "/api/chat", data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        for raw in r:
            chunk = json.loads(raw)
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                yield delta
            if chunk.get("done"):
                break
