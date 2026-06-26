"""Local chain backend — Ollama over HTTP (httpx, async).

Three responsibilities, all against a local Ollama:

* :func:`is_ready` — is the configured model loaded? (drives chain readiness)
* :func:`classify` — match a query to modules; timeout-guarded so a cold-starting
  Ollama never hangs a query (returns ``[]`` on timeout).
* :func:`summarize` — stream a one-sentence answer grounded in prefetched data.

The prompt-building / parsing helpers are pure and unit-tested without a network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from nixadmin.errors import BackendError
from nixadmin.log import get_logger
from nixadmin.sdk import Module

log = get_logger(__name__)

CLASSIFY_TIMEOUT = 2.0  # cold-start guard (seconds)

LOCAL_SYSTEM_PROMPT = (
    "You are a sysadmin assistant for a non-technical user. Use ONLY the inline "
    "system data to answer; never mention where it came from; never invent details.\n"
    "If everything is fine: answer in ONE short sentence "
    "(e.g. 'Yes, your WiFi is connected and the internet is working.').\n"
    "If something is wrong: in at most THREE short sentences say (1) what is wrong, "
    "(2) why — quote the specific error or cause from the data, and (3) the most "
    "likely fix in plain words. No lists, no jargon.\n"
    "Never make changes unless explicitly asked."
)


# ---- pure helpers (unit-tested) ------------------------------------------- #


def build_classify_prompt(query: str, modules: list[Module]) -> str:
    descriptions = "\n".join(f"- {m.name}: {m.description}" for m in modules)
    return (
        "Which categories match this question? Reply with ONLY a comma-separated "
        "list of matching names, or the word 'none'.\n\n"
        f"Categories:\n{descriptions}\n\nQuestion: {query}"
    )


def parse_classify_response(reply: str, modules: list[Module]) -> list[Module]:
    """Map a model reply (comma list of names) back to Module objects."""
    text = reply.strip().lower()
    return [m for m in modules if m.name.lower() in text]


def augment(query: str, context: str) -> str:
    if not context:
        return query
    return (
        f"{query}\n\n"
        "[Live system data — use this to answer directly, do not run these commands "
        f"again:]\n{context}"
    )


# ---- network ops ---------------------------------------------------------- #


async def is_ready(url: str, model: str) -> bool:
    """True if ``model`` is currently loaded in Ollama."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{url}/api/ps")
            r.raise_for_status()
            running = {m.get("name", "") for m in r.json().get("models", [])}
            return model in running
    except Exception:  # noqa: BLE001 — readiness probe must never raise
        return False


async def classify(query: str, modules: list[Module], *, model: str, url: str) -> list[Module]:
    """Match the query to modules. Returns ``[]`` on timeout or error (caller
    treats that as 'no privacy auto-detection this once')."""
    prompt = build_classify_prompt(query, modules)
    body = {
        "model": model, "prompt": prompt, "stream": False,
        "options": {"num_predict": 20, "temperature": 0},
    }
    try:
        async with httpx.AsyncClient(timeout=CLASSIFY_TIMEOUT) as client:
            r = await client.post(f"{url}/api/generate", json=body)
            r.raise_for_status()
            return parse_classify_response(r.json().get("response", ""), modules)
    except Exception as e:  # noqa: BLE001
        log.warning("classify failed; proceeding ungrounded", error=str(e))
        return []


async def judge_package(query: str, candidates: list[str], *, model: str, url: str) -> str:
    """Pick the package the user most likely meant from REAL candidates.

    The model recognises/ranks (its strength) rather than recalls (its weakness),
    and can only return a name that actually exists — the result is constrained to
    the candidate list. Returns '' if it picks nothing recognisable.
    """
    if not candidates:
        return ""
    opts = ", ".join(candidates)
    prompt = (
        f"A user typed '{query}' to install an app. Which of these real nixpkgs "
        "packages did they most likely mean? Reply with ONLY the exact package name "
        f"from the list, or 'none'.\nOptions: {opts}"
    )
    body = {
        "model": model, "prompt": prompt, "stream": False,
        "options": {"num_predict": 16, "temperature": 0},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{url}/api/generate", json=body)
            r.raise_for_status()
            reply = r.json().get("response", "").strip().lower()
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("judge_package failed", error=str(e))
        return ""
    # Constrain to a real candidate: exact match, then substring fallback.
    for c in candidates:
        if c.lower() == reply:
            return c
    for c in candidates:
        if c.lower() in reply:
            return c
    return ""


async def summarize(message: str, *, model: str, url: str) -> AsyncIterator[str]:
    """Stream the local model's answer to an already-augmented message."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": LOCAL_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "stream": True,
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{url}/api/chat", json=body) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    delta = chunk.get("message", {}).get("content", "")
                    if delta:
                        yield delta
                    if chunk.get("done"):
                        break
    except httpx.HTTPError as e:
        raise BackendError(f"local model request failed: {e}") from e
