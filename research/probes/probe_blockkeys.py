"""Map the real blockKey space for a venue via seatMeta.

block-data needs a booking session, but seatMeta answers for any key, so the
block numbering can be recovered by scanning a 2-D slice of `AAA:BBB` rather
than assuming the diagonal.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pureclick_showinfo import BROWSER_UA, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": f"{ORIGIN}/onestop/seat",
}
BATCH = 12


def seat_meta(goods: str, place: str, seq: str, keys: list[str]):
    params = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]
    params += [("blockKeys", key) for key in keys]
    request = urllib.request.Request(
        ORIGIN + "/onestop/api/seatMeta?" + urllib.parse.urlencode(params), headers=HEADERS
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def scan(goods: str, outer: int = 8, inner: int = 14) -> None:
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    print(f"\n### {goods} {catalog.place_name} place={place} seq={seq}")

    keys = [f"{a:03d}:{b:03d}" for a in range(1, outer + 1) for b in range(1, inner + 1)]
    hits: list[tuple[str, int, int]] = []
    for index in range(0, len(keys), BATCH):
        for block in seat_meta(goods, place, seq, keys[index : index + BATCH]) or []:
            seats = block.get("seats") or []
            if seats:
                exposable = sum(1 for seat in seats if seat.get("isExposable"))
                hits.append((block.get("blockKey"), len(seats), exposable))

    total = sum(count for _, count, _ in hits)
    live = sum(count for _, _, count in hits)
    print(f"  non-empty blocks: {len(hits)}  seats: {total}  exposable: {live}")
    for key, count, exposable in hits[:20]:
        print(f"    {key}  seats={count:<6} exposable={exposable}")


def main() -> int:
    for goods in sys.argv[1:] or ["26012258", "26004566", "26012391"]:
        scan(goods)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
