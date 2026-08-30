"""Does the whole-venue remaining count track the seat bitmap?

The catch loop's detection cost is one request per two blocks, so answering
"did anything free anywhere?" on a 34-block venue takes 17 requests and about
4.4 seconds. The remaining-seat feed answers the same question in one 132ms
call. Using it as a trigger is only sound if it moves with the bitmap rather
than being batched or cached server-side, and that is the thing to establish
before building on it.

Two checks, both read-only:

  agreement  — does `remainCnt` equal the number of free bits in seatStatus,
               right now, for the same round? A standing offset means it counts
               something else and cannot be compared against itself over time.
  tracking   — polled together over a while, do they move at the same moment?
               A lag here is the trigger's latency, and it has to be well under
               a sweep to be worth anything.

Usage:  python3 research/probes/probe_remains_vs_bitmap.py [goodsCode] [playDate] [seconds]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pureclick_seat_core import parse_seat_status  # noqa: E402
from pureclick_showinfo import BROWSER_UA, fetch_round_remains, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": f"{ORIGIN}/onestop/seat",
}
# seatStatus answers HTTP 400 above two blockKeys — measured, not assumed.
KEYS_PER_CALL = 2


def _get(path: str, params: list[tuple[str, str]]) -> object:
    url = ORIGIN + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def block_keys(goods: str, place: str, seq: str) -> list[str]:
    """The round's blocks, discovered through seatMeta.

    Block keys embed the round: the same venue is 001:001 on one round and
    096:001 on another, and seatStatus keys off the blockKey's own round field
    rather than the playSeq parameter. Asking for 001:001 while passing
    playSeq=096 answers with round 001's bitmap and no error — which is how
    this probe first "found" that the two sources disagreed by 463 seats.

    /onestop/api/seats/block-data would list them but answers HTTP 400 without
    a booking session, which this probe deliberately does not have. seatMeta
    needs none, so walk the block numbers until one comes back empty.
    """
    keys: list[str] = []
    for index in range(1, 64):
        key = f"{seq}:{index:03d}"
        params = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq)]
        params.append(("blockKeys", key))
        payload = _get("/onestop/api/seatMeta", params)
        seats = sum(len(row.get("seats") or []) for row in payload) if isinstance(payload, list) else 0
        if not seats:
            break
        keys.append(key)
    return keys


def free_bits(goods: str, place: str, seq: str, keys: list[str]) -> tuple[int, int]:
    """(free seats across the venue, requests spent)."""
    total = requests = 0
    for index in range(0, len(keys), KEYS_PER_CALL):
        batch = keys[index : index + KEYS_PER_CALL]
        params = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]
        params += [("blockKeys", key) for key in batch]
        total += sum(sum(mask) for mask in parse_seat_status(_get("/onestop/api/seatStatus", params)))
        requests += 1
    return total, requests


def main() -> int:
    goods = sys.argv[1] if len(sys.argv) > 1 else "26005128"
    play_date = sys.argv[2] if len(sys.argv) > 2 else ""
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0

    catalog = fetch_show_catalog(goods)
    play_date = play_date or catalog.play_end_date
    grades, seq, play_time = fetch_round_remains(goods, play_date, place_code=catalog.place_code)
    remains = sum(grade.remain for grade in grades)
    keys = block_keys(goods, catalog.place_code, seq)
    print(f"{goods} {catalog.place_name} · {play_date} {play_time} 회차 {seq}")
    print(f"{len(keys)} blocks → {-(-len(keys) // KEYS_PER_CALL)} seatStatus requests per sweep\n")

    started = time.perf_counter()
    bits, spent = free_bits(goods, catalog.place_code, seq, keys)
    sweep_ms = (time.perf_counter() - started) * 1000
    print("agreement  (these must match, or a change in one says nothing about the other)")
    print(f"  remainCnt total   {remains}")
    print(f"  free bits total   {bits}")
    print(f"  difference        {bits - remains}")
    if remains == 0 and bits:
        print("  NOTE: remainCnt is 0 for a round that is not on sale, while the map")
        print("        still holds seats. A trigger must not read 0 as sold out.")
    print(f"  one sweep         {spent} requests, {sweep_ms:.0f} ms\n")

    print(f"tracking (for {seconds:.0f}s — a change in either column is what matters)")
    print(f"  {'t':>6} {'remain':>8} {'bits':>8} {'remain ms':>10}")
    deadline = time.perf_counter() + seconds
    last = (remains, bits)
    moves = 0
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        try:
            grades, _, _ = fetch_round_remains(goods, play_date, place_code=catalog.place_code, play_seq=seq)
            remains = sum(grade.remain for grade in grades)
        except Exception as error:  # noqa: BLE001
            print(f"  remains failed: {error}")
            break
        remain_ms = (time.perf_counter() - t0) * 1000
        bits, _ = free_bits(goods, catalog.place_code, seq, keys)
        moved = (remains, bits) != last
        if moved:
            moves += 1
        print(
            f"  {time.perf_counter() - (deadline - seconds):>6.1f} {remains:>8} {bits:>8}"
            f" {remain_ms:>10.0f}{'   <- moved' if moved else ''}"
        )
        last = (remains, bits)
        time.sleep(2.0)

    print(f"\n{moves} change(s) seen. No changes means a quiet show, not a working trigger —")
    print("re-run against a show that is actually selling, or release a held seat by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
