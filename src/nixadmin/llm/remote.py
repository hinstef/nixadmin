"""Remote chain backend — LiteLLM agent loop with tool calling.

The capable model grounds itself by calling tools. Two tool shapes, never a
free-form shell string (see spec "Tool exposure & the argument rule"):

* fetcher-derived zero-arg tools (``<module>_<fetcher>``)
* built-in ``nixadmin_rebuild(action)`` with an enum-validated argument

Tool *execution* is injected as a ``run_tool`` callback so this module stays
focused on the LLM loop and the safety gate lives elsewhere. Text deltas are
streamed out; the loop continues until the model stops requesting tools.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import litellm

from nixadmin.errors import BackendError
from nixadmin.history import Message
from nixadmin.log import get_logger
from nixadmin.sdk import Module

log = get_logger(__name__)

# A tool/message object as the OpenAI-compatible APIs shape them.
JsonDict = dict[str, Any]

# Callback the dispatcher supplies: given (tool_name, args) -> result text.
RunTool = Callable[[str, JsonDict], Awaitable[str]]

REMOTE_SYSTEM_PROMPT = (
    "You are an AI system administrator for a NixOS laptop. The user is non-technical.\n"
    "Questions: run tools to gather data, then give ONE short, plain-language summary.\n"
    "Changes: only when explicitly asked. Never act on a question.\n"
    "Run tools silently; do not narrate what you are about to do."
)

REBUILD_TOOL: JsonDict = {
    "type": "function",
    "function": {
        "name": "nixadmin_rebuild",
        "description": "Rebuild the NixOS configuration. Always 'test' before 'switch'.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["test", "switch", "boot", "revert"]},
            },
            "required": ["action"],
        },
    },
}


def build_tools(modules: list[Module]) -> list[JsonDict]:
    """Build the tool schema list: fetcher-derived zero-arg tools + rebuild."""
    tools: list[JsonDict] = []
    for mod in modules:
        for f in mod.fetchers:
            if not f.expose_as_tool:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{mod.name}_{f.name}",
                    "description": f.description,
                    "parameters": {"type": "object", "properties": {}},
                },
            })
    tools.append(REBUILD_TOOL)
    return tools


async def run(
    query: str,
    *,
    model: str,
    api_base: str | None,
    tools: list[JsonDict],
    run_tool: RunTool,
    history: list[Message] | None = None,
    system_extra: str = "",
) -> AsyncIterator[str]:
    """Drive the agent loop, streaming assistant text deltas."""
    messages: list[JsonDict] = [
        {"role": "system", "content": REMOTE_SYSTEM_PROMPT + system_extra},
        *(history or []),
        {"role": "user", "content": query},
    ]

    while True:
        text_acc = ""
        tool_calls: dict[int, JsonDict] = {}

        try:
            stream = await litellm.acompletion(
                model=model, messages=messages, tools=tools, stream=True,
                **({"api_base": api_base} if api_base else {}),
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    text_acc += delta.content
                    yield delta.content
                for tc in getattr(delta, "tool_calls", None) or []:
                    _accumulate_tool_call(tool_calls, tc)
        except Exception as e:  # noqa: BLE001 — provider/network errors
            raise BackendError(f"remote model request failed: {e}") from e

        if not tool_calls:
            return  # model produced a final answer

        messages.append(_assistant_turn(text_acc, tool_calls))
        for call in tool_calls.values():
            result = await _execute(call, run_tool)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})


# ---- internals ------------------------------------------------------------ #


def _accumulate_tool_call(acc: dict[int, JsonDict], tc: Any) -> None:
    """Merge streamed tool-call fragments by index."""
    idx = getattr(tc, "index", 0)
    slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})
    if getattr(tc, "id", None):
        slot["id"] = tc.id
    fn = getattr(tc, "function", None)
    if fn is not None:
        if getattr(fn, "name", None):
            slot["name"] += fn.name
        if getattr(fn, "arguments", None):
            slot["args"] += fn.arguments


def _assistant_turn(text: str, tool_calls: dict[int, JsonDict]) -> JsonDict:
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["args"]}}
            for c in tool_calls.values()
        ],
    }


async def _execute(call: JsonDict, run_tool: RunTool) -> str:
    try:
        args = json.loads(call["args"]) if call["args"] else {}
    except json.JSONDecodeError:
        return "(error: malformed tool arguments)"
    return await run_tool(call["name"], args)
