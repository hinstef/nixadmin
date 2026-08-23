"""Packaged assets for the local web hub.

The page is deliberately split into ordinary HTML, CSS, and ES modules. Python
only injects the per-process bearer token into the root document; presentation
state and rendering stay in the frontend files where they can evolve without
turning this module into a generated-looking string literal.
"""

from __future__ import annotations

from importlib.resources import files

_ASSETS = files("nixadmin.web.assets")
_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def render(token: str) -> str:
    """Return the root document with its short-lived session token embedded."""
    return _ASSETS.joinpath("page.html").read_text().replace("__NIXADMIN_TOKEN__", token)


def asset(name: str) -> tuple[str, bytes] | None:
    """Return a known static asset, rejecting traversal and unknown file types."""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    path = _ASSETS.joinpath(name)
    ctype = next((value for ext, value in _CONTENT_TYPES.items() if name.endswith(ext)), None)
    if ctype is None or not path.is_file():
        return None
    return ctype, path.read_bytes()
