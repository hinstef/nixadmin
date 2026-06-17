"""Daemon configuration — populated from the environment by the NixOS module.

The NixOS module renders ``services.nixadmin.*`` options into ``NIXADMIN_*``
environment variables; :meth:`Config.from_env` reads them. Defaults make a bare
``nixadmin-daemon`` runnable for local development without any env at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nixadmin.errors import ConfigError

Chain = Literal["local", "remote"]


def _default_socket() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = runtime or "/tmp"  # noqa: S108 — dev fallback only; service always has XDG_RUNTIME_DIR
    return str(Path(base) / "nixadmin.sock")


@dataclass(frozen=True, slots=True)
class Config:
    # identity / target
    flake_dir: str = ""
    hostname: str = ""

    # local chain (Ollama). Empty model = no local chain (remote-only machine).
    local_model: str = ""
    local_url: str = "http://localhost:11434"

    # remote chain (LiteLLM target: Hermes proxy, direct API, OpenRouter…)
    remote_model: str = "claude-sonnet-4-5"
    remote_base: str | None = None

    default_chain: Chain = "remote"
    history: str = "null"  # "null" | "sqlite" (future)
    socket_path: str = field(default_factory=_default_socket)

    log_format: Literal["json", "console"] = "json"
    log_level: str = "INFO"

    #: Env vars that indicate a usable remote credential (provider-agnostic).
    _REMOTE_KEYS = (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
        "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
    )

    @property
    def has_local(self) -> bool:
        """True when a local chain is configured (a local model is set)."""
        return bool(self.local_model)

    @property
    def remote_usable(self) -> bool:
        """True only when the remote chain can actually authenticate — a proxy/base
        URL (e.g. Hermes) is set, or a provider API key is in the environment.

        A bare model name is not enough: routing must not send work to a backend
        that will fail with an auth error.
        """
        if not self.remote_model:
            return False
        if self.remote_base:
            return True
        return any(k in os.environ for k in self._REMOTE_KEYS)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = env if env is not None else dict(os.environ)

        def get(key: str, default: str) -> str:
            return e.get(f"NIXADMIN_{key}", default)

        default_chain = get("CHAIN", "remote")
        if default_chain not in ("local", "remote"):
            raise ConfigError(f"NIXADMIN_CHAIN must be 'local' or 'remote', got {default_chain!r}")

        remote_base = e.get("NIXADMIN_REMOTE_BASE") or None

        return cls(
            flake_dir=get("FLAKE_DIR", ""),
            hostname=get("HOSTNAME", ""),
            local_model=get("LOCAL_MODEL", ""),
            local_url=get("LOCAL_URL", "http://localhost:11434"),
            remote_model=get("REMOTE_MODEL", "claude-sonnet-4-5"),
            remote_base=remote_base,
            default_chain=default_chain,  # type: ignore[arg-type]
            history=get("HISTORY", "null"),
            socket_path=get("SOCKET", _default_socket()),
            log_format=get("LOG_FORMAT", "json"),  # type: ignore[arg-type]
            log_level=get("LOG_LEVEL", "INFO"),
        )
