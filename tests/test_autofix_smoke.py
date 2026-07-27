"""Smoke tests for the autofix policy — the pure decision, exhaustively.

The engine that gathers state and restarts lives in the daemon (covered in
test_server_smoke); here we pin the policy: when to act, when to only inform, and
when to stay out of the way.
"""

from __future__ import annotations

import pytest

from nixadmin.autofix import AutofixConfig, decide
from nixadmin.config import Config
from nixadmin.errors import ConfigError


def test_fresh_failure_restarts():
    cfg = AutofixConfig()
    assert decide(scope="user", prior_attempts=0, cfg=cfg) == "restart"
    assert decide(scope="system", prior_attempts=0, cfg=cfg) == "restart"  # system on by default


def test_loop_guard_informs_after_budget():
    cfg = AutofixConfig(max_attempts=1)
    assert decide(scope="user", prior_attempts=1, cfg=cfg) == "inform"
    assert decide(scope="user", prior_attempts=5, cfg=cfg) == "inform"
    cfg2 = AutofixConfig(max_attempts=3)
    assert decide(scope="user", prior_attempts=2, cfg=cfg2) == "restart"
    assert decide(scope="user", prior_attempts=3, cfg=cfg2) == "inform"


def test_system_autonomy_can_be_disabled():
    cfg = AutofixConfig(system=False)
    assert decide(scope="system", prior_attempts=0, cfg=cfg) == "inform"  # surface, don't act
    assert decide(scope="user", prior_attempts=0, cfg=cfg) == "restart"   # user still auto


def test_disabled_skips_everything():
    cfg = AutofixConfig(enable=False)
    assert decide(scope="user", prior_attempts=0, cfg=cfg) == "skip"
    assert decide(scope="system", prior_attempts=9, cfg=cfg) == "skip"


def test_config_max_attempts_clamped_to_at_least_one():
    # 0 would make the loop guard fire immediately and silently disable restarts.
    assert Config.from_env({"NIXADMIN_AUTOFIX_MAX_ATTEMPTS": "0"}).autofix_max_attempts == 1
    assert Config.from_env({"NIXADMIN_AUTOFIX_MAX_ATTEMPTS": "3"}).autofix_max_attempts == 3


def test_config_max_attempts_non_numeric_raises_config_error():
    with pytest.raises(ConfigError):
        Config.from_env({"NIXADMIN_AUTOFIX_MAX_ATTEMPTS": "two"})


def test_config_autofix_flags_parse_falsey_words():
    for word in ("0", "false", "False", "no", "off", ""):
        assert Config.from_env({"NIXADMIN_AUTOFIX_SYSTEM": word}).autofix_system is False
    assert Config.from_env({"NIXADMIN_AUTOFIX_SYSTEM": "1"}).autofix_system is True
