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
import contextlib
import json
import os
from collections.abc import Awaitable, Callable

from nixadmin.errors import SafetyError
from nixadmin.log import get_logger
from nixadmin.session import SessionState

log = get_logger(__name__)

ConfirmFn = Callable[[str], Awaitable[bool]]
ACTIONS = ("test", "switch", "boot", "revert")
HELPER_TIMEOUT_S = float(os.environ.get("NIXADMIN_HELPER_TIMEOUT", "3700"))


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
        output, code = await self._run_helper({"action": action})

        # The test→switch invariant gates on the helper's real exit code, never on
        # whether the word "failed" happens to appear in the build output.
        if action == "test":
            state.record_test(code == 0)
        if code != 0:
            return f"{output}\n(rebuild failed, exit {code})".strip()
        return output or "(done)"

    async def apply_switch(self) -> str:
        """Run `switch` directly, for the deterministic action tier which has
        already validated the change in an isolated worktree and confirmed with the
        user. The root helper remains the privilege boundary.

        Raises :class:`SafetyError` on a nonzero rebuild, so the action tier can
        revert its config edit."""
        output, code = await self._run_helper({"action": "switch"})
        if code != 0:
            raise SafetyError(f"rebuild failed (exit {code}):\n{output}".strip())
        return output or "(done)"

    async def apply_revert(self) -> str:
        """Roll the system back to the previous generation (`switch --rollback`),
        for the action tier's recovery when a switch fails mid-activation. Raises
        :class:`SafetyError` if the rollback itself fails."""
        output, code = await self._run_helper({"action": "revert"})
        if code != 0:
            raise SafetyError(f"rollback failed (exit {code}):\n{output}".strip())
        return output or "(done)"

    async def apply_restart(self, unit: str) -> str:
        """Restart a *system* unit via the root helper (the tray's "fix it"). Raises
        :class:`SafetyError` on failure so the remediation tier reports honestly."""
        output, code = await self._run_helper({"action": "restart", "unit": unit})
        if code != 0:
            raise SafetyError(f"restart {unit} failed (exit {code}):\n{output}".strip())
        return output or "(done)"

    async def _run_helper(self, request: dict[str, str]) -> tuple[str, int]:
        """Send a request to the root helper, collect its streamed output, and
        return ``(output, exit_code)``."""
        chunks: list[str] = []
        exit_code: int | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(HELPER_TIMEOUT_S):
                reader, writer = await asyncio.open_unix_connection(self._socket)
                writer.write((json.dumps(request) + "\n").encode())
                await writer.drain()
                writer.write_eof()

                async for raw in reader:
                    line = raw.decode().strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise SafetyError("privileged helper returned a malformed response")
                    if "error" in msg:
                        raise SafetyError(f"privileged helper refused request: {msg['error']}")
                    stream = msg.get("stream")
                    if stream is not None:
                        if not isinstance(stream, str):
                            raise SafetyError("privileged helper returned a malformed stream")
                        chunks.append(stream)
                    if "exit" in msg:
                        value = msg["exit"]
                        if isinstance(value, bool) or not isinstance(value, int):
                            raise SafetyError("privileged helper returned an invalid exit code")
                        exit_code = value
        except TimeoutError as e:
            raise SafetyError(
                f"privileged helper timed out after {HELPER_TIMEOUT_S:g}s"
            ) from e
        except OSError as e:
            raise SafetyError(f"cannot reach privileged helper: {e}") from e
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SafetyError("privileged helper returned malformed JSON") from e
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()

        if exit_code is None:
            raise SafetyError("privileged helper disconnected without a final exit status")
        return "".join(chunks).strip(), exit_code
