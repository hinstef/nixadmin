"""Module registry — load built-in and third-party modules.

Built-ins are loaded directly and first; third-party modules are discovered via
the ``nixadmin.modules`` entry-point group. A module that fails to load, has the
wrong ``spec_version``, or collides on name is skipped with a warning — one bad
module never takes down the daemon.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from nixadmin.builtins import BUILTIN_MODULES
from nixadmin.log import get_logger
from nixadmin.sdk import SPEC_VERSION, Module

log = get_logger(__name__)

ENTRY_POINT_GROUP = "nixadmin.modules"


def load_modules() -> list[Module]:
    """Return built-in modules plus all valid discovered third-party modules."""
    modules: list[Module] = list(BUILTIN_MODULES)
    seen: dict[str, str] = {m.name: "builtin" for m in modules}

    for ep in entry_points(group=ENTRY_POINT_GROUP):
        loaded = _load_one(ep.name, ep)
        if loaded is None:
            continue
        if loaded.name in seen:
            log.warning(
                "module name collision — skipped",
                name=loaded.name, entry_point=ep.name, existing=seen[loaded.name],
            )
            continue
        seen[loaded.name] = ep.name
        modules.append(loaded)

    log.info("modules loaded", count=len(modules), names=[m.name for m in modules])
    return modules


def _load_one(ep_name: str, ep: object) -> Module | None:
    try:
        obj = ep.load()  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001 — third-party code; never crash the daemon
        log.warning("module failed to load", entry_point=ep_name, error=str(e))
        return None

    if not isinstance(obj, Module):
        log.warning(
            "entry point is not a Module manifest", entry_point=ep_name, got=type(obj).__name__
        )
        return None

    if obj.spec_version != SPEC_VERSION:
        log.warning(
            "module built for incompatible spec version — skipped",
            entry_point=ep_name, module=obj.name,
            module_spec=obj.spec_version, daemon_spec=SPEC_VERSION,
        )
        return None

    return obj
