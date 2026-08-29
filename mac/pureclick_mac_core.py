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


class MacClicker:
    """Native macOS clicker using CoreGraphics events (top-left screen coordinates)."""

    def click(self, x: int, y: int, *, hold_ms: int = 20) -> None:
        if platform.system() != "Darwin":
            raise MacClickError("Real clicks are only enabled on macOS")
        _post_mouse_event(_kCGEventLeftMouseDown, x, y)
        hold_seconds = max(0.0, hold_ms / 1000.0)
        if hold_seconds:
            time.sleep(hold_seconds)
        _post_mouse_event(_kCGEventLeftMouseUp, x, y)

    def move_to(self, x: int, y: int) -> None:
        if platform.system() != "Darwin":
            raise MacClickError("Native cursor move is only enabled on macOS")
        cg = _coregraphics()
        point = top_left_to_cg_point(x, y)
        event = cg.CGEventCreateMouseEvent(None, _kCGEventMouseMoved, point, 0)
        if not event:
            raise MacClickError("CGEventCreateMouseEvent failed for move")
        cg.CGEventPost(_kCGHIDEventTap, event)

    def cursor_position(self) -> tuple[int, int]:
        if platform.system() != "Darwin":
            raise MacClickError("Native cursor capture is only enabled on macOS")
        cg = _coregraphics()
        event = cg.CGEventCreate(None)
        if not event:
            raise MacClickError("CGEventCreate failed")
        return cg_point_to_top_left(cg.CGEventGetLocation(event))


class MacPrecision:
    """Raise thread scheduling priority during the final wait window."""

    def __init__(self) -> None:
        self._enabled = False
        self._policy: int | None = None

    def __enter__(self) -> MacPrecision:
        if platform.system() != "Darwin":
            return self
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            libc.pthread_self.argtypes = []
            libc.pthread_self.restype = ctypes.c_void_p
            libc.pthread_getschedparam.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_void_p,
            ]
            libc.pthread_getschedparam.restype = ctypes.c_int
            libc.pthread_setschedparam.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            libc.pthread_setschedparam.restype = ctypes.c_int

            policy = ctypes.c_int()
            param = ctypes.c_byte * 32
            scheduling = param()
            thread = libc.pthread_self()
            if libc.pthread_getschedparam(thread, ctypes.byref(policy), scheduling) == 0:
                self._policy = policy.value
                # SCHED_FIFO = 4 on macOS; use priority 63 (max for FIFO is 63)
                scheduling[0] = 63
                if libc.pthread_setschedparam(thread, 4, scheduling) == 0:
                    self._enabled = True
        except Exception:
            self._enabled = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._enabled or self._policy is None:
            return
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            libc.pthread_self.argtypes = []
            libc.pthread_self.restype = ctypes.c_void_p
            libc.pthread_setschedparam.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            libc.pthread_setschedparam.restype = ctypes.c_int
            param = ctypes.c_byte * 32
            scheduling = param()
            libc.pthread_setschedparam(libc.pthread_self(), self._policy, scheduling)
        except Exception:
            pass


def _mach_waitable_sleep(duration_seconds: float) -> None:
    if duration_seconds <= 0:
        return
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.mach_absolute_time.argtypes = []
        libc.mach_absolute_time.restype = ctypes.c_uint64
        libc.mach_wait_until.argtypes = [ctypes.c_uint64]
        libc.mach_wait_until.restype = ctypes.c_int

        class MachTimebaseInfo(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        libc.mach_timebase_info.argtypes = [ctypes.POINTER(MachTimebaseInfo)]
        libc.mach_timebase_info.restype = ctypes.c_int
        info = MachTimebaseInfo()
        if libc.mach_timebase_info(ctypes.byref(info)) != 0:
            time.sleep(duration_seconds)
            return
        start = libc.mach_absolute_time()
        ticks = int(duration_seconds * 1_000_000_000 * info.denom / info.numer) + start
        libc.mach_wait_until(ticks)
    except Exception:
        time.sleep(duration_seconds)


def precise_wait_until(
    deadline_perf: float,
    *,
    stop_event: threading.Event | None = None,
    spin_window_seconds: float = 0.004,
    high_res_window_seconds: float = 0.050,
) -> bool:
    stop_event = stop_event or threading.Event()
    while True:
        remaining = deadline_perf - time.perf_counter()
        if remaining <= 0:
            return not stop_event.is_set()
        if stop_event.is_set():
            return False
        if remaining > spin_window_seconds:
            sleep_for = min(remaining - spin_window_seconds, 0.05)
            if (
                platform.system() == "Darwin"
                and sleep_for <= high_res_window_seconds
                and sleep_for > 0
            ):
                _mach_waitable_sleep(sleep_for)
            else:
                stop_event.wait(sleep_for)
        else:
            while time.perf_counter() < deadline_perf:
                if stop_event.is_set():
                    return False
            return True


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
