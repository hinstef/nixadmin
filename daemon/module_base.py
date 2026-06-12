"""
Module interface — define this in any pip package under the entry point group
"nixadmin.modules" and the daemon will discover it automatically.

Example pyproject.toml:
  [project.entry-points."nixadmin.modules"]
  network = "nixadmin_network:MODULE"
"""

from dataclasses import dataclass, field
from typing import Callable, Literal


Routing = Literal["local", "remote", "auto"]
Severity = Literal["info", "warning", "error"]


@dataclass
class Fetcher:
    cmd: str                        # shell command to run
    timeout: int = 15               # seconds


@dataclass
class Monitor:
    name: str
    source: Literal["poll", "dbus", "journal"]
    severity: Severity = "warning"

    # poll-based
    cmd: str = ""                   # command whose output is checked
    interval: int = 60              # seconds between checks
    trigger: Callable = None        # fn(output: str) -> bool

    # dbus-based
    interface: str = ""
    signal: str = ""
    filter: Callable = None         # fn(*signal_args) -> bool


@dataclass
class Module:
    name: str
    description: str                # used by the classifier — be descriptive
    fetchers: list[Fetcher] = field(default_factory=list)
    monitors: list[Monitor] = field(default_factory=list)
    routing: Routing = "auto"       # force local/remote or let daemon decide


def load_modules() -> list[Module]:
    """Discover all installed nixadmin modules via entry points."""
    from importlib.metadata import entry_points
    modules = []
    for ep in entry_points(group="nixadmin.modules"):
        try:
            modules.append(ep.load())
        except Exception as e:
            print(f"[modules] failed to load {ep.name}: {e}")
    return modules
