"""Central exception hierarchy.

Everything raised deliberately by nixadmin derives from :class:`NixadminError`, so
callers can catch the whole family with one ``except`` and the daemon can map any
of them to a protocol ``Error`` message. Subclasses stay coarse on purpose — add
more only when a caller needs to distinguish them.
"""

from __future__ import annotations

from typing import Literal


class NixadminError(Exception):
    """Base class for all nixadmin errors."""


class ConfigError(NixadminError):
    """Invalid or missing configuration."""


class ProtocolError(NixadminError):
    """A wire message was malformed, unknown, or missing required fields."""


class ModuleError(NixadminError):
    """A module manifest is invalid or failed to load."""


class BackendError(NixadminError):
    """An LLM backend (local Ollama or remote provider) failed."""


class SafetyError(NixadminError):
    """The safety gate refused an action."""


ExternalErrorKind = Literal[
    "timeout", "unavailable", "permission_denied", "command_failed",
]


class ExternalProcessError(NixadminError):
    """Stable failure from launching or waiting for an external command."""

    def __init__(
        self, kind: ExternalErrorKind, command: tuple[str, ...], *,
        exit_code: int | None = None, detail: str = "",
    ) -> None:
        self.kind = kind
        self.command = command
        self.exit_code = exit_code
        self.detail = detail
        label = command[0] if command else "command"
        if kind == "timeout":
            message = f"{label} timed out"
        elif kind == "unavailable":
            message = f"{label} is unavailable"
        elif kind == "permission_denied":
            message = f"permission denied running {label}"
        else:
            message = f"{label} failed with exit {exit_code}"
        if detail:
            message += f": {detail}"
        super().__init__(message)
