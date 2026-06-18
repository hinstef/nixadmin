"""Smoke tests for the nixadmin-extras module package.

Validates the reference third-party modules load, are well-formed (their
__post_init__ validation passed at import), and don't collide with built-ins.
End-to-end entry-point *discovery* is exercised by the deployed daemon (the
modules only register once the package is installed in the daemon's env).
"""

from __future__ import annotations

import nixadmin_extras.bluetooth as bluetooth
import nixadmin_extras.health as health
import nixadmin_extras.performance as performance
import nixadmin_extras.power as power
import nixadmin_extras.security as security
import nixadmin_extras.system as system
import nixadmin_extras.updates as updates

from nixadmin.registry import load_modules
from nixadmin.sdk import Module

EXTRAS = [system, power, performance, bluetooth, updates, security, health]


def test_all_extras_are_valid_modules():
    for mod in EXTRAS:
        assert isinstance(mod.manifest, Module)
        assert mod.manifest.fetchers, f"{mod.manifest.name} has no fetchers"


def test_extra_names_are_unique_and_distinct_from_builtins():
    builtin_names = {m.name for m in load_modules()}
    extra_names = [m.manifest.name for m in EXTRAS]
    assert len(extra_names) == len(set(extra_names)), "duplicate extra module names"
    assert builtin_names.isdisjoint(extra_names), "extra collides with a built-in name"


def test_security_is_privacy_pinned():
    assert security.manifest.routing == "local"


def test_power_has_low_battery_monitor():
    mon = power.manifest.monitors[0]
    assert mon.source == "poll"
    assert mon.trigger("5") is True
    assert mon.trigger("80") is False


def test_exposed_tools_have_descriptions():
    for mod in EXTRAS:
        for f in mod.manifest.fetchers:
            if f.expose_as_tool:
                assert f.description, f"{mod.manifest.name}.{f.name} exposed without description"
