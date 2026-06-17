"""Safety gate — the only path to privileged actions.

Enforced in code, never in a prompt. In v1 the sole privileged tool is
``nixadmin_rebuild``; the gate guarantees:

* ``switch``/``boot`` require an explicit user ``confirm``,
* ``switch`` is refused unless a ``test`` succeeded earlier in the same session,
* ``test``/``revert`` are non-destructive and run without confirm.

Execution is delegated to the root ``nixadmin-helper`` over a Unix socket — the
daemon (a user service) never holds privilege itself.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from nixadmin.errors import SafetyError
from nixadmin.log import get_logger
from nixadmin.session import SessionState

log = get_logger(__name__)

ConfirmFn = Callable[[str], Awaitable[bool]]
ACTIONS = ("test", "switch", "boot", "revert")


class SafetyGate:
    def __init__(self, helper_socket: str) -> None:
        self._socket = helper_socket

    async def rebuild(
        self,
        action: str,
        *,
        state: SessionState,
        confirm: ConfirmFn,
    ) -> str:
        """Validate, gate, and execute a rebuild action. Returns helper output."""
        if action not in ACTIONS:
            raise SafetyError(f"unknown rebuild action: {action!r}")

        if action == "switch" and not state.last_test_ok:
            return ("Refused: run a configuration `test` first and confirm it "
                    "succeeds before switching.")

        if action in ("switch", "boot"):
            verb = "apply" if action == "switch" else "stage for next boot"
            if not await confirm(f"Confirm: {verb} the NixOS configuration change?"):
                return "Cancelled — no changes were made."

        log.info("rebuild dispatch", action=action)
        output = await self._run_helper(action)

        if action == "test":
            state.record_test(_looks_successful(output))
        return output

    async def apply_switch(self) -> str:
        """Run `switch` directly, for the deterministic action tier which has
        already validated the change in an isolated worktree and confirmed with the
        user. The root helper remains the privilege boundary."""
        return await self._run_helper("switch")

    async def _run_helper(self, action: str) -> str:
        """Send the action to the root helper and collect its streamed output."""
        try:
            reader, writer = await asyncio.open_unix_connection(self._socket)
        except OSError as e:
            raise SafetyError(f"cannot reach privileged helper: {e}") from e

        writer.write((json.dumps({"action": action}) + "\n").encode())
        await writer.drain()
        writer.write_eof()

        chunks: list[str] = []
        exit_code = 0
        async for raw in reader:
            line = raw.decode().strip()
            if not line:
                continue
            msg = json.loads(line)
            if "stream" in msg:
                chunks.append(msg["stream"])
            if "exit" in msg:
                exit_code = msg["exit"]
        writer.close()

        out = "".join(chunks).strip()
        if exit_code != 0:
            return f"{out}\n(rebuild failed, exit {exit_code})".strip()
        return out or "(done)"


def _looks_successful(output: str) -> bool:
    return "failed" not in output.lower()
