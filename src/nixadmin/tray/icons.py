"""Tray icon rendering — pure, dependency-free ARGB32 discs.

We draw the icon ourselves rather than depend on a named theme icon: it guarantees
the same green/amber dot on any desktop, and keeps the whole tray to stdlib +
``dbus-fast``. The StatusNotifierItem ``IconPixmap`` format is an array of
``(width, height, bytes)`` where the bytes are ARGB32 in network (big-endian)
byte order — one byte each of A, R, G, B per pixel.
"""

from __future__ import annotations

# Palette — a calm green and a clear-but-not-alarming amber (the ADAS "kept-well"
# tone, not a red klaxon). Grey means "I can't tell" (daemon unreachable).
HEALTHY = (0x2E, 0xCC, 0x71)
ATTENTION = (0xF3, 0x9C, 0x12)
UNKNOWN = (0x95, 0xA5, 0xA6)

# Two sizes so HiDPI hosts pick a crisp one.
SIZES = (22, 44)

RGB = tuple[int, int, int]
Pixmap = list[object]  # [width: int, height: int, data: bytes]


def disc_argb(size: int, rgb: RGB) -> bytes:
    """A filled, softly anti-aliased disc of ``rgb`` on transparent, ARGB32."""
    r, g, b = rgb
    center = (size - 1) / 2.0
    radius = size / 2.0 - 0.5
    r2 = radius * radius
    ss = 3  # supersampling for a smooth edge
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            covered = 0
            for sy in range(ss):
                for sx in range(ss):
                    px = x + (sx + 0.5) / ss - 0.5 - center
                    py = y + (sy + 0.5) / ss - 0.5 - center
                    if px * px + py * py <= r2:
                        covered += 1
            alpha = (covered * 255) // (ss * ss)
            i = (y * size + x) * 4
            out[i] = alpha
            out[i + 1] = r
            out[i + 2] = g
            out[i + 3] = b
    return bytes(out)


def pixmaps(rgb: RGB) -> list[Pixmap]:
    """The full ``a(iiay)`` pixmap array for a colour, one entry per size."""
    return [[s, s, disc_argb(s, rgb)] for s in SIZES]


def health_color(connected: bool, failure_count: int) -> RGB:
    if not connected:
        return UNKNOWN
    return ATTENTION if failure_count > 0 else HEALTHY


def status_word(connected: bool, failure_count: int) -> str:
    """SNI ``Status``: keep the icon visible always (never ``Passive``); flag
    failures as ``NeedsAttention`` so hosts that highlight it, do."""
    if connected and failure_count > 0:
        return "NeedsAttention"
    return "Active"
