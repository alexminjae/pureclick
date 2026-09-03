"""Draw NOLSniper.icns — the Dock icon.

No image library. A PNG is zlib plus four chunks, and the only shapes here are
a rounded square and a reticle, so adding Pillow to requirements.txt for one
icon that will never change would cost more than it saves.

The palette is the app's own (see the header of nolsniper.py): ink ground, one
hot accent, nothing else. A Dock icon is the smallest surface this design has,
so it is the last place to start adding hues.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from pathlib import Path

MAC_DIR = Path(__file__).resolve().parent

GROUND = (0x14, 0x14, 0x1A)
ACCENT = (0xFF, 0x4D, 0x2E)

# All as a fraction of the canvas, measured from the centre.
INSET = 0.085          # macOS icons never bleed to the edge
CORNER = 0.225         # the Big Sur squircle, near enough
RING_INNER, RING_OUTER = 0.250, 0.315
TICK_INNER, TICK_OUTER = 0.315, 0.430
TICK_HALF = 0.032
DOT = 0.060

SUPERSAMPLE = 2        # the only antialiasing there is
MASTER = 1024
ICONSET_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _write_png(path: Path, size: int, rows: list[bytearray]) -> None:
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _in_rounded_square(x: float, y: float) -> bool:
    half = 0.5 - INSET
    flat = half - CORNER
    if abs(x) > half or abs(y) > half:
        return False
    dx, dy = max(abs(x) - flat, 0.0), max(abs(y) - flat, 0.0)
    return dx * dx + dy * dy <= CORNER * CORNER


def _in_reticle(x: float, y: float) -> bool:
    r2 = x * x + y * y
    if RING_INNER * RING_INNER <= r2 <= RING_OUTER * RING_OUTER:
        return True
    if r2 <= DOT * DOT:
        return True
    # Four ticks, outside the ring, on the axes.
    if abs(y) <= TICK_HALF and TICK_INNER <= abs(x) <= TICK_OUTER:
        return True
    if abs(x) <= TICK_HALF and TICK_INNER <= abs(y) <= TICK_OUTER:
        return True
    return False


def render(size: int) -> list[bytearray]:
    """RGBA rows. Only one quadrant is computed — the mark is symmetric in both
    axes, and at 2048 samples a square the interpreter walks four times is the
    difference between a second and four."""
    span = size * SUPERSAMPLE
    half = span // 2
    step = 1.0 / span

    # coverage[y][x] for the top-left quadrant, as (ground, accent) sample counts
    quadrant: list[list[tuple[int, int]]] = []
    for py in range(size // 2):
        row: list[tuple[int, int]] = []
        for px in range(size // 2):
            ground = accent = 0
            for sy in range(SUPERSAMPLE):
                y = (py * SUPERSAMPLE + sy - half + 0.5) * step
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx - half + 0.5) * step
                    if not _in_rounded_square(x, y):
                        continue
                    ground += 1
                    if _in_reticle(x, y):
                        accent += 1
            row.append((ground, accent))
        quadrant.append(row)

    total = SUPERSAMPLE * SUPERSAMPLE
    rows: list[bytearray] = []
    for py in range(size):
        qy = quadrant[py if py < size // 2 else size - 1 - py]
        row = bytearray()
        for px in range(size):
            ground, accent = qy[px if px < size // 2 else size - 1 - px]
            alpha = round(255 * ground / total)
            if not alpha:
                row += b"\x00\x00\x00\x00"
                continue
            mix = accent / total
            colour = tuple(
                round(GROUND[i] + (ACCENT[i] - GROUND[i]) * (mix * total / ground))
                for i in range(3)
            )
            row += bytes((*colour, alpha))
        rows.append(row)
    return rows


def main() -> int:
    out = MAC_DIR / "NOLSniper.icns"
    iconset = MAC_DIR / "NOLSniper.iconset"
    subprocess.run(["rm", "-rf", str(iconset)], check=True)
    iconset.mkdir()

    master = iconset / "master.png"
    print(f"drawing {MASTER}px master…", flush=True)
    _write_png(master, MASTER, render(MASTER))

    # sips downsamples better than a box filter written here would, and it is
    # already on every Mac.
    for size in ICONSET_SIZES:
        target = iconset / f"icon_{size}.png"
        subprocess.run(["sips", "-z", str(size), str(size), str(master),
                        "--out", str(target)], check=True, capture_output=True)

    # iconutil wants Apple's names, and both members of each @1x/@2x pair.
    names = {
        16: ["icon_16x16.png"],
        32: ["icon_16x16@2x.png", "icon_32x32.png"],
        64: ["icon_32x32@2x.png"],
        128: ["icon_128x128.png"],
        256: ["icon_128x128@2x.png", "icon_256x256.png"],
        512: ["icon_256x256@2x.png", "icon_512x512.png"],
        1024: ["icon_512x512@2x.png"],
    }
    for size, targets in names.items():
        for name in targets:
            (iconset / name).write_bytes((iconset / f"icon_{size}.png").read_bytes())
        (iconset / f"icon_{size}.png").unlink()
    master.unlink()

    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    subprocess.run(["rm", "-rf", str(iconset)], check=True)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
