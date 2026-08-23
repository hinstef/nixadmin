"""Smoke tests for config + the module registry / built-ins."""

from __future__ import annotations

import pytest

from nixadmin.config import Config
from nixadmin.errors import ConfigError
from nixadmin.registry import load_modules
from nixadmin.sdk import Module


def test_builtins_load_and_are_valid():
    mods = load_modules()
    names = {m.name for m in mods}
    assert {"apps", "network", "disk", "services"} <= names
    assert all(isinstance(m, Module) for m in mods)


def test_builtin_monitors_are_well_formed():
    # If any builtin monitor had bad source/field combos, __post_init__ would have
    # raised at import — reaching here means they validate. Spot-check one.
    services = next(m for m in load_modules() if m.name == "services")
    mon = services.monitors[0]
    assert mon.source == "dbus"
    assert mon.signal == "JobRemoved"


def test_config_defaults_and_has_local():
    cfg = Config.from_env({})
    assert cfg.default_chain == "remote"
    assert cfg.has_local is False  # no local model by default
    assert cfg.socket_path.endswith("nixadmin.sock")


def test_config_from_env_reads_local_model():
    cfg = Config.from_env({"NIXADMIN_LOCAL_MODEL": "qwen2.5:3b", "NIXADMIN_CHAIN": "local"})
    assert cfg.has_local is True
    assert cfg.local_model == "qwen2.5:3b"
    assert cfg.default_chain == "local"


def test_remote_usable_requires_credentials(monkeypatch):
    for k in Config._REMOTE_KEYS:
        monkeypatch.delenv(k, raising=False)
    # model set but no key, no base → not usable
    assert Config(remote_model="claude-sonnet-4-5").remote_usable is False
    # a base URL (e.g. Hermes proxy) makes it usable
    assert Config(remote_model="x", remote_base="http://localhost:4000").remote_usable is True
    # an API key in the env makes it usable
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert Config(remote_model="claude-sonnet-4-5").remote_usable is True


def test_config_reports_independent_errors_together():
    with pytest.raises(ConfigError) as caught:
        Config.from_env({
            "NIXADMIN_CHAIN": "nearby",
            "NIXADMIN_EVENTS": "memory",
            "NIXADMIN_LOG_FORMAT": "pretty",
            "NIXADMIN_LOCAL_URL": "localhost:11434",
            "NIXADMIN_SOCKET": "relative.sock",
            "NIXADMIN_AUTOFIX": "sometimes",
        })
    message = str(caught.value)
    for key in ("CHAIN", "EVENTS", "LOG_FORMAT", "LOCAL_URL", "SOCKET", "AUTOFIX"):
        assert f"NIXADMIN_{key}" in message


def test_effective_config_summary_redacts_url_credentials_and_keys():
    cfg = Config.from_env({
        "NIXADMIN_REMOTE_BASE": "https://alice:secret@example.test/v1?token=hidden",
        "ANTHROPIC_API_KEY": "sk-very-secret",
    })
    summary = cfg.effective_summary()
    rendered = repr(summary)
    assert summary["remote"] == {
        "configured": True,
        "model": "claude-sonnet-4-5",
        "base": "https://example.test/v1",
        "credential_configured": True,
    }
    assert "secret" not in rendered and "sk-very-secret" not in rendered
