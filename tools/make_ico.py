"""Write mac/pureclick.ico from the same drawing the .icns uses.

The exe is a file you hand to someone, and a generic PyInstaller icon reads as
unfinished. The macOS pipeline could not produce this: make_icon.py resizes with
`sips` and packs with `iconutil`, both macOS-only binaries.

Nothing here needs them. `render(size)` in make_icon.py draws at any size in
pure Python, and a .ico is a header plus directory entries plus embedded PNGs —
so this runs anywhere, including the Windows build runner.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mac"))

from make_icon import render  # noqa: E402

# 256 is what Explorer shows at large sizes; 16 and 32 are the taskbar and title
# bar, and are drawn rather than downscaled so they stay legible.
SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = ROOT / "mac" / "pureclick.ico"


def png_bytes(size: int, rows: list[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    images = [(size, png_bytes(size, render(size))) for size in SIZES]

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    directory = b""
    for size, payload in images:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256 in the ICO directory
            0 if size >= 256 else size,
            0,                            # a full-colour image declares no palette
            0,
            1,                            # colour planes
            32,                           # bits per pixel
            len(payload),
            offset,
        )
        offset += len(payload)

    OUT.write_bytes(header + directory + b"".join(payload for _, payload in images))
    print(f"wrote {OUT.relative_to(ROOT)} — {len(images)} sizes, {OUT.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
