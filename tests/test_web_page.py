"""Web presentation assets and synchronous daemon client behavior."""

from __future__ import annotations

from nixadmin.web import page, security
from nixadmin.web.dclient import Daemon


def test_page_embeds_token_and_no_placeholder_leaks():
    tok = security.new_token()
    html = page.render(tok)
    assert tok in html
    assert "__NIXADMIN_TOKEN__" not in html
    assert "<title>nixadmin</title>" in html


def test_page_has_hub_sections_and_timeline_wiring():
    """The document stays structural; behavior lives in packaged ES modules."""
    html = page.render(security.new_token())
    app = (page.asset("app.js") or ("", b""))[1].decode()
    timeline = (page.asset("timeline.js") or ("", b""))[1].decode()
    assert ">Now<" in html and ">Activity<" in html
    assert "/api/timeline" in timeline
    assert 'params.get("explain")' in app
    assert 'type="module"' in html


def test_daemon_client_graceful_when_socket_absent(tmp_path):
    d = Daemon(str(tmp_path / "nope.sock"))
    assert d.list_failures() is None       # unreachable → None, not a crash
    assert d.journal("x.service", "user") is None
    assert d.timeline() == ([], None)      # unreachable → empty, not a crash


def test_page_has_invoke_bar():
    """The hub carries the invoke bar and its streaming client."""
    html = page.render(security.new_token())
    assert 'id="ask"' in html and "What would you like?" in html
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert "/api/stream" in (page.asset("api.js") or ("", b""))[1].decode()
    assert "EventSource" in operation


def test_invoke_bar_comes_before_the_status_sections():
    """Single pane of glass: the prompt is the first thing on the page, not a
    control buried under the health readout."""
    html = page.render(security.new_token())
    body = html[html.index("<main>"):]
    assert body.index('id="ask"') < body.index('id="kept-sec"') < body.index(">Now<")


def test_common_action_chips_are_seeded_prompts_only():
    """A chip must not be a second path to the daemon. It either runs a query the
    user could have typed, or fills the box — never its own API call."""
    app = (page.asset("app.js") or ("", b""))[1].decode()
    assert 'fill: "install "' in app and 'run: "is anything broken?"' in app


def test_replies_stack_and_stay_capped():
    """Cards accumulate (a slow install stays visible) but the stack is bounded —
    a working record, not the chat transcript docs/ux.md rules out."""
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert "MAX_CARDS = 6" in operation and "container.prepend(card)" in operation
    assert "trimCards" in operation


def test_running_query_can_be_stopped():
    """The cancel endpoint is reachable from the UI, not just from the wire."""
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert 'control("/api/cancel"' in operation


def test_timeline_is_five_rows_cursor_paginated():
    timeline = (page.asset("timeline.js") or ("", b""))[1].decode()
    html = page.render(security.new_token())
    assert "PAGE_SIZE = 5" in timeline
    assert 'query.set("before"' in timeline
    assert 'id="older"' in html and 'id="newer"' in html
    assert "New activity" in html


def test_operations_use_progressive_disclosure_and_phases():
    operation = (page.asset("operation.js") or ("", b""))[1].decode()
    assert 'el("details")' in operation and '"Details"' in operation
    assert "Waiting for approval" in operation
    assert "Applying the change" in operation
    assert "Verifying the result" in operation
    assert "startTask" in operation
    assert "do not duplicate streamed answer text" in operation


def test_activity_uses_human_summaries_and_groups_service_episodes():
    timeline = (page.asset("timeline.js") or ("", b""))[1].decode()
    assert "groupEpisodes" in timeline and "LIFECYCLE_KINDS" in timeline
    assert "needs attention" in timeline
    assert "restarted it and verified" in timeline
    assert 'replaceAll("_", " ")' not in timeline
    # Raw kinds and metadata remain available, but only in diagnostic evidence.
    assert "rawEvidence" in timeline and "event.kind" in timeline


def test_failed_unit_controls_share_operation_cards():
    app = (page.asset("app.js") or ("", b""))[1].decode()
    assert "startTask(`Restart" in app
    assert "startTask(`Explain" in app
    assert "startTask(`Journal" in app


def test_assets_reject_traversal_and_unknown_types():
    assert page.asset("styles.css") is not None
    assert page.asset("../config.py") is None
    assert page.asset("page.html") is None

