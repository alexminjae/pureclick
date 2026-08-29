"""Measure how fresh /onestop/api/seatStatus really is.

Polling faster than the server's cache window buys nothing, so before tuning a
catch loop we need the cache headers and the observed round-trip cost.
"""

from __future__ import annotations

import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pureclick_showinfo import BROWSER_UA, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": f"{ORIGIN}/onestop/seat",
}
INTERESTING = (
    "cache-control", "age", "etag", "expires", "x-cache", "cf-cache-status",
    "vary", "server", "date", "content-length",
)


def main() -> int:
    goods = sys.argv[1] if len(sys.argv) > 1 else "26005128"
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    query = urllib.parse.urlencode(
        [
            ("goodsCode", goods), ("placeCode", place), ("playSeq", seq),
            ("bizCode", "WEBBR"), ("blockKeys", "001:001"),
        ]
    )
    url = f"{ORIGIN}/onestop/api/seatStatus?{query}"
    print(f"{goods} {catalog.place_name}\n{url}\n")

    latencies: list[float] = []
    bodies: list[str] = []
    for attempt in range(8):
        request = urllib.request.Request(url, headers=HEADERS)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8", "replace")
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                bodies.append(body)
                if attempt == 0:
                    print("response headers:")
                    for key, value in response.getheaders():
                        if key.lower() in INTERESTING:
                            print(f"  {key}: {value}")
                    print()
                print(f"  poll {attempt}: {elapsed:6.1f} ms  bytes={len(body)}")
        except Exception as error:  # noqa: BLE001
            print(f"  poll {attempt}: failed {error}")
        time.sleep(0.25)

    if latencies:
        print(
            f"\nlatency: min={min(latencies):.0f}ms median={statistics.median(latencies):.0f}ms "
            f"max={max(latencies):.0f}ms"
        )
    print(f"distinct payloads across {len(bodies)} polls: {len(set(bodies))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
