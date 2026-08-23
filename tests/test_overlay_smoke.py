"""Pure overlay tests; GTK integration is exercised by the Nix package check."""

from __future__ import annotations

from nixadmin.overlay import APPLICATION_ID, overlay_url
from nixadmin.web import page


def test_overlay_url_preserves_token_and_selects_compact_surface(tmp_path):
    published = tmp_path / "nixadmin-web.url"
    published.write_text("http://127.0.0.1:7677/?token=secret\n")
    assert overlay_url(published) == (
        "http://127.0.0.1:7677/?token=secret&surface=overlay"
    )


def test_overlay_url_is_absent_when_web_service_has_not_published(tmp_path):
    assert overlay_url(tmp_path / "missing") is None


def test_overlay_has_stable_desktop_identity():
    assert APPLICATION_ID == "io.github.hinstef.NixadminOverlay"


def test_web_assets_define_a_compact_overlay_composition():
    app = (page.asset("app.js") or ("", b""))[1].decode()
    styles = (page.asset("styles.css") or ("", b""))[1].decode()
    assert 'params.get("surface") === "overlay"' in app
    assert "body.overlay main > section:not(#invoke)" in styles
