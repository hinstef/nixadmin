"""Built-in modules — always loaded, before any third-party module.

Each submodule exposes a ``manifest``. They are collected here so the registry
can load them directly (built-ins are not discovered via entry points).
"""

from __future__ import annotations

from nixadmin.builtins import apps, disk, network, services
from nixadmin.sdk import Module

BUILTIN_MODULES: list[Module] = [
    apps.manifest,
    network.manifest,
    disk.manifest,
    services.manifest,
]
