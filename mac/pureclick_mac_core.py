from __future__ import annotations

import ctypes
import ctypes.util
import platform
import threading
import time
from typing import TYPE_CHECKING

from pureclick_core import ClickError, PureClickError

if TYPE_CHECKING:
    from types import TracebackType


class MacClickError(ClickError):
    """Raised when macOS native input APIs are unavailable or denied."""


_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_kCGHIDEventTap = 0
_kCGEventLeftMouseDown = 1
_kCGEventLeftMouseUp = 2
_kCGEventMouseMoved = 5
_kCGMouseButtonLeft = 0


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load_coregraphics() -> ctypes.CDLL:
    if platform.system() != "Darwin":
        raise MacClickError("CoreGraphics clicker is only available on macOS")
    try:
        return ctypes.CDLL(_CG_PATH)
    except OSError as exc:
        raise MacClickError(f"Could not load CoreGraphics: {exc}") from exc


def _configure_coregraphics(cg: ctypes.CDLL) -> None:
    cg.CGMainDisplayID.argtypes = []
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = _CGPoint
    cg.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        _CGPoint,
        ctypes.c_uint32,
    ]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventPost.restype = None
    cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    cg.CGEventSetType.restype = None


_CG: ctypes.CDLL | None = None
_SCREEN_HEIGHT: int | None = None


def _coregraphics() -> ctypes.CDLL:
    global _CG
    if _CG is None:
        _CG = _load_coregraphics()
        _configure_coregraphics(_CG)
    return _CG


def screen_height() -> int:
    global _SCREEN_HEIGHT
    if _SCREEN_HEIGHT is None:
        cg = _coregraphics()
        _SCREEN_HEIGHT = int(cg.CGDisplayPixelsHigh(cg.CGMainDisplayID()))
    return _SCREEN_HEIGHT


def top_left_to_cg_point(x: int, y: int) -> _CGPoint:
    return _CGPoint(float(x), float(screen_height() - y))


def cg_point_to_top_left(point: _CGPoint) -> tuple[int, int]:
    return int(point.x), int(screen_height() - point.y)


def _post_mouse_event(event_type: int, x: int, y: int) -> None:
    cg = _coregraphics()
    point = top_left_to_cg_point(x, y)
    event = cg.CGEventCreateMouseEvent(None, event_type, point, _kCGMouseButtonLeft)
    if not event:
        raise MacClickError("CGEventCreateMouseEvent failed")
    cg.CGEventPost(_kCGHIDEventTap, event)


def accessibility_hint() -> str:
    return (
        "macOS requires Accessibility permission for PureClick to click other apps. "
        "Open System Settings → Privacy & Security → Accessibility and enable "
        "Terminal, iTerm, or Python."
    )


def ensure_mac_ready() -> None:
    if platform.system() != "Darwin":
        raise PureClickError("PureClick for Mac only runs on macOS")
    try:
        _coregraphics()
    except MacClickError as exc:
        raise PureClickError(str(exc)) from exc
