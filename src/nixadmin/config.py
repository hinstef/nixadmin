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
from urllib.parse import urlsplit, urlunsplit

from nixadmin.errors import ConfigError

Chain = Literal["local", "remote"]


def _default_socket() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = runtime or "/tmp"  # noqa: S108 — dev fallback only; service always has XDG_RUNTIME_DIR
    return str(Path(base) / "nixadmin.sock")


def _default_state_dir() -> str:
    """Where the persistent event store lives, honouring the XDG base-dir spec."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return str(base / "nixadmin")


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
    # Persistent system-event timeline (observability substrate). On by default —
    # legibility is the point. "null" opts out. See nixadmin.store.
    events: str = "sqlite"  # "sqlite" | "null"
    event_retention_days: int = 90  # 0 disables age-based pruning
    state_dir: str = field(default_factory=lambda: _default_state_dir())
    socket_path: str = field(default_factory=_default_socket)

    # Autofix: silently restart failed units (act/ask matrix — see nixadmin.autofix).
    autofix: bool = True
    autofix_system: bool = True     # also auto-restart system units (via root helper)
    autofix_max_attempts: int = 1   # restarts per unit before we stop and inform

    log_format: Literal["json", "console"] = "json"
    log_level: str = "INFO"
    _remote_credential_configured: bool = field(default=False, repr=False)

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
        return self._remote_credential_configured or any(k in os.environ for k in self._REMOTE_KEYS)

    def effective_summary(self) -> dict[str, object]:
        """Effective diagnostics without credential values or URL secrets."""
        return {
            "flake_dir": self.flake_dir,
            "hostname": self.hostname,
            "local": {
                "configured": self.has_local, "model": self.local_model,
                "url": _safe_url(self.local_url),
            },
            "remote": {
                "configured": bool(self.remote_model), "model": self.remote_model,
                "base": _safe_url(self.remote_base) if self.remote_base else None,
                "credential_configured": self.remote_usable,
            },
            "default_chain": self.default_chain,
            "history": self.history,
            "events": self.events,
            "event_retention_days": self.event_retention_days,
            "state_dir": self.state_dir,
            "socket_path": self.socket_path,
            "autofix": {
                "enabled": self.autofix, "system": self.autofix_system,
                "max_attempts": self.autofix_max_attempts,
            },
            "logging": {"format": self.log_format, "level": self.log_level},
        }

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = env if env is not None else dict(os.environ)
        errors: list[str] = []

        def get(key: str, default: str) -> str:
            return e.get(f"NIXADMIN_{key}", default)

        def flag(key: str, default: str) -> bool:
            raw = get(key, default).strip().lower()
            if raw in ("1", "true", "yes", "on"):
                return True
            if raw in ("0", "false", "no", "off", ""):
                return False
            errors.append(f"NIXADMIN_{key} must be a boolean, got {raw!r}")
            return False

        default_chain = get("CHAIN", "remote")
        if default_chain not in ("local", "remote"):
            errors.append(f"NIXADMIN_CHAIN must be 'local' or 'remote', got {default_chain!r}")
            default_chain = "remote"

        remote_base = e.get("NIXADMIN_REMOTE_BASE") or None

        raw_attempts = get("AUTOFIX_MAX_ATTEMPTS", "1")
        try:
            # Clamp to >= 1: 0 would make the loop guard fire immediately and
            # silently disable every restart.
            max_attempts = max(1, int(raw_attempts))
        except ValueError:
            errors.append(
                f"NIXADMIN_AUTOFIX_MAX_ATTEMPTS must be an integer, got {raw_attempts!r}"
            )
            max_attempts = 1

        raw_retention = get("EVENT_RETENTION_DAYS", "90")
        try:
            retention_days = int(raw_retention)
        except ValueError:
            errors.append(
                "NIXADMIN_EVENT_RETENTION_DAYS must be a non-negative integer, "
                f"got {raw_retention!r}"
            )
            retention_days = 90
        if retention_days < 0:
            errors.append(
                "NIXADMIN_EVENT_RETENTION_DAYS must be a non-negative integer, "
                f"got {raw_retention!r}"
            )
            retention_days = 90

        history = get("HISTORY", "null")
        events = get("EVENTS", "sqlite")
        log_format = get("LOG_FORMAT", "json")
        log_level = get("LOG_LEVEL", "INFO").upper()
        local_url = get("LOCAL_URL", "http://localhost:11434")
        state_dir = get("STATE_DIR", _default_state_dir())
        socket_path = get("SOCKET", _default_socket())
        flake_dir = get("FLAKE_DIR", "")
        for key, value, choices in (
            ("HISTORY", history, ("null",)),
            ("EVENTS", events, ("sqlite", "null")),
            ("LOG_FORMAT", log_format, ("json", "console")),
            ("LOG_LEVEL", log_level, ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
        ):
            if value not in choices:
                errors.append(f"NIXADMIN_{key} must be one of {', '.join(choices)}, got {value!r}")
        for url_key, url_value in (("LOCAL_URL", local_url), ("REMOTE_BASE", remote_base)):
            if url_value and not _http_url(url_value):
                errors.append(
                    f"NIXADMIN_{url_key} must be an absolute http(s) URL, got {url_value!r}"
                )
        for path_key, path_value in (("STATE_DIR", state_dir), ("SOCKET", socket_path),
                                     ("FLAKE_DIR", flake_dir)):
            if path_value and not Path(path_value).is_absolute():
                errors.append(
                    f"NIXADMIN_{path_key} must be an absolute path, got {path_value!r}"
                )
        local_model = get("LOCAL_MODEL", "")
        if default_chain == "local" and not local_model:
            errors.append("NIXADMIN_LOCAL_MODEL is required when NIXADMIN_CHAIN='local'")
        autofix_enabled = flag("AUTOFIX", "1")
        autofix_system = flag("AUTOFIX_SYSTEM", "1")
        if errors:
            raise ConfigError("invalid configuration:\n- " + "\n- ".join(errors))

        return cls(
            flake_dir=flake_dir,
            hostname=get("HOSTNAME", ""),
            local_model=local_model,
            local_url=local_url,
            remote_model=get("REMOTE_MODEL", "claude-sonnet-4-5"),
            remote_base=remote_base,
            default_chain=default_chain,  # type: ignore[arg-type]
            history=history,
            events=events,
            event_retention_days=retention_days,
            state_dir=state_dir,
            autofix=autofix_enabled,
            autofix_system=autofix_system,
            autofix_max_attempts=max_attempts,
            socket_path=socket_path,
            log_format=log_format,  # type: ignore[arg-type]
            log_level=log_level,
            _remote_credential_configured=any(e.get(key) for key in cls._REMOTE_KEYS),
        )


def _http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _port = parsed.port  # access validates malformed ports
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "(invalid URL)"
