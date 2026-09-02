from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from core.clock import (
    SyncResult,
    KST,
    ServerClock,
    TimeSample,
    fetch_server_date,
    parse_http_date,
    parse_target_time,
)


class PureClickCoreTests(unittest.TestCase):
    def test_parse_http_date(self) -> None:
        unix = parse_http_date("Thu, 28 May 2026 01:00:00 GMT")
        self.assertEqual(datetime.fromtimestamp(unix, UTC).year, 2026)

    def test_parse_time_only_keeps_future_time_today(self) -> None:
        local_tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=local_tz).timestamp()
        target = parse_target_time("12:00:00.500", now)
        self.assertAlmostEqual(target - now, 0.5, places=3)

    def test_parse_time_only_rolls_past_time_forward(self) -> None:
        local_tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=local_tz).timestamp()
        target = parse_target_time("11:59:59.500", now)
        self.assertAlmostEqual(target - now, 86399.5, places=3)

    def test_parse_explicit_local_datetime(self) -> None:
        local_tz = datetime.now().astimezone().tzinfo
        expected = datetime(2026, 5, 28, 20, 0, 0, 250000, tzinfo=local_tz)
        target = parse_target_time("2026-05-28 20:00:00.250", time.time())
        self.assertAlmostEqual(target, expected.timestamp(), places=3)

    def test_parse_explicit_kst_datetime(self) -> None:
        expected = datetime(2026, 5, 28, 20, 0, 0, 250000, tzinfo=KST)
        target = parse_target_time(
            "2026-05-28 20:00:00.250",
            time.time(),
            target_tz=KST,
        )
        self.assertAlmostEqual(target, expected.timestamp(), places=3)

    def test_fetch_server_date_reads_date_header(self) -> None:
        def opener(request, timeout):
            self.assertEqual(request.get_method(), "HEAD")
            self.assertEqual(timeout, 1.0)
            return SimpleNamespace(headers={"Date": "Thu, 28 May 2026 01:00:00 GMT"})

        sample = fetch_server_date("https://example.com/", timeout_seconds=1.0, opener=opener)
        self.assertEqual(sample.raw_date, "Thu, 28 May 2026 01:00:00 GMT")
        self.assertGreaterEqual(sample.rtt_seconds, 0)

    def test_server_clock_uses_median_of_best_rtt_samples(self) -> None:
        samples = [
            TimeSample(0.100, 0.050, 0, 0, "a"),
            TimeSample(0.200, 0.010, 0, 0, "b"),
            TimeSample(0.300, 0.020, 0, 0, "c"),
            TimeSample(9.999, 0.500, 0, 0, "slow"),
        ]

        with patch("core.clock.fetch_server_date", side_effect=samples):
            result = ServerClock().sync(
                "https://example.com/",
                sample_count=4,
                keep_best=3,
                pause_seconds=0,
            )

        self.assertEqual(result.best_rtt_seconds, 0.010)
        self.assertEqual(result.offset_seconds, 0.200)

    def test_server_clock_tick_sync_anchors_on_date_rollover(self) -> None:
        dates = iter(
            [
                "Thu, 28 May 2026 01:00:00 GMT",
                "Thu, 28 May 2026 01:00:00 GMT",
                "Thu, 28 May 2026 01:00:01 GMT",
                "Thu, 28 May 2026 01:00:01 GMT",
                "Thu, 28 May 2026 01:00:02 GMT",
            ]
        )

        def opener(request, timeout):
            return SimpleNamespace(headers={"Date": next(dates)})

        result = ServerClock().sync_tick(
            "https://example.com/",
            sample_count=2,
            min_samples=2,
            max_wait_seconds=1.0,
            poll_seconds=0,
            opener=opener,
        )

        self.assertEqual(result.mode, "tick")
        self.assertEqual(len(result.samples), 2)
        self.assertIn(
            datetime.fromtimestamp(result.anchor_server_unix, UTC).second,
            {1, 2},
        )


class SleepDriftTests(unittest.TestCase):
    """A clock held across a sleep is not a clock.

    Observed live: a panel left running from 20:45 across 23 hours of machine
    sleep had a clock 19 hours behind, so the 오픈 대기 countdown was still
    counting down to an open that had already happened — while the note beside
    it, which reads datetime.now(), correctly said 판매 중. Same panel, two
    clocks. perf_counter is mach_absolute_time() on macOS and does not advance
    while the machine sleeps, which is what separates them.
    """

    def _slept(self, hours: float) -> ServerClock:
        clock = ServerClock()
        now = time.time()
        # A sync whose wall anchor is `hours` old but whose monotonic anchor has
        # barely moved: exactly what a sleep leaves behind.
        clock._sync_result = SyncResult(
            offset_seconds=0.0,
            best_rtt_seconds=0.01,
            samples=(),
            anchor_perf=time.perf_counter(),
            anchor_server_unix=now - hours * 3600,
            anchor_wall_unix=now - hours * 3600,
        )
        return clock

    def test_a_sleep_is_detected(self) -> None:
        clock = self._slept(19)
        self.assertAlmostEqual(clock.sync_result.anchor_drift_seconds / 3600, 19, places=1)
        self.assertTrue(clock.anchor_is_stale())

    def test_a_stale_anchor_is_not_used_for_the_time(self) -> None:
        clock = self._slept(19)
        # Without the staleness check this reads 19 hours in the past, which is
        # what put a live countdown on a show that had already opened.
        self.assertLess(abs(clock.server_time_unix() - time.time()), 2.0)

    def test_a_stale_anchor_is_not_used_for_a_deadline(self) -> None:
        clock = self._slept(19)
        target = time.time() + 30.0
        deadline = clock.deadline_for_server_time(target)
        # The arm fires against this. A stale anchor made it hours late.
        self.assertLess(abs(deadline - (time.perf_counter() + 30.0)), 2.0)

    def test_a_fresh_anchor_is_still_trusted(self) -> None:
        clock = ServerClock()
        now = time.time()
        clock._sync_result = SyncResult(
            offset_seconds=0.25, best_rtt_seconds=0.01, samples=(),
            anchor_perf=time.perf_counter(), anchor_server_unix=now + 0.25,
            anchor_wall_unix=now,
        )
        self.assertFalse(clock.anchor_is_stale())
        # The monotonic path is the precise one and must stay in use.
        self.assertLess(abs(clock.server_time_unix() - (now + 0.25)), 0.5)

    def test_age_is_measured_in_real_time(self) -> None:
        clock = self._slept(19)
        # perf_counter-based age reported a 19-hour-old sync as seconds old —
        # on the reading whose whole job is to say "too old to trust".
        self.assertGreater(clock.sync_result.age_seconds, 18 * 3600)


if __name__ == "__main__":
    unittest.main()
