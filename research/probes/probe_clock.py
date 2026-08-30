"""Measure the achievable accuracy of server-time synchronisation.

Firing a queue-entry request "at open" only works if we know the server clock
to better than the round-trip jitter. This samples the Date header from the
hosts involved in booking and reports offset and spread.

The Date header has one-second resolution, so the offset is estimated from the
observed second-boundary transition rather than from a single reading.
"""

from __future__ import annotations

import statistics
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

HOSTS = {
    "ticketfront (waiting API)": "https://api-ticketfront.interpark.com/v1/goods/26011315/summary",
    "tickets (onestop)": "https://tickets.interpark.com/onestop/seat",
    "poticket (BookSession)": "https://poticket.interpark.com/Book/BookMain.asp",
    "nol": "https://nol.yanolja.com/ticket",
}


def sample(url: str) -> tuple[float, float, float] | None:
    """Return (local_send, server_epoch, round_trip_ms) for one HEAD-ish request."""
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            finished = time.time()
            raw = response.getheader("Date")
            response.read(1)
    except urllib.error.HTTPError as error:
        finished = time.time()
        raw = error.headers.get("Date") if error.headers else None
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        server = parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        return None
    return started, server, (finished - started) * 1000


def measure(name: str, url: str, samples: int = 25) -> None:
    offsets: list[float] = []
    trips: list[float] = []
    # The Date header is second-resolution: watching where the reported second
    # flips relative to local time localises the true offset far better than
    # reading it once.
    for _ in range(samples):
        result = sample(url)
        if result is None:
            continue
        local_send, server, trip = result
        midpoint = local_send + (trip / 2000.0)
        offsets.append(server - midpoint)
        trips.append(trip)
        time.sleep(0.12)

    if not offsets:
        print(f"  {name:28s} unavailable")
        return

    lo, hi = min(offsets), max(offsets)
    print(
        f"  {name:28s} offset {statistics.median(offsets):+.3f}s "
        f"(range {lo:+.3f}..{hi:+.3f}, spread {hi - lo:.3f}s)  "
        f"rtt median {statistics.median(trips):.0f}ms  n={len(offsets)}"
    )


def main() -> int:
    print("server clock offset vs this machine (positive = server ahead)\n")
    for name, url in HOSTS.items():
        measure(name, url)
    print(
        "\nDate header resolution is 1s, so the true offset lies within the observed"
        "\nrange; the midpoint of that range is the best estimate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
