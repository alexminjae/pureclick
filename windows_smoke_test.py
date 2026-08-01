from __future__ import annotations

import argparse
import statistics
import platform
import time

from pureclick_core import ServerClock, WindowsClicker, precise_wait_until


def main() -> int:
    parser = argparse.ArgumentParser(description="PureClick Windows handoff smoke test")
    parser.add_argument("--url", default="https://ticket.interpark.com/")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument(
        "--click-test",
        action="store_true",
        help="Move to the current cursor position and send one real click after 3 seconds.",
    )
    args = parser.parse_args()

    print(f"Platform: {platform.platform()}")
    print(f"Testing local precision wait across {args.runs} runs...")
    lateness_values: list[float] = []
    for _ in range(max(1, args.runs)):
        deadline = time.perf_counter() + max(0.01, args.interval)
        precise_wait_until(deadline)
        lateness_values.append((time.perf_counter() - deadline) * 1000)
    print(f"Local wait median: {statistics.median(lateness_values):.3f} ms")
    print(f"Local wait worst: {max(lateness_values):.3f} ms")
    if len(lateness_values) > 1:
        print(f"Local wait jitter: {statistics.pstdev(lateness_values):.3f} ms")

    if not args.skip_sync:
        print(f"Catching server time ticks from {args.url}...")
        clock = ServerClock()
        result = clock.sync_tick(args.url, sample_count=args.samples)
        print(f"Sync mode: {result.mode}")
        print(f"Offset: {result.offset_seconds * 1000:+.1f} ms")
        print(f"Best RTT: {result.best_rtt_seconds * 1000:.1f} ms")
        print(f"Jitter: {result.jitter_seconds * 1000:.1f} ms")

    if args.click_test:
        if platform.system() != "Windows":
            print("Real click test is only available on Windows.")
            return 1
        clicker = WindowsClicker()
        x, y = clicker.cursor_position()
        print(f"Clicking current cursor position {x}, {y} in 3 seconds...")
        deadline = time.perf_counter() + 3.0
        precise_wait_until(deadline)
        clicker.click(x, y)
        print("Click sent.")

    print("Smoke test complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
