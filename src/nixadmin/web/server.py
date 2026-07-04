"""The localhost HTTP server — loopback-bound, token + Host + Origin gated.

Threaded stdlib server (no framework, no new deps). Every handler runs the same
guard before doing anything; mutations additionally demand a matching Origin.
Data comes from the daemon over its Unix socket — this process touches nothing
privileged itself.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

from nixadmin.log import get_logger
from nixadmin.web import page, security
from nixadmin.web.dclient import Daemon

log = get_logger(__name__)

DEFAULT_PORT = 7677
# CSP: no external anything; inline style/script (the page is self-contained);
# fetch only same-origin. Belt-and-braces on top of the token + Origin checks.
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'"
)


def socket_path() -> str:
    env = os.environ.get("NIXADMIN_SOCKET")
    if env:
        return env
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")  # noqa: S108
    return str(Path(runtime) / "nixadmin.sock")


def url_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")  # noqa: S108
    return Path(runtime) / "nixadmin-web.url"


class Handler(BaseHTTPRequestHandler):
    server_version = "nixadmin-web"

    def log_message(self, format: str, *args: object) -> None:
        return  # don't spew request lines to stderr/journal

    @property
    def app(self) -> _WebServer:
        return cast("_WebServer", self.server)

    # --- guards ----------------------------------------------------------- #
    def _token(self, qs: dict[str, list[str]]) -> str | None:
        header = self.headers.get("X-Nixadmin-Token")
        if header:
            return header
        vals = qs.get("token")
        return vals[0] if vals else None

    def _guard(self, qs: dict[str, list[str]], *, mutation: bool) -> bool:
        port = self.app.port
        if not security.host_ok(self.headers.get("Host"), port):
            self._json(403, {"error": "bad host"})
            return False
        if not security.origin_ok(self.headers.get("Origin"), port, require=mutation):
            self._json(403, {"error": "bad origin"})
            return False
        if not security.token_ok(self._token(qs), self.app.token):
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    # --- writers ---------------------------------------------------------- #
    def _write(
        self, code: int, ctype: str, body: bytes, extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: object) -> None:
        self._write(code, "application/json", json.dumps(obj).encode())

    # --- routes ----------------------------------------------------------- #
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/":
            if not self._guard(qs, mutation=False):
                return
            self._write(200, "text/html; charset=utf-8",
                        page.render(self.app.token).encode(), {"Content-Security-Policy": _CSP})
        elif parsed.path == "/api/state":
            if not self._guard(qs, mutation=False):
                return
            units = self.app.dclient.list_failures()
            self._json(200, {"connected": units is not None, "units": units or []})
        elif parsed.path == "/api/journal":
            if not self._guard(qs, mutation=False):
                return
            unit = (qs.get("unit") or [""])[0]
            scope = (qs.get("scope") or ["system"])[0]
            self._json(200, {"text": self.app.dclient.journal(unit, scope) or ""})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/restart", "/api/explain"):
            self._json(404, {"error": "not found"})
            return
        if not self._guard(parse_qs(parsed.query), mutation=True):
            return
        body = self._body()
        unit = str(body.get("unit", ""))
        scope = str(body.get("scope", "system"))
        if parsed.path == "/api/restart":
            self._json(200, {"result": self.app.dclient.restart(unit, scope)})
        else:
            self._json(200, {"text": self.app.dclient.explain(unit, scope)})

    def _body(self) -> dict[str, object]:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n) if n > 0 else b"{}"
            parsed = json.loads(raw or b"{}")
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}


class _WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], token: str, dclient: Daemon) -> None:
        super().__init__(addr, Handler)
        self.token = token
        self.dclient = dclient
        self.port = self.server_address[1]


def _write_url_file(url: str) -> None:
    path = url_file()
    path.write_text(url + "\n")
    path.chmod(0o600)


def main() -> None:
    token = security.new_token()
    port = int(os.environ.get("NIXADMIN_WEB_PORT", str(DEFAULT_PORT)))
    server = _WebServer(("127.0.0.1", port), token, Daemon(socket_path()))
    _write_url_file(f"http://127.0.0.1:{server.port}/?token={token}")
    log.info("web listening", addr=f"127.0.0.1:{server.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        url_file().unlink(missing_ok=True)


if __name__ == "__main__":
    main()
