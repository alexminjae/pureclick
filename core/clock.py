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


class NolSniperError(Exception):
    """Base exception for user-facing NOL Sniper failures."""


class ServerTimeError(NolSniperError):
    """Raised when server time cannot be sampled."""


class ClickError(NolSniperError):
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


# How far the monotonic anchor may drift from the wall clock before the sync is
# treated as describing the past rather than the present. Sleep produces hours;
# an NTP slew produces well under a second, so this separates them cleanly.
ANCHOR_DRIFT_TOLERANCE_SECONDS = 2.0


@dataclass(frozen=True)
class SyncResult:
    offset_seconds: float
    best_rtt_seconds: float
    samples: tuple[TimeSample, ...]
    anchor_perf: float = 0.0
    anchor_server_unix: float = 0.0
    # The wall clock at the same instant as anchor_perf. perf_counter is
    # mach_absolute_time() on macOS and does not advance while the machine
    # sleeps, so the monotonic anchor alone cannot tell a long sleep from a
    # short one — and there is nothing to compare it against without this.
    anchor_wall_unix: float = 0.0
    mode: str = "median"

    @property
    def jitter_seconds(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return statistics.pstdev(sample.offset_seconds for sample in self.samples)

    @property
    def age_seconds(self) -> float:
        """How long ago this sync happened, in real time.

        Measured on the wall clock. perf_counter stops during sleep, so a sync
        taken before a 23-hour sleep reported as hours younger than it was —
        on the reading whose whole job is to say "this is too old to trust".
        """
        if self.anchor_wall_unix:
            return max(0.0, time.time() - self.anchor_wall_unix)
        if not self.anchor_perf:
            return 0.0
        return max(0.0, time.perf_counter() - self.anchor_perf)

    @property
    def anchor_drift_seconds(self) -> float:
        """How far the monotonic anchor and the wall clock have diverged.

        Positive means real time has advanced further than perf_counter, i.e.
        the machine slept. An NTP step shows up here too. Either way the sync is
        no longer describing the present.
        """
        if not self.anchor_perf or not self.anchor_wall_unix:
            return 0.0
        monotonic_elapsed = time.perf_counter() - self.anchor_perf
        wall_elapsed = time.time() - self.anchor_wall_unix
        return wall_elapsed - monotonic_elapsed


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
                    headers={"Connection": "keep-alive", "User-Agent": "NOLSniper"},
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
            anchor_wall_unix=time.time(),
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
            anchor_wall_unix=best.server_unix - best.offset_seconds,
            mode="tick",
        )
        with self._lock:
            self._sync_result = result
        return result

    def anchor_is_stale(self, tolerance_seconds: float = ANCHOR_DRIFT_TOLERANCE_SECONDS) -> bool:
        """True when the monotonic anchor no longer describes the present.

        The anchor is monotonic on purpose: over the seconds around a ticket open
        it is immune to an NTP step that would otherwise move the target. Held
        for hours it is the opposite of safe, because perf_counter is
        mach_absolute_time() and does not advance while the Mac sleeps.

        On Windows perf_counter is QueryPerformanceCounter, which *does* keep
        advancing across sleep, so there this catches only NTP steps and never
        a resume. That is benign — the anchor stays valid, which is why nothing
        branches on the platform here — but it means the wake-up re-sync in the
        panel is a macOS safety net, not a cross-platform one. Worth knowing
        before relying on it.

        Observed: a panel left running from 20:45 across 23 hours of sleep had a
        clock 19 hours behind. `_sale_open()` reads datetime.now() and correctly
        said 판매 중, while the countdown beside it read this clock and was still
        counting down to an open that had already happened. Same panel, two
        clocks, no way to tell which was lying.
        """
        result = self.sync_result
        if not result:
            return False
        return abs(result.anchor_drift_seconds) > tolerance_seconds

    def server_time_unix(self) -> float:
        result = self.sync_result
        if not result:
            return time.time()
        if self.anchor_is_stale():
            # The wall clock survived the sleep; the anchor did not. The measured
            # offset is still the best correction we have, so keep it and drop
            # only the anchor.
            return time.time() + result.offset_seconds
        return result.anchor_server_unix + (time.perf_counter() - result.anchor_perf)

    def deadline_for_server_time(self, target_server_unix: float) -> float:
        result = self.sync_result
        if not result or self.anchor_is_stale():
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
