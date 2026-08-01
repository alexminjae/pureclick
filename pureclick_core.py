from __future__ import annotations

import ctypes
from ctypes import wintypes
import email.utils
import http.client
import platform
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable


class PureClickError(Exception):
    """Base exception for user-facing PureClick failures."""


class ServerTimeError(PureClickError):
    """Raised when server time cannot be sampled."""


class ClickError(PureClickError):
    """Raised when a click cannot be sent."""


KST = timezone(timedelta(hours=9), "KST")


@dataclass(frozen=True)
class TimeSample:
    offset_seconds: float
    rtt_seconds: float
    server_unix: float
    local_mid_unix: float
    raw_date: str
    local_mid_perf: float = 0.0
    local_recv_perf: float = 0.0


@dataclass(frozen=True)
class SyncResult:
    offset_seconds: float
    best_rtt_seconds: float
    samples: tuple[TimeSample, ...]
    anchor_perf: float = 0.0
    anchor_server_unix: float = 0.0
    mode: str = "median"

    @property
    def jitter_seconds(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return statistics.pstdev(sample.offset_seconds for sample in self.samples)

    @property
    def age_seconds(self) -> float:
        if not self.anchor_perf:
            return 0.0
        return max(0.0, time.perf_counter() - self.anchor_perf)


def parse_http_date(value: str) -> float:
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed is None:
        raise ServerTimeError(f"Could not parse Date header: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def fetch_server_date(
    url: str,
    *,
    timeout_seconds: float = 2.0,
    opener: Callable[..., object] | None = None,
) -> TimeSample:
    if not url.startswith(("http://", "https://")):
        raise ServerTimeError("Server URL must start with http:// or https://")

    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, method="HEAD")
    start_unix = time.time()
    start_perf = time.perf_counter()

    try:
        response = _open_request(opener, request, timeout_seconds)
    except urllib.error.HTTPError as exc:
        response = exc
    except Exception:
        request = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
        start_unix = time.time()
        start_perf = time.perf_counter()
        try:
            response = _open_request(opener, request, timeout_seconds)
        except Exception as exc:
            raise ServerTimeError(f"Could not reach server: {exc}") from exc

    end_perf = time.perf_counter()
    headers = getattr(response, "headers", None)
    raw_date = headers.get("Date") if headers is not None else None
    if not raw_date:
        raise ServerTimeError("Server response did not include a Date header")

    rtt = end_perf - start_perf
    local_mid_unix = start_unix + (rtt / 2.0)
    local_mid_perf = start_perf + (rtt / 2.0)
    server_unix = parse_http_date(raw_date)
    return TimeSample(
        offset_seconds=server_unix - local_mid_unix,
        rtt_seconds=rtt,
        server_unix=server_unix,
        local_mid_unix=local_mid_unix,
        raw_date=raw_date,
        local_mid_perf=local_mid_perf,
        local_recv_perf=end_perf,
    )


def _open_request(
    opener: Callable[..., object],
    request: urllib.request.Request,
    timeout_seconds: float,
) -> object:
    try:
        return opener(request, timeout=timeout_seconds)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return opener(request, timeout_seconds)


class KeepAliveProbe:
    """Samples the server ``Date`` header over one persistent connection.

    Reusing a single TCP/TLS connection removes the handshake cost from every
    poll, so the tick-catch loop can poll at nearly the bare network RTT. That
    directly tightens the bracket around the server's second boundary, which
    is the dominant error term in the whole sync.
    """

    def __init__(self, url: str, *, timeout_seconds: float = 1.5) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ServerTimeError("Server URL must start with http:// or https://")
        self._host = parts.hostname or ""
        self._port = parts.port
        self._https = parts.scheme == "https"
        self._path = parts.path or "/"
        if parts.query:
            self._path += "?" + parts.query
        self._timeout = timeout_seconds
        self._conn: http.client.HTTPConnection | None = None

    def _connect(self) -> http.client.HTTPConnection:
        if self._https:
            return http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def sample(self) -> TimeSample:
        last_error: Exception | None = None
        # One retry: a keep-alive connection the server quietly dropped fails
        # on the first request after the drop and succeeds on a fresh socket.
        for attempt in range(2):
            if self._conn is None:
                self._conn = self._connect()
            connection = self._conn
            try:
                start_unix = time.time()
                start_perf = time.perf_counter()
                connection.request(
                    "HEAD",
                    self._path,
                    headers={"Connection": "keep-alive", "User-Agent": "PureClick"},
                )
                response = connection.getresponse()
                response.read()
                end_perf = time.perf_counter()
            except Exception as exc:
                last_error = exc
                self.close()
                continue

            raw_date = response.getheader("Date")
            if response.will_close:
                self.close()
            if not raw_date:
                raise ServerTimeError("Server response did not include a Date header")

            rtt = end_perf - start_perf
            local_mid_unix = start_unix + (rtt / 2.0)
            local_mid_perf = start_perf + (rtt / 2.0)
            server_unix = parse_http_date(raw_date)
            return TimeSample(
                offset_seconds=server_unix - local_mid_unix,
                rtt_seconds=rtt,
                server_unix=server_unix,
                local_mid_unix=local_mid_unix,
                raw_date=raw_date,
                local_mid_perf=local_mid_perf,
                local_recv_perf=end_perf,
            )
        raise ServerTimeError(f"Could not reach server: {last_error}")


class ServerClock:
    def __init__(self) -> None:
        self._sync_result: SyncResult | None = None
        self._lock = threading.Lock()

    @property
    def sync_result(self) -> SyncResult | None:
        with self._lock:
            return self._sync_result

    def sync(
        self,
        url: str,
        *,
        sample_count: int = 9,
        keep_best: int = 5,
        timeout_seconds: float = 2.0,
        pause_seconds: float = 0.05,
    ) -> SyncResult:
        if sample_count < 1:
            raise ValueError("sample_count must be at least 1")

        samples: list[TimeSample] = []
        failures: list[str] = []
        for index in range(sample_count):
            try:
                samples.append(fetch_server_date(url, timeout_seconds=timeout_seconds))
            except ServerTimeError as exc:
                failures.append(str(exc))
            if index != sample_count - 1:
                time.sleep(pause_seconds)

        if not samples:
            detail = failures[-1] if failures else "no samples collected"
            raise ServerTimeError(f"Unable to sync server time: {detail}")

        best = tuple(sorted(samples, key=lambda sample: sample.rtt_seconds)[:keep_best])
        offset = statistics.median(sample.offset_seconds for sample in best)
        anchor_perf = time.perf_counter()
        anchor_server_unix = time.time() + offset
        result = SyncResult(
            offset_seconds=offset,
            best_rtt_seconds=min(sample.rtt_seconds for sample in samples),
            samples=best,
            anchor_perf=anchor_perf,
            anchor_server_unix=anchor_server_unix,
            mode="median",
        )
        with self._lock:
            self._sync_result = result
        return result

    def sync_tick(
        self,
        url: str,
        *,
        sample_count: int = 5,
        min_samples: int = 2,
        max_wait_seconds: float = 8.0,
        timeout_seconds: float = 1.5,
        poll_seconds: float = 0.01,
        opener: Callable[..., object] | None = None,
    ) -> SyncResult:
        if sample_count < 1:
            raise ValueError("sample_count must be at least 1")
        if min_samples < 1:
            raise ValueError("min_samples must be at least 1")

        probe: KeepAliveProbe | None = None
        if opener is None:
            probe = KeepAliveProbe(url, timeout_seconds=timeout_seconds)
            fetch = probe.sample
        else:
            def fetch() -> TimeSample:
                return fetch_server_date(url, timeout_seconds=timeout_seconds, opener=opener)

        deadline = time.perf_counter() + max_wait_seconds
        tick_samples: list[TimeSample] = []
        failures: list[str] = []

        try:
            try:
                previous = fetch()
                current_raw_date = previous.raw_date
            except ServerTimeError as exc:
                previous = None
                current_raw_date = ""
                failures.append(str(exc))

            while len(tick_samples) < sample_count and time.perf_counter() < deadline:
                try:
                    sample = fetch()
                except ServerTimeError as exc:
                    failures.append(str(exc))
                    time.sleep(poll_seconds)
                    continue

                if sample.raw_date != current_raw_date:
                    # The server-second boundary fell between the previous
                    # poll's receipt and this one. Bracket it with the midpoint
                    # of that gap, so the anchor error is at most half the poll
                    # spacing rather than a full RTT.
                    if previous is not None and previous.local_recv_perf:
                        bracket_perf = (previous.local_recv_perf + sample.local_recv_perf) / 2.0
                        bracket_gap = sample.local_recv_perf - previous.local_recv_perf
                    else:
                        bracket_perf = sample.local_mid_perf
                        bracket_gap = sample.rtt_seconds
                    tick_samples.append(
                        TimeSample(
                            offset_seconds=sample.offset_seconds,
                            rtt_seconds=bracket_gap,
                            server_unix=sample.server_unix,
                            local_mid_unix=sample.local_mid_unix,
                            raw_date=sample.raw_date,
                            local_mid_perf=bracket_perf,
                            local_recv_perf=sample.local_recv_perf,
                        )
                    )
                    current_raw_date = sample.raw_date
                    if len(tick_samples) >= min_samples:
                        brackets = sorted(s.rtt_seconds * 1000 for s in tick_samples)
                        jitter_ms = brackets[-1] - brackets[0] if len(brackets) > 1 else 0.0
                        if brackets[0] <= 60.0 and jitter_ms <= 40.0:
                            break

                previous = sample
                time.sleep(poll_seconds)
        finally:
            if probe is not None:
                probe.close()

        if not tick_samples:
            detail = failures[-1] if failures else "server Date header did not roll over"
            raise ServerTimeError(f"Unable to catch server time tick: {detail}")

        # Tightest bracket = smallest gap between the polls that straddled the
        # boundary, i.e. the most precisely located second tick.
        best = min(tick_samples, key=lambda sample: sample.rtt_seconds)
        selected = tuple(sorted(tick_samples, key=lambda sample: sample.rtt_seconds))
        result = SyncResult(
            offset_seconds=best.offset_seconds,
            best_rtt_seconds=best.rtt_seconds,
            samples=selected,
            anchor_perf=best.local_mid_perf,
            anchor_server_unix=best.server_unix,
            mode="tick",
        )
        with self._lock:
            self._sync_result = result
        return result

    def server_time_unix(self) -> float:
        result = self.sync_result
        if not result:
            return time.time()
        return result.anchor_server_unix + (time.perf_counter() - result.anchor_perf)

    def deadline_for_server_time(self, target_server_unix: float) -> float:
        result = self.sync_result
        if not result:
            return time.perf_counter() + (target_server_unix - self.server_time_unix())
        return result.anchor_perf + (target_server_unix - result.anchor_server_unix)


def parse_target_time(
    value: str,
    now_server_unix: float | None = None,
    *,
    target_tz: timezone | None = None,
) -> float:
    text = value.strip()
    if not text:
        raise ValueError("Target time is required")

    local_tz = target_tz or datetime.now().astimezone().tzinfo
    now = datetime.fromtimestamp(now_server_unix or time.time(), timezone.utc).astimezone(
        local_tz
    )
    formats: Iterable[str] = (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%H:%M:%S.%f",
        "%H:%M:%S",
    )
    last_error: ValueError | None = None

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError as exc:
            last_error = exc
            continue

        if fmt.startswith("%Y"):
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=local_tz)
        else:
            parsed = now.replace(
                hour=parsed.hour,
                minute=parsed.minute,
                second=parsed.second,
                microsecond=parsed.microsecond,
            )
            if parsed.timestamp() <= now.timestamp():
                parsed = parsed + timedelta(days=1)
        return parsed.timestamp()

    raise ValueError(
        "Use HH:MM:SS.mmm, YYYY-MM-DD HH:MM:SS.mmm, or ISO time with timezone"
    ) from last_error


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
                platform.system() == "Windows"
                and sleep_for <= high_res_window_seconds
                and sleep_for > 0
            ):
                _windows_waitable_sleep(sleep_for)
            else:
                stop_event.wait(sleep_for)
        else:
            while time.perf_counter() < deadline_perf:
                if stop_event.is_set():
                    return False
            return True


class WindowsPrecision:
    def __init__(self) -> None:
        self._enabled = False
        self._old_priority: int | None = None

    def __enter__(self) -> "WindowsPrecision":
        if platform.system() == "Windows":
            try:
                winmm = ctypes.windll.winmm
                kernel32 = ctypes.windll.kernel32
                kernel32.GetCurrentThread.argtypes = ()
                kernel32.GetCurrentThread.restype = wintypes.HANDLE
                kernel32.GetThreadPriority.argtypes = (wintypes.HANDLE,)
                kernel32.GetThreadPriority.restype = ctypes.c_int
                kernel32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
                kernel32.SetThreadPriority.restype = wintypes.BOOL
                winmm.timeBeginPeriod(1)
                thread = kernel32.GetCurrentThread()
                self._old_priority = kernel32.GetThreadPriority(thread)
                kernel32.SetThreadPriority(thread, 15)
                self._enabled = True
            except Exception:
                self._enabled = False
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._enabled:
            try:
                if self._old_priority is not None:
                    ctypes.windll.kernel32.SetThreadPriority(
                        ctypes.windll.kernel32.GetCurrentThread(), self._old_priority
                    )
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass


class WindowsClicker:
    def click(self, x: int, y: int, *, hold_ms: int = 20) -> None:
        if platform.system() != "Windows":
            raise ClickError("Real clicks are only enabled on Windows")
        _send_windows_click(x, y, hold_ms=hold_ms)

    def move_to(self, x: int, y: int) -> None:
        if platform.system() != "Windows":
            raise ClickError("Native cursor move is only enabled on Windows")
        _set_windows_cursor_pos(x, y)

    def cursor_position(self) -> tuple[int, int]:
        if platform.system() != "Windows":
            raise ClickError("Native cursor capture is only enabled on Windows")
        point = _POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            raise ClickError("GetCursorPos failed")
        return point.x, point.y


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUT_UNION)]


def _windows_waitable_sleep(duration_seconds: float) -> None:
    if duration_seconds <= 0:
        return
    kernel32 = ctypes.windll.kernel32
    timer = None
    try:
        kernel32.CreateWaitableTimerExW.argtypes = (
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel32.CreateWaitableTimerExW.restype = wintypes.HANDLE
        kernel32.CreateWaitableTimerW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateWaitableTimerW.restype = wintypes.HANDLE
        kernel32.SetWaitableTimer.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.LONG,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
        )
        kernel32.SetWaitableTimer.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        if hasattr(kernel32, "CreateWaitableTimerExW"):
            timer = kernel32.CreateWaitableTimerExW(None, None, 0x00000002, 0x001F0003)
        if not timer:
            timer = kernel32.CreateWaitableTimerW(None, True, None)
        if not timer:
            time.sleep(duration_seconds)
            return
        due_time = ctypes.c_longlong(-int(duration_seconds * 10_000_000))
        ok = kernel32.SetWaitableTimer(timer, ctypes.byref(due_time), 0, None, None, False)
        if not ok:
            time.sleep(duration_seconds)
            return
        kernel32.WaitForSingleObject(timer, 0xFFFFFFFF)
    except Exception:
        time.sleep(duration_seconds)
    finally:
        if timer:
            kernel32.CloseHandle(timer)


def enable_windows_dpi_awareness() -> None:
    """Make cursor, click, and screen-capture coordinates physical pixels.

    Must run before any window is created. Tries per-monitor-v2 first
    (correct on mixed-DPI multi-monitor setups), then falls back to older
    APIs for pre-1703 Windows 10.
    """
    if platform.system() != "Windows":
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _configure_user32() -> object:
    user32 = ctypes.windll.user32
    enable_windows_dpi_awareness()

    user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    user32.SetCursorPos.restype = ctypes.c_int
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    return user32


def _set_windows_cursor_pos(x: int, y: int) -> None:
    user32 = _configure_user32()
    if not user32.SetCursorPos(int(x), int(y)):
        raise ClickError("SetCursorPos failed")


def _send_windows_click(x: int, y: int, *, hold_ms: int = 20) -> None:
    user32 = _configure_user32()
    if not user32.SetCursorPos(int(x), int(y)):
        raise ClickError("SetCursorPos failed")

    hold_seconds = max(0.0, hold_ms / 1000.0)
    inputs = (_INPUT * 2)(
        _INPUT(0, _INPUT_UNION(mi=_MOUSEINPUT(0, 0, 0, 0x0002, 0, 0))),
        _INPUT(0, _INPUT_UNION(mi=_MOUSEINPUT(0, 0, 0, 0x0004, 0, 0))),
    )
    down = (_INPUT * 1)(inputs[0])
    up = (_INPUT * 1)(inputs[1])
    sent_down = user32.SendInput(1, down, ctypes.sizeof(_INPUT))
    if sent_down != 1:
        raise ClickError(f"SendInput mouse-down sent {sent_down} of 1 events")
    if hold_seconds:
        time.sleep(hold_seconds)
    sent_up = user32.SendInput(1, up, ctypes.sizeof(_INPUT))
    if sent_up != 1:
        raise ClickError(f"SendInput mouse-up sent {sent_up} of 1 events")
