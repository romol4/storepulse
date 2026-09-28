from __future__ import annotations

import struct
import zlib
from pathlib import Path

from storepulse.core import charts

FIXTURES = Path(__file__).parent / "fixtures" / "charts"


def _decode(png: bytes) -> tuple[int, int, int, int, list[bytearray]]:
    """A minimal, independent PNG reader for these always-filter-0 grayscale images.

    Returns (width, height, bit_depth, color_type, rows).
    """
    assert png[:8] == charts.PNG_SIGNATURE
    pos = 8
    chunks: dict[bytes, bytes] = {}
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        tag = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        crc_read = png[pos + 8 + length : pos + 12 + length]
        assert struct.pack(">I", zlib.crc32(tag + data)) == crc_read, f"bad CRC on {tag!r}"
        chunks[tag] = data
        pos += 12 + length
    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"], "unexpected chunk order/set"
    width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
        ">IIBBBBB", chunks[b"IHDR"]
    )
    assert (comp, filt, interlace) == (0, 0, 0)
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = width + 1  # one filter-type byte per scanline
    assert len(raw) == stride * height
    rows = []
    for y in range(height):
        line = raw[y * stride : (y + 1) * stride]
        assert line[0] == 0, "every scanline must use filter type 0 (None)"
        rows.append(bytearray(line[1:]))
    return width, height, bit_depth, color_type, rows


def test_default_size_and_grayscale_ihdr() -> None:
    width, height, bit_depth, color_type, rows = _decode(charts.render_sparkline([1, 2, 3]))
    assert (width, height) == (charts.WIDTH, charts.HEIGHT)
    assert (bit_depth, color_type) == (8, 0)
    assert len(rows) == height and all(len(r) == width for r in rows)


def test_custom_size_is_honored() -> None:
    width, height, *_rest = _decode(charts.render_sparkline([1, 2], width=40, height=10))
    assert (width, height) == (40, 10)


def test_empty_values_is_a_blank_canvas() -> None:
    _w, _h, _bd, _ct, rows = _decode(charts.render_sparkline([]))
    assert all(byte == 255 for row in rows for byte in row)


def _mid_row(height: int) -> int:
    """The module keeps a MARGIN-pixel gap top and bottom; this matches its midpoint."""
    margin = 2
    return (margin + (height - 1 - margin)) // 2


def test_single_value_draws_one_pixel() -> None:
    height = 8
    _width, _h, _bd, _ct, rows = _decode(charts.render_sparkline([7], width=20, height=height))
    dark = [(x, y) for y in range(height) for x in range(20) if rows[y][x] != 255]
    assert dark == [(19, _mid_row(height))]


def test_flat_values_draw_one_horizontal_midline() -> None:
    _width, height, _bd, _ct, rows = _decode(
        charts.render_sparkline([5, 5, 5, 5], width=20, height=10)
    )
    mid = _mid_row(height)
    for y, row in enumerate(rows):
        expected = 37 if y == mid else 255
        assert set(row) == {expected}, f"row {y} should be all {expected}"


def test_rising_values_draw_higher_on_the_left_is_lower() -> None:
    # A strictly increasing series should end higher (smaller y) than it starts.
    width, height, _bd, _ct, rows = _decode(charts.render_sparkline([0, 10], width=20, height=10))
    first_dark_y = min(y for y in range(height) if rows[y][0] != 255)
    last_dark_y = min(y for y in range(height) if rows[y][width - 1] != 255)
    assert last_dark_y < first_dark_y


def test_output_is_deterministic_across_calls() -> None:
    values = [1, 5, 3, 9, 2, 2, 2, 0, 10]
    assert charts.render_sparkline(values) == charts.render_sparkline(values)


def test_golden_bytes() -> None:
    expected = (FIXTURES / "sparkline_golden.png").read_bytes()
    assert charts.render_sparkline([0, 3, 1, 4], width=10, height=6) == expected
