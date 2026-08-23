"""Spotlight-style COSMIC/Wayland wrapper around the compact web surface.

The application is intentionally a tiny native shell: GTK/layer-shell owns
placement, focus and dismissal; WebKit renders the same token-gated UI as the
browser hub. It is normally started by systemd with ``--gapplication-service``
and held warm. Running ``nixadmin-overlay`` again activates that single resident
instance rather than creating another window.

GTK is imported lazily so the daemon, CLI and tests remain usable without any
desktop libraries in their Python environment.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nixadmin.web.server import url_file

APPLICATION_ID = "io.github.hinstef.NixadminOverlay"
OVERLAY_WIDTH = 700
OVERLAY_HEIGHT = 520
TOP_MARGIN = 96


def overlay_url(path: Path | None = None) -> str | None:
    """Read the web service's private URL and select its compact composition."""
    try:
        value = (path or url_file()).read_text().strip()
    except OSError:
        return None
    if not value:
        return None
    parsed = urlsplit(value)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["surface"] = "overlay"
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _desktop() -> tuple[Any, Any, Any, Any, Any]:
    """Load optional GI bindings only for the overlay entry point."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("WebKit", "6.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell, WebKit

    return Gdk, GLib, Gtk, Gtk4LayerShell, WebKit


def _application_class() -> type:
    Gdk, GLib, Gtk, LayerShell, WebKit = _desktop()

    class OverlayApplication(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(application_id=APPLICATION_ID)
            self.window: Any | None = None
            self.webview: Any | None = None

        def do_startup(self) -> None:
            Gtk.Application.do_startup(self)
            self.hold()  # stay warm while the layer surface is hidden

        def do_activate(self) -> None:
            if self.window is None:
                self._create_window()
            if self.window.get_visible():
                self._hide()
            else:
                self._show()

        def _create_window(self) -> None:
            window = Gtk.ApplicationWindow(application=self)
            window.set_title("nixadmin")
            window.set_decorated(False)
            window.set_resizable(False)
            window.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)

            LayerShell.init_for_window(window)
            LayerShell.set_namespace(window, "nixadmin-overlay")
            LayerShell.set_layer(window, LayerShell.Layer.OVERLAY)
            LayerShell.set_anchor(window, LayerShell.Edge.TOP, True)
            LayerShell.set_margin(window, LayerShell.Edge.TOP, TOP_MARGIN)
            LayerShell.set_exclusive_zone(window, 0)

            settings = WebKit.Settings()
            settings.set_enable_developer_extras(False)
            settings.set_enable_html5_database(False)
            settings.set_enable_html5_local_storage(False)
            webview = WebKit.WebView(settings=settings)
            webview.set_background_color(Gdk.RGBA(0.07, 0.08, 0.10, 1.0))
            window.set_child(webview)

            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._key_pressed)
            window.add_controller(keys)
            window.connect("notify::is-active", self._active_changed)

            self.window = window
            self.webview = webview

        def _show(self) -> None:
            target = overlay_url()
            if target is None:
                self.webview.load_html(
                    "<body style='background:#121419;color:#e6e8ee;font:16px system-ui;"
                    "padding:28px'>The nixadmin web service is not ready yet.</body>",
                    None,
                )
            elif self.webview.get_uri() != target:
                self.webview.load_uri(target)
            LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.EXCLUSIVE)
            self.window.present()

        def _hide(self) -> None:
            LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.NONE)
            self.window.set_visible(False)

        def _key_pressed(self, _controller: Any, key: int, _code: int, _state: Any) -> bool:
            if key == Gdk.KEY_Escape:
                self._hide()
                return True
            return False

        def _active_changed(self, window: Any, _spec: Any) -> None:
            # Activation briefly reports false while the compositor grants focus;
            # defer the check so we only hide on a real focus loss.
            if window.get_visible() and not window.is_active():
                GLib.timeout_add(150, self._hide_if_inactive)

        def _hide_if_inactive(self) -> bool:
            if self.window.get_visible() and not self.window.is_active():
                self._hide()
            return GLib.SOURCE_REMOVE

    return OverlayApplication


def main() -> None:
    app = _application_class()()
    raise SystemExit(app.run(sys.argv))


if __name__ == "__main__":
    main()
