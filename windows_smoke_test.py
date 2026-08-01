from __future__ import annotations

import argparse
import statistics
import platform
import time

from pureclick_core import ServerClock, WindowsClicker, precise_wait_until


def main() -> int:
    parser = argparse.ArgumentParser(description="PureClick Windows handoff smoke test")
    parser.add_argument("--url", default="https://poticket.interpark.com/Book/BookMain.asp")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument(
        "--click-test",
        action="store_true",
        help="Move to the current cursor position and send one real click after 3 seconds.",
    )
    parser.add_argument(
        "--capture-test",
        action="store_true",
        help="Benchmark screen capture speed for the Phase 2 watcher (Windows only).",
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

    if args.capture_test:
        if platform.system() != "Windows":
            print("Screen capture test is only available on Windows.")
            return 1
        from pureclick_watch_core import WatchRegion, WindowsScreenGrabber, grid_from_bgra

        region = WatchRegion(left=0, top=0, width=800, height=600)
        grabber = WindowsScreenGrabber()
        try:
            grabber.grab(region)  # warm up cached GDI resources
            frame_times: list[float] = []
            for _ in range(30):
                started = time.perf_counter()
                data = grabber.grab(region)
                grid_from_bgra(data, region.width, region.height, stride=2)
                frame_times.append((time.perf_counter() - started) * 1000)
            print(f"Capture+grid median: {statistics.median(frame_times):.1f} ms")
            print(f"Capture+grid worst: {max(frame_times):.1f} ms")
            print(f"Watch loop can sustain ~{1000 / statistics.median(frame_times):.0f} fps")
        finally:
            grabber.close()

    print("Smoke test complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
