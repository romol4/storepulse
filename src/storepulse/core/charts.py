"""A tiny, dependency-free PNG writer: just enough to draw an installs sparkline.

Charts are embedded inline in the digest email by ``cid`` (docs/SPEC.md: email clients
strip SVG and JavaScript), so this draws a flat 8-bit grayscale PNG by hand with
``struct`` and ``zlib`` rather than pulling in an imaging library.

The IDAT chunk is written with ``zlib`` compression level 0 (stored/uncompressed deflate
blocks) on purpose: stored blocks have a fully-specified, choice-free byte encoding
(RFC 1951), so the output is byte-for-byte identical across zlib builds. A sparkline is
tiny, so the size cost of skipping real compression is a few hundred bytes, and it keeps
the golden-bytes test portable across the CI matrix's three operating systems.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence
from itertools import pairwise

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

WIDTH = 96
HEIGHT = 24
_BACKGROUND = 255  # white
_LINE = 37  # dark gray
_MARGIN = 2  # pixels kept clear at top/bottom so the line never touches the edge


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def _encode_png(rows: list[bytearray], width: int, height: int) -> bytes:
    """``rows``: ``height`` rows of ``width`` grayscale (0-255) bytes each."""
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 ("None") on every scanline
        raw += row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    idat = zlib.compress(bytes(raw), level=0)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _draw_line(canvas: list[bytearray], x0: int, y0: int, x1: int, y1: int, value: int) -> None:
    """Bresenham's line algorithm onto ``canvas``, mutated in place."""
    height, width = len(canvas), len(canvas[0])
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    x, y = x0, y0
    while True:
        if 0 <= x < width and 0 <= y < height:
            canvas[y][x] = value
        if x == x1 and y == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def render_sparkline(values: Sequence[float], *, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """Render ``values`` (oldest first) as a PNG polyline sparkline."""
    canvas = [bytearray([_BACKGROUND] * width) for _ in range(height)]
    points = list(values)
    top, bottom = _MARGIN, height - 1 - _MARGIN
    if len(points) == 1:
        canvas[(top + bottom) // 2][width - 1] = _LINE
    elif len(points) >= 2:
        lo, hi = min(points), max(points)
        span = hi - lo

        def plot_y(value: float) -> int:
            if span == 0:
                return (top + bottom) // 2
            # Higher values draw higher on the canvas, i.e. a smaller y.
            return round(bottom - (value - lo) / span * (bottom - top))

        n = len(points)
        xs = [round(i * (width - 1) / (n - 1)) for i in range(n)]
        ys = [plot_y(v) for v in points]
        coords = list(zip(xs, ys, strict=True))
        for (x0, y0), (x1, y1) in pairwise(coords):
            _draw_line(canvas, x0, y0, x1, y1, _LINE)
    return _encode_png(canvas, width, height)
