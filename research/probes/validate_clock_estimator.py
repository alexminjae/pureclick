"""Validate the max-of-samples clock estimator against boundary detection.

A single Date reading understates the true offset by the fractional part of the
current second, so the maximum across many samples converges on the truth. This
compares that estimator with `ServerClock.sync_tick`, which brackets the actual
second boundary and is the reference.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pureclick_core import ServerClock, fetch_server_date  # noqa: E402

SYNC_URL = "https://poticket.interpark.com/Book/BookMain.asp"


def collect(samples: int) -> list[float]:
    offsets: list[float] = []
    for _ in range(samples):
        try:
            offsets.append(fetch_server_date(SYNC_URL, timeout_seconds=2.0).offset_seconds)
        except Exception:  # noqa: BLE001
            continue
    return offsets


def main() -> int:
    clock = ServerClock()
    reference = clock.sync_tick(SYNC_URL, sample_count=5, min_samples=3, max_wait_seconds=12.0, poll_seconds=0.005)
    print(f"reference (boundary bracketing): offset = {reference.offset_seconds:+.4f}s")
    print(f"  best rtt = {reference.best_rtt_seconds * 1000:.0f}ms\n")

    for count in (5, 10, 20, 40):
        offsets = collect(count)
        if not offsets:
            print(f"  n={count:<3} no samples")
            continue
        naive = offsets[0]
        best = max(offsets)
        print(
            f"  n={count:<3} max={best:+.4f}s (err {abs(best - reference.offset_seconds) * 1000:6.1f}ms)   "
            f"median={statistics.median(offsets):+.4f}s "
            f"(err {abs(statistics.median(offsets) - reference.offset_seconds) * 1000:6.1f}ms)   "
            f"single={naive:+.4f}s (err {abs(naive - reference.offset_seconds) * 1000:6.1f}ms)"
        )
        time.sleep(0.2)

    print("\nLower error is better. 'single' is what the browser used to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
