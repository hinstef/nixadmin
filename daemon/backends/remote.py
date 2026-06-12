"""
Remote backend — LiteLLM (Hermes proxy, API keys, OpenRouter, etc.)
Full agentic loop with tool calling. Safety gates enforced here, not by the LLM.
"""

import json
import subprocess
from typing import AsyncGenerator

import litellm


SYSTEM_PROMPT = """\
You are an AI system administrator for a NixOS laptop. The user is non-technical.

Rules:
1. Questions → run commands, give ONE short summary. Never explain what you're about to do.
2. Changes → only when explicitly asked. Never act on a question.
3. Run commands silently first. Write nothing between tool calls.

Hard limits — the daemon enforces these, but you must never attempt to bypass them:
- Never touch hardware-configuration.nix
- Always test before switch
- Always confirm with user before applying changes
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a safe read-only system command",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nixadmin_rebuild",
            "description": "Rebuild the NixOS configuration",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["test", "switch", "boot", "revert"],
                    }
                },
                "required": ["action"],
            },
        },
    },
]

ALLOWED_COMMANDS = {
    "nixadmin-apps", "df -h", "lsblk", "ip link show",
    "nmcli device status", "systemctl --failed --no-pager",
    "ping -c 2 8.8.8.8", "uname -r", "lscpu",
}


def _execute_tool(name: str, args: dict, confirm_fn) -> str:
    """Execute a tool call. Safety gates live here."""
    if name == "run_command":
        cmd = args.get("command", "")
        if cmd not in ALLOWED_COMMANDS:
            return f"(blocked: '{cmd}' is not in the allowed command list)"
        try:
            return subprocess.check_output(
                cmd, shell=True, stderr=subprocess.STDOUT, timeout=15, text=True
            ).strip()
        except subprocess.CalledProcessError as e:
            return (e.output or "").strip() or f"(exit {e.returncode})"

    if name == "nixadmin_rebuild":
        action = args.get("action", "test")
        if action == "switch":
            if not confirm_fn(f"Apply the NixOS configuration change?"):
                return "(cancelled by user)"
        import socket, json as _json
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect("/run/nixadmin-helper.sock")
        sock.sendall(_json.dumps({"action": action}).encode())
        sock.shutdown(socket.SHUT_WR)
        buf = b""
        while chunk := sock.recv(4096):
            buf += chunk
        return buf.decode()

    return f"(unknown tool: {name})"


async def call(
    query: str,
    model: str,
    api_base: str | None,
    confirm_fn,
    system_prompt_extra: str = "",
) -> AsyncGenerator[str, None]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + system_prompt_extra},
        {"role": "user", "content": query},
    ]

    kwargs = {"model": model, "messages": messages, "tools": TOOLS, "stream": True}
    if api_base:
        kwargs["api_base"] = api_base

    while True:
        tool_calls_acc = {}
        text_acc = ""

        async for chunk in await litellm.acompletion(**kwargs):
            delta = chunk.choices[0].delta

            if delta.content:
                text_acc += delta.content
                yield delta.content

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": tc.id, "name": "", "args": ""}
                    if tc.function.name:
                        tool_calls_acc[idx]["name"] += tc.function.name
                    if tc.function.arguments:
                        tool_calls_acc[idx]["args"] += tc.function.arguments

        if not tool_calls_acc:
            break

        # Execute all tool calls, append results, loop
        messages.append({"role": "assistant", "content": text_acc or None,
                         "tool_calls": [
                             {"id": tc["id"], "type": "function",
                              "function": {"name": tc["name"], "arguments": tc["args"]}}
                             for tc in tool_calls_acc.values()
                         ]})

        for tc in tool_calls_acc.values():
            args = json.loads(tc["args"])
            result = _execute_tool(tc["name"], args, confirm_fn)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
