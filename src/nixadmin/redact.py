"""Redaction — strip sensitive detail before anything leaves the device.

Escalating to the frontier model is only acceptable if the person can trust what
crosses the line. So the local side redacts first, in two passes:

1. :func:`scrub` — a **pure, deterministic** pass over known secret *shapes*
   (API keys/tokens, emails, IP addresses, home paths). Reliable and testable; it
   never depends on a model getting it right.
2. a **local-model rewrite** (:func:`nixadmin.llm.local.redact_rewrite`) for
   *contextual* PII a regex can't catch ("my wife Anna's laptop"). Best-effort.

:func:`redact` runs both and returns the result the daemon shows the user verbatim
before the confirmed send — the privacy promise made legible, not just asserted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered, non-overlapping-enough patterns → the placeholder they collapse to.
# Deliberately conservative: better to over-redact a token than leak one.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Provider API keys / tokens with distinctive prefixes.
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[api-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws-key]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[slack-token]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}"), "Bearer [token]"),
    # Emails.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    # IPv4 (skip anything that isn't dotted-quad-ish is out of scope).
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    # Home paths → keep the shape, drop the username.
    (re.compile(r"/home/[^/\s]+"), "/home/[user]"),
    (re.compile(r"/Users/[^/\s]+"), "/Users/[user]"),
    # Long opaque hex/base64 blobs (generic secrets) — last, so labelled keys win.
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[secret]"),
]


@dataclass(frozen=True, slots=True)
class ScrubResult:
    text: str
    removed: list[str] = field(default_factory=list)


def scrub(text: str) -> ScrubResult:
    """Deterministically replace known secret shapes with typed placeholders.

    Returns the scrubbed text and the list of placeholder labels applied (so the
    caller can say *what kind* of thing was removed without echoing the secret).
    """
    removed: list[str] = []
    out = text
    for pattern, placeholder in _PATTERNS:
        out, n = pattern.subn(placeholder, out)
        if n:
            removed.extend([placeholder] * n)
    return ScrubResult(text=out, removed=removed)


@dataclass(frozen=True, slots=True)
class Redaction:
    original: str
    redacted: str
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.redacted.strip() != self.original.strip()


async def redact(text: str, *, model: str, url: str) -> Redaction:
    """Scrub deterministically, then let the local model catch contextual PII.

    The model pass is best-effort and can only ever see the *already-scrubbed*
    text, so a model failure degrades to the deterministic result — never to the
    raw input.
    """
    scrubbed = scrub(text)
    try:
        from nixadmin.llm import local as local_llm
        # redact_rewrite already returns non-empty (falls back to its input), so
        # this is the redacted text whether the model helped or not.
        rewritten = await local_llm.redact_rewrite(scrubbed.text, model=model, url=url)
    except Exception:  # noqa: BLE001 — privacy pass must never break the flow
        rewritten = scrubbed.text
    return Redaction(original=text, redacted=rewritten, removed=scrubbed.removed)


def scrub_only(text: str) -> Redaction:
    """Deterministic redaction with no model pass — for a machine with no local
    model to run the contextual rewrite. Still strips every known secret shape."""
    s = scrub(text)
    return Redaction(original=text, redacted=s.text, removed=s.removed)
