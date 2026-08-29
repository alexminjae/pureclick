from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from pureclick_core import (
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

        with patch("pureclick_core.fetch_server_date", side_effect=samples):
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


if __name__ == "__main__":
    unittest.main()
