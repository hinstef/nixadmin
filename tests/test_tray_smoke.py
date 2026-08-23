"""Smoke tests for the tray — pure rendering/model logic and menu marshalling.

No live D-Bus or daemon: we exercise the icon maths, the health→colour mapping,
the menu model, and that DBusMenu builds a well-formed layout from a model.
"""

from __future__ import annotations

from nixadmin.tray import icons
from nixadmin.tray.main import INVOKE_ID, QUIT_ID, _menu_model, _tooltip
from nixadmin.tray.sni import DBusMenu, MenuEntry


def test_disc_argb_dimensions_and_coverage():
    size = 22
    data = icons.disc_argb(size, icons.HEALTHY)
    assert len(data) == size * size * 4
    # centre pixel is fully opaque and carries the colour
    ci = ((size // 2) * size + size // 2) * 4
    assert data[ci] == 255
    assert (data[ci + 1], data[ci + 2], data[ci + 3]) == icons.HEALTHY
    # a corner is outside the disc → transparent
    assert data[0] == 0


def test_pixmaps_one_entry_per_size():
    pm = icons.pixmaps(icons.ATTENTION)
    assert [entry[0] for entry in pm] == list(icons.SIZES)
    for size, _h, data in pm:
        assert len(data) == size * size * 4


def test_health_color_and_status():
    assert icons.health_color(True, 0) == icons.HEALTHY
    assert icons.health_color(True, 2) == icons.ATTENTION
    assert icons.health_color(False, 0) == icons.UNKNOWN
    assert icons.status_word(True, 0) == "Active"
    assert icons.status_word(True, 1) == "NeedsAttention"
    assert icons.status_word(False, 3) == "Active"  # visible, not Passive


def test_menu_model_states():
    healthy = _menu_model(True, [], overlay_available=True)
    assert healthy[0].id == INVOKE_ID and healthy[0].action == "overlay"
    status = next(entry for entry in healthy if entry.id == 1)
    assert "healthy" in status.label and status.enabled is False
    assert healthy[-1].id == QUIT_ID
    assert healthy[-1].label == "Close tray icon (nixadmin keeps running)"
    assert any(e.separator for e in healthy)

    failed = _menu_model(True, [
        {"unit": "cups.service", "scope": "system", "description": ""},
        {"unit": "x.service", "scope": "user", "description": ""},
    ])
    assert "2 service" in failed[0].label
    # each failed unit gets a Restart and an Explain row, carrying exact unit+scope
    restarts = [e for e in failed if e.action == "restart"]
    explains = [e for e in failed if e.action == "explain"]
    assert [(e.unit, e.scope) for e in restarts] == [
        ("cups.service", "system"), ("x.service", "user"),
    ]
    assert [(e.unit, e.scope) for e in explains] == [
        ("cups.service", "system"), ("x.service", "user"),
    ]
    assert all(e.label.startswith("Restart ") for e in restarts)
    assert all(e.label.startswith("Explain ") for e in explains)
    assert failed[-1].id == QUIT_ID

    down = _menu_model(False, None)
    assert "unreachable" in down[0].label


def test_tooltip_truncates_unit_list():
    units = [{"unit": f"u{i}.service", "scope": "system", "description": ""} for i in range(5)]
    tip = _tooltip(True, units)
    assert "+2 more" in tip
    assert _tooltip(True, []).endswith("healthy")
    assert "unreachable" in _tooltip(False, None)


def test_dbusmenu_build_layout_shape():
    entries = [MenuEntry(1, "hi"), MenuEntry(2, separator=True), MenuEntry(9, "Quit")]
    fired: list[MenuEntry] = []
    menu = DBusMenu(lambda: entries, fired.append)

    revision, root = menu.build_layout()
    assert isinstance(revision, int)
    root_id, _root_props, children = root
    assert root_id == 0
    assert len(children) == 3  # one variant per entry

    # a separator carries a "type" prop; a normal row carries label/enabled
    assert "type" in menu.entry_props(entries[1])
    assert "label" in menu.entry_props(entries[0])

    # firing a click routes the right entry to on_activate
    menu.fire(9)
    assert fired and fired[-1].id == 9
