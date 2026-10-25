"""Render the favicon raster assets from the geometry of ``public/favicon.svg``.

Run with ``python3 frontend/scripts/make_favicon.py`` after changing the icon. It needs
no third-party packages: every shape is drawn into an RGBA buffer at a supersampled
resolution and downscaled, which gives clean edges without any image tooling.
"""

import struct
import zlib
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent / "public"
PREVIEW = Path("/tmp/favicon-preview")

UNITS = 32.0  # the SVG viewBox
SUPERSAMPLE = 4

DARK = (0x10, 0x18, 0x28)
SHEET = (0xEE, 0xF2, 0xF6)
GOLD = (0xF0, 0xC6, 0x74)
ACCENT = (0xE0, 0x81, 0x7A)

# (x, y, width, height, corner radius, colour, optional clip below y)
Shape = tuple[float, float, float, float, float, tuple[int, int, int], float | None]

TILE: list[Shape] = [
    (0, 0, 32, 32, 7, DARK, None),
    (4, 6.5, 24, 21.5, 3.5, SHEET, None),
    (4, 6.5, 24, 6.5, 3.5, GOLD, 13),
    (9.25, 3.5, 2.5, 5.5, 1.25, GOLD, None),
    (20.25, 3.5, 2.5, 5.5, 1.25, GOLD, None),
    (7.75, 15.75, 4.25, 3.5, 1.1, DARK, None),
    (13.9, 15.75, 4.25, 3.5, 1.1, DARK, None),
    (20.05, 15.75, 4.25, 3.5, 1.1, DARK, None),
    (7.75, 21.5, 4.25, 3.5, 1.1, DARK, None),
    (13.9, 21.5, 4.25, 3.5, 1.1, ACCENT, None),
    (20.05, 21.5, 4.25, 3.5, 1.1, DARK, None),
]

# The touch icon keeps square corners: iOS applies its own mask, and a transparent
# corner would show up black on the home screen.
TOUCH: list[Shape] = [(0, 0, 32, 32, 0, DARK, None), *TILE[1:]]


def covered(px: float, py: float, shape: Shape) -> bool:
    x, y, w, h, radius, _, clip_bottom = shape
    if clip_bottom is not None and py > clip_bottom:
        return False
    if not (x <= px <= x + w and y <= py <= y + h):
        return False
    radius = min(radius, w / 2, h / 2)
    nearest_x = min(max(px, x + radius), x + w - radius)
    nearest_y = min(max(py, y + radius), y + h - radius)
    dx, dy = px - nearest_x, py - nearest_y
    return dx * dx + dy * dy <= radius * radius


def render(size: int, shapes: list[Shape]) -> bytes:
    scale = size / UNITS * SUPERSAMPLE
    pixels = bytearray(size * size * 4)

    for shape in shapes:
        x, y, w, h, _, color, _ = shape
        for sy in range(int(y * scale), int((y + h) * scale) + 1):
            row = (sy // SUPERSAMPLE) * size
            for sx in range(int(x * scale), int((x + w) * scale) + 1):
                if covered((sx + 0.5) / scale, (sy + 0.5) / scale, shape):
                    offset = (row + sx // SUPERSAMPLE) * 4
                    pixels[offset : offset + 4] = bytes(color) + b"\xff"
    return bytes(pixels)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def png_bytes(size: int, pixels: bytes) -> bytes:
    stride = size * 4
    raw = b"".join(b"\x00" + pixels[row * stride : (row + 1) * stride] for row in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def write_png(path: Path, size: int, pixels: bytes) -> None:
    path.write_bytes(png_bytes(size, pixels))


def write_ico(path: Path, size: int, pixels: bytes) -> None:
    """A single-image ICO whose payload is a PNG (supported since Windows Vista)."""
    png = png_bytes(size, pixels)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), 22)
    path.write_bytes(header + entry + png)


def main() -> None:
    PREVIEW.mkdir(exist_ok=True)
    write_png(PUBLIC / "apple-touch-icon.png", 180, render(180, TOUCH))
    write_ico(PUBLIC / "favicon.ico", 32, render(32, TILE))
    for size in (16, 32, 64):
        write_png(PREVIEW / f"tile-{size}.png", size, render(size, TILE))
    write_png(PREVIEW / "touch-180.png", 180, render(180, TOUCH))
    print("geschrieben:", *(p.name for p in sorted(PUBLIC.iterdir())), sep="\n  ")


if __name__ == "__main__":
    main()
