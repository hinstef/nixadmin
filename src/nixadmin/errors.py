"""Central exception hierarchy.

Everything raised deliberately by nixadmin derives from :class:`NixadminError`, so
callers can catch the whole family with one ``except`` and the daemon can map any
of them to a protocol ``Error`` message. Subclasses stay coarse on purpose — add
more only when a caller needs to distinguish them.
"""

from __future__ import annotations


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
