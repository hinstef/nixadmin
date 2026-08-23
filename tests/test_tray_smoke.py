"""Smoke tests for the tray — pure rendering/model logic and menu marshalling.

No live D-Bus or daemon: we exercise the icon maths, the health→colour mapping,
the menu model, and that DBusMenu builds a well-formed layout from a model.
"""

from __future__ import annotations

import asyncio

import pytest

from nixadmin import protocol as wire
from nixadmin.tray import icons
from nixadmin.tray.client import DaemonClient
from nixadmin.tray.main import INVOKE_ID, QUIT_ID, _menu_model, _tooltip
from nixadmin.tray.sni import DBusMenu, MenuEntry


async def test_tray_connects_only_after_valid_hello(tmp_path, monkeypatch):
    path = str(tmp_path / "daemon.sock")
    release = asyncio.Event()

    async def peer(_reader, writer):
        await release.wait()
        writer.write(wire.encode(wire.Hello(
            chains=[], ready={}, default_chain="remote", modules=[])).encode())
        await writer.drain()
        await _reader.read()
        writer.close()

    server = await asyncio.start_unix_server(peer, path=path)
    states: list[bool] = []
    connected = asyncio.Event()

    def on_state(state: bool) -> None:
        states.append(state)
        if state:
            connected.set()

    client = DaemonClient(path, on_state=on_state)
    task = asyncio.create_task(client.run())
    try:
        await asyncio.sleep(0.03)
        assert True not in states
        release.set()
        async with asyncio.timeout(1):
            await connected.wait()
        assert states == [True]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("reply", ["incompatible", "not-hello", "silent"])
async def test_tray_rejects_invalid_or_missing_hello(tmp_path, monkeypatch, reply):
    path = str(tmp_path / "daemon.sock")
    monkeypatch.setattr("nixadmin.tray.client.HANDSHAKE_TIMEOUT_S", 0.02)
    monkeypatch.setattr("nixadmin.tray.client.RECONNECT_DELAY_S", 1.0)

    async def peer(_reader, writer):
        if reply == "incompatible":
            message = wire.Hello(
                chains=[], ready={}, default_chain="remote", modules=[],
                min_version=wire.VERSION + 1, version=wire.VERSION + 1,
            )
            writer.write(wire.encode(message).encode())
        elif reply == "not-hello":
            writer.write(wire.encode(wire.Ready(chain="local")).encode())
        else:
            await asyncio.sleep(0.1)
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(peer, path=path)
    states: list[bool] = []
    client = DaemonClient(path, on_state=states.append)
    task = asyncio.create_task(client.run())
    try:
        await asyncio.sleep(0.08)
        assert not client.connected
        assert True not in states
        assert client._writer is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.close()
        await server.wait_closed()


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
