"""Phase 2 core: watch a screen region for a color change and locate it.

The intended use is cancellation catching ("취켓팅"): the user frames the seat
map, PureClick keeps a baseline image of that region, and when a seat bubble
appears (someone's reservation is released) the changed spot is detected and
clicked within one or two poll intervals.

Detection is pure logic over sampled pixel grids so it is unit-testable on any
platform. Screen capture and key injection are Windows-only (ctypes GDI /
SendInput) and kept at the bottom of this module.
"""

from __future__ import annotations

import ctypes
import json
import platform
import time
from ctypes import wintypes
from dataclasses import dataclass, replace
from typing import Any

from pureclick_core import ClickError, PureClickError


class WatchError(PureClickError):
    """Raised when the watch region cannot be captured or configured."""


@dataclass(frozen=True)
class WatchRegion:
    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width < 8 or self.height < 8:
            raise ValueError("Watch region must be at least 8x8 pixels")

    def to_mapping(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "WatchRegion":
        return cls(
            left=int(data["left"]),
            top=int(data["top"]),
            width=int(data["width"]),
            height=int(data["height"]),
        )


@dataclass(frozen=True)
class WatchSettings:
    tolerance: int = 40
    min_points: int = 3
    max_fraction: float = 0.35
    confirm_frames: int = 2
    poll_ms: int = 60
    stride: int = 2
    refresh_seconds: float = 0.0
    region: WatchRegion | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.tolerance <= 255:
            raise ValueError("Tolerance must be between 1 and 255")
        if self.min_points < 1:
            raise ValueError("Min points must be at least 1")
        if not 0.0 < self.max_fraction <= 1.0:
            raise ValueError("Max fraction must be between 0 and 1")
        if self.confirm_frames < 1:
            raise ValueError("Confirm frames must be at least 1")
        if self.poll_ms < 10:
            raise ValueError("Poll interval must be at least 10 ms")
        if self.stride < 1:
            raise ValueError("Stride must be at least 1")
        if self.refresh_seconds < 0:
            raise ValueError("Refresh seconds cannot be negative")

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tolerance": self.tolerance,
            "min_points": self.min_points,
            "max_fraction": self.max_fraction,
            "confirm_frames": self.confirm_frames,
            "poll_ms": self.poll_ms,
            "stride": self.stride,
            "refresh_seconds": self.refresh_seconds,
        }
        if self.region is not None:
            data["region"] = self.region.to_mapping()
        return data

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "WatchSettings":
        region_data = data.get("region")
        return cls(
            tolerance=int(data.get("tolerance", 40)),
            min_points=int(data.get("min_points", 3)),
            max_fraction=float(data.get("max_fraction", 0.35)),
            confirm_frames=int(data.get("confirm_frames", 2)),
            poll_ms=int(data.get("poll_ms", 60)),
            stride=int(data.get("stride", 2)),
            refresh_seconds=float(data.get("refresh_seconds", 0.0)),
            region=WatchRegion.from_mapping(region_data) if region_data else None,
        )


def serialize_settings(settings: WatchSettings) -> str:
    return json.dumps(settings.to_mapping(), indent=2)


def settings_with_region(settings: WatchSettings, region: WatchRegion) -> WatchSettings:
    return replace(settings, region=region)


@dataclass(frozen=True)
class PixelGrid:
    cols: int
    rows: int
    stride: int
    pixels: tuple[int, ...]  # packed 0xRRGGBB, row-major


def grid_from_bgra(data: bytes, width: int, height: int, *, stride: int = 2) -> PixelGrid:
    """Downsample a top-down 32-bit BGRA buffer onto a coarse grid.

    Sampling every ``stride`` pixels keeps the per-frame diff cheap enough for
    pure Python at 15+ fps. Seat bubbles are far larger than the stride, so
    nothing relevant slips between sample points.
    """
    if len(data) < width * height * 4:
        raise ValueError("BGRA buffer is smaller than width*height*4")
    cols = max(1, (width + stride - 1) // stride)
    rows = max(1, (height + stride - 1) // stride)
    pixels: list[int] = []
    row_bytes = width * 4
    for row in range(rows):
        base = min(row * stride, height - 1) * row_bytes
        for col in range(cols):
            index = base + min(col * stride, width - 1) * 4
            b = data[index]
            g = data[index + 1]
            r = data[index + 2]
            pixels.append((r << 16) | (g << 8) | b)
    return PixelGrid(cols=cols, rows=rows, stride=stride, pixels=tuple(pixels))


def changed_points(
    baseline: PixelGrid,
    current: PixelGrid,
    *,
    tolerance: int,
) -> list[tuple[int, int]]:
    """Grid coordinates where any color channel moved more than ``tolerance``."""
    if (baseline.cols, baseline.rows) != (current.cols, current.rows):
        raise ValueError("Pixel grids have different shapes")
    points: list[tuple[int, int]] = []
    cols = baseline.cols
    base_pixels = baseline.pixels
    cur_pixels = current.pixels
    for index, (old, new) in enumerate(zip(base_pixels, cur_pixels)):
        if old == new:
            continue
        dr = abs((old >> 16) - (new >> 16))
        dg = abs(((old >> 8) & 0xFF) - ((new >> 8) & 0xFF))
        db = abs((old & 0xFF) - (new & 0xFF))
        if dr > tolerance or dg > tolerance or db > tolerance:
            points.append((index % cols, index // cols))
    return points


@dataclass(frozen=True)
class FrameResult:
    kind: str  # "quiet" | "unstable" | "candidate"
    changed: int
    point: tuple[int, int] | None = None  # grid coords of the click target


def classify_frame(
    baseline: PixelGrid,
    current: PixelGrid,
    *,
    tolerance: int,
    min_points: int,
    max_fraction: float,
) -> FrameResult:
    """Decide whether this frame shows a clickable appearance.

    - Fewer than ``min_points`` changed samples: quiet (noise).
    - More than ``max_fraction`` of the grid changed: unstable — the page is
      reloading, scrolling, or flashing. Never click into that.
    - Otherwise: candidate. The target is the changed point closest to the
      centroid of all changes, so two separate blobs cannot pull the click
      into empty space between them.
    """
    points = changed_points(baseline, current, tolerance=tolerance)
    if len(points) < min_points:
        return FrameResult(kind="quiet", changed=len(points))
    if len(points) > max_fraction * len(baseline.pixels):
        return FrameResult(kind="unstable", changed=len(points))

    mean_col = sum(point[0] for point in points) / len(points)
    mean_row = sum(point[1] for point in points) / len(points)
    target = min(
        points,
        key=lambda point: (point[0] - mean_col) ** 2 + (point[1] - mean_row) ** 2,
    )
    return FrameResult(kind="candidate", changed=len(points), point=target)


def grid_point_to_screen(
    region: WatchRegion,
    point: tuple[int, int],
    stride: int,
) -> tuple[int, int]:
    """Map a grid coordinate back to an absolute screen pixel (sample center)."""
    x = region.left + min(point[0] * stride + stride // 2, region.width - 1)
    y = region.top + min(point[1] * stride + stride // 2, region.height - 1)
    return x, y


class ConfirmTracker:
    """Requires N consecutive candidate frames before a fire is confirmed.

    One extra frame of confirmation costs a single poll interval but rejects
    one-frame flashes (cursor blinks, tooltips, render glitches).
    """

    def __init__(self, confirm_frames: int) -> None:
        self._required = max(1, confirm_frames)
        self._streak = 0
        self._last_point: tuple[int, int] | None = None

    def update(self, result: FrameResult) -> tuple[int, int] | None:
        if result.kind != "candidate" or result.point is None:
            self._streak = 0
            self._last_point = None
            return None
        self._streak += 1
        self._last_point = result.point
        if self._streak >= self._required:
            return self._last_point
        return None


# --- Windows-only capture and key injection ---------------------------------

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_DIB_RGB_COLORS = 0
_BI_RGB = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WindowsScreenGrabber:
    """Captures a screen region as a top-down BGRA buffer via GDI.

    Device contexts and the destination bitmap are cached between grabs, so a
    steady watch loop only pays for one BitBlt and one GetDIBits per frame.
    """

    def __init__(self) -> None:
        if platform.system() != "Windows":
            raise WatchError("Screen capture is only enabled on Windows")
        self._gdi32 = ctypes.windll.gdi32
        self._user32 = ctypes.windll.user32
        self._screen_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._bitmap_size: tuple[int, int] | None = None

    def _ensure_resources(self, width: int, height: int) -> None:
        if self._screen_dc is None:
            self._screen_dc = self._user32.GetDC(None)
            if not self._screen_dc:
                raise WatchError("GetDC failed")
            self._mem_dc = self._gdi32.CreateCompatibleDC(self._screen_dc)
            if not self._mem_dc:
                raise WatchError("CreateCompatibleDC failed")
        if self._bitmap_size != (width, height):
            if self._bitmap:
                self._gdi32.DeleteObject(self._bitmap)
            self._bitmap = self._gdi32.CreateCompatibleBitmap(self._screen_dc, width, height)
            if not self._bitmap:
                raise WatchError("CreateCompatibleBitmap failed")
            self._gdi32.SelectObject(self._mem_dc, self._bitmap)
            self._bitmap_size = (width, height)

    def grab(self, region: WatchRegion) -> bytes:
        self._ensure_resources(region.width, region.height)
        ok = self._gdi32.BitBlt(
            self._mem_dc,
            0,
            0,
            region.width,
            region.height,
            self._screen_dc,
            region.left,
            region.top,
            _SRCCOPY | _CAPTUREBLT,
        )
        if not ok:
            raise WatchError("BitBlt failed")

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = region.width
        info.bmiHeader.biHeight = -region.height  # negative = top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB

        buffer = ctypes.create_string_buffer(region.width * region.height * 4)
        rows = self._gdi32.GetDIBits(
            self._mem_dc,
            self._bitmap,
            0,
            region.height,
            buffer,
            ctypes.byref(info),
            _DIB_RGB_COLORS,
        )
        if rows != region.height:
            raise WatchError("GetDIBits failed")
        return buffer.raw

    def close(self) -> None:
        if self._bitmap:
            self._gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
            self._bitmap_size = None
        if self._mem_dc:
            self._gdi32.DeleteDC(self._mem_dc)
            self._mem_dc = None
        if self._screen_dc:
            self._user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None


_VK_F5 = 0x74
_KEYEVENTF_KEYUP = 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYINPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class _KEYINPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _KEYINPUT_UNION)]


def press_refresh_key() -> None:
    """Send F5 to the foreground window (the browser must be focused)."""
    if platform.system() != "Windows":
        raise ClickError("Key injection is only enabled on Windows")
    user32 = ctypes.windll.user32
    events = (_KEYINPUT * 2)(
        _KEYINPUT(1, _KEYINPUT_UNION(ki=_KEYBDINPUT(_VK_F5, 0, 0, 0, None))),
        _KEYINPUT(1, _KEYINPUT_UNION(ki=_KEYBDINPUT(_VK_F5, 0, _KEYEVENTF_KEYUP, 0, None))),
    )
    sent = user32.SendInput(2, events, ctypes.sizeof(_KEYINPUT))
    if sent != 2:
        raise ClickError(f"SendInput refresh key sent {sent} of 2 events")
