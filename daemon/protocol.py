"""
nixadmin socket protocol — newline-delimited JSON.

Client → Daemon:
  {"type": "query",  "id": "abc", "text": "is my wifi working?"}
  {"type": "cancel", "id": "abc"}
  {"type": "respond","id": "abc", "value": true}   # answer a confirm/input request

Daemon → Client (streaming):
  {"type": "delta",   "id": "abc", "text": "Yes, your WiFi is connected."}
  {"type": "done",    "id": "abc"}
  {"type": "error",   "id": "abc", "text": "..."}
  {"type": "confirm", "id": "abc", "text": "Install firefox? This will rebuild."}
  {"type": "input",   "id": "abc", "prompt": "Enter package name:"}
  {"type": "event",   "source": "monitor.systemd", "severity": "warning",
                      "text": "nginx stopped — port 80 already in use"}
"""

from dataclasses import dataclass, asdict
from typing import Literal
import json


Severity = Literal["info", "warning", "error"]


@dataclass
class QueryMsg:
    id: str
    text: str
    type: str = "query"


@dataclass
class DeltaMsg:
    id: str
    text: str
    type: str = "delta"


@dataclass
class DoneMsg:
    id: str
    type: str = "done"


@dataclass
class ErrorMsg:
    id: str
    text: str
    type: str = "error"


@dataclass
class ConfirmMsg:
    id: str
    text: str
    type: str = "confirm"


@dataclass
class EventMsg:
    source: str
    severity: Severity
    text: str
    type: str = "event"


def encode(msg) -> str:
    return json.dumps(asdict(msg)) + "\n"


def decode(line: str) -> dict:
    return json.loads(line)
