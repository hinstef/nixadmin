"""Smoke tests for the module-author SDK — proves a module can be declared."""

from __future__ import annotations

import pytest

from nixadmin.errors import ModuleError
from nixadmin.sdk import SPEC_VERSION, Fetcher, Module, Monitor


def test_declare_a_module():
    """The canonical thing a third-party author writes still type-checks/builds."""
    manifest = Module(
        spec_version=SPEC_VERSION,
        name="network",
        description="wifi, internet, connectivity",
        fetchers=[
            Fetcher(name="wifi", cmd="nmcli dev wifi", description="Wi-Fi status",
                    expose_as_tool=True),
        ],
        monitors=[
            Monitor(name="net-down", source="dbus",
                    interface="org.freedesktop.NetworkManager", signal="StateChanged"),
        ],
        routing="local",
    )
    assert manifest.name == "network"
    assert manifest.fetchers[0].name == "wifi"
    assert manifest.routing == "local"


def test_minimal_module_defaults():
    """A bare module has empty capability lists, not None."""
    m = Module(spec_version=SPEC_VERSION, name="x", description="x")
    assert m.fetchers == []
    assert m.monitors == []
    assert m.context_provider is None
    assert m.routing == "auto"


def test_tool_fetcher_requires_description():
    with pytest.raises(ModuleError, match="description"):
        Fetcher(name="ps", cmd="docker ps", expose_as_tool=True)


def test_duplicate_fetcher_names_rejected():
    with pytest.raises(ModuleError, match="duplicate fetcher names"):
        Module(spec_version=SPEC_VERSION, name="x", description="x",
               fetchers=[Fetcher(name="a", cmd="true"), Fetcher(name="a", cmd="false")])


def test_poll_monitor_needs_trigger():
    with pytest.raises(ModuleError, match="requires a trigger"):
        Monitor(name="m", source="poll", cmd="df /")


def test_dbus_monitor_rejects_cmd():
    with pytest.raises(ModuleError, match="must not set a cmd"):
        Monitor(name="m", source="dbus", interface="org.x", signal="S", cmd="oops")
