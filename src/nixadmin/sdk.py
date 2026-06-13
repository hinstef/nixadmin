"""Module-author API — the public surface for writing nixadmin modules.

A *module* teaches nixadmin about one domain (network, disk, docker, …). It is a
plain Python object you assign to a top-level ``manifest`` and register via an
entry point (lowercase, like Flask's ``app`` — distinct from the ``Module`` class):

    # pyproject.toml of your module package
    [project.entry-points."nixadmin.modules"]
    docker = "nixadmin_docker:manifest"

    # nixadmin_docker/__init__.py
    from nixadmin.sdk import Module, Fetcher, SPEC_VERSION

    manifest = Module(
        spec_version=SPEC_VERSION,
        name="docker",
        description="containers, images, docker, compose, running services",
        fetchers=[
            Fetcher(name="ps", cmd="docker ps", description="Running containers"),
        ],
    )

This module is **stdlib-only on purpose**: writing a module must not require
installing litellm, dbus, or anything the daemon uses internally. The daemon
imports your ``MODULE`` and drives it; you only describe capabilities here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

#: ABI version of this module interface. Bump when the dataclasses below change
#: shape in a backward-incompatible way. The daemon skips modules whose
#: ``spec_version`` does not match, with a warning instead of a crash.
SPEC_VERSION = 1

Severity = Literal["info", "warning", "error"]


@dataclass
class Fetcher:
    """A fixed, read-only command that produces system data.

    The command is *fixed at definition time*; the model never supplies or
    modifies it. On the local chain a matched fetcher's output is injected as
    context. On the remote chain, a fetcher with ``expose_as_tool=True`` is
    offered to the agent as a zero-argument tool named ``<module>_<name>``.
    """

    name: str
    """Stable identifier, unique within the module. Becomes the tool name suffix."""

    cmd: str
    """Shell command, run verbatim. Never interpolated with model output."""

    description: str = ""
    """Human/LLM-facing explanation. Required (non-empty) if ``expose_as_tool``.
    This is what the remote model reads to decide whether to call the tool — never
    the raw ``cmd``."""

    timeout: int = 15
    """Seconds before the command is killed."""

    expose_as_tool: bool = False
    """Offer this fetcher to the remote agent as a callable tool."""


@dataclass
class Monitor:
    """A reactive watch that emits an event when something noteworthy happens.

    Two sources:

    * ``poll`` — run ``cmd`` every ``interval`` seconds, fire when ``trigger``
      returns True for its output.
    * ``dbus`` — subscribe to ``signal`` on ``interface``; fire when ``filter``
      (if given) returns True for the signal arguments.
    """

    name: str
    source: Literal["poll", "dbus"]
    severity: Severity = "warning"

    # poll source
    cmd: str = ""
    interval: int = 60
    """Seconds between polls. The daemon clamps this to a floor (≥10s)."""
    trigger: Callable[[str], bool] | None = None
    """``fn(output) -> bool``. Return True to fire an event."""

    # dbus source
    bus: Literal["system", "session"] = "system"
    interface: str = ""
    signal: str = ""
    filter: Callable[..., bool] | None = None
    """``fn(*signal_args) -> bool``. Return True to fire an event."""


@dataclass
class ContextProvider:
    """Contributes text to the *remote* chain's system prompt.

    Called lazily on first use, cached, and re-fetched every ``refresh_interval``
    seconds (None = once per daemon lifetime). The local chain never uses context
    providers — it always runs on the minimal hardcoded prompt.
    """

    name: str
    get: Callable[[], Awaitable[str]]
    """Async producer of the context text."""
    refresh_interval: int | None = None


@dataclass
class Module:
    """A domain capability bundle. Assign one to a top-level ``MODULE``."""

    spec_version: int
    """Set to :data:`SPEC_VERSION`. Lets the daemon reject incompatible modules."""

    name: str
    """Unique module name, e.g. ``"network"``. Used as the tool-name prefix."""

    description: str
    """Classifier-facing keywords/phrases. Be descriptive — this is how the local
    model decides the module is relevant to a query."""

    fetchers: list[Fetcher] = field(default_factory=list)
    monitors: list[Monitor] = field(default_factory=list)
    context_provider: ContextProvider | None = None

    routing: Literal["local", "remote", "auto"] = "auto"
    """Routing hint. ``local`` marks the domain privacy-sensitive (keep on device);
    ``remote`` marks it as needing the capable model; ``auto`` defers to the daemon
    default. Collisions resolve ``local > auto > remote``."""
