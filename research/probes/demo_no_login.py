"""Demonstrate that seat layout and live availability need no login.

Sends no cookies and no auth header, then prints named seats and which ones are
free right now, so the output can be checked against the logged-in seat map.
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pureclick_showinfo import BROWSER_UA, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"

# Explicitly refuse to store or send cookies so the result cannot be explained
# away by an incidental session.
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def call(path: str, params: list[tuple[str, str]]):
    request = urllib.request.Request(
        ORIGIN + path + "?" + urllib.parse.urlencode(params),
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
            "Referer": f"{ORIGIN}/onestop/seat",
        },
    )
    with _opener.open(request, timeout=20) as response:
        sent_cookie = request.get_header("Cookie")
        return json.loads(response.read().decode("utf-8", "replace")), sent_cookie


def bits(hex_string: str) -> list[bool]:
    out: list[bool] = []
    for char in str(hex_string or ""):
        try:
            value = int(char, 16)
        except ValueError:
            continue
        out.extend(bool((value >> s) & 1) for s in (3, 2, 1, 0))
    return out


def main() -> int:
    goods = sys.argv[1] if len(sys.argv) > 1 else "26005128"
    blocks = sys.argv[2:] or ["001:001", "002:002", "003:003"]

    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    print(f"{catalog.goods_name}  @ {catalog.place_name}")
    print(f"goodsCode={goods} placeCode={place} playSeq={seq}")
    print(f"engine={catalog.flow}\n")

    grand_total = grand_free = 0
    by_grade: Counter = Counter()
    samples: list[str] = []

    for block in blocks:
        base = [
            ("goodsCode", goods), ("placeCode", place),
            ("playSeq", seq), ("bizCode", "WEBBR"), ("blockKeys", block),
        ]
        meta, cookie_sent = call("/onestop/api/seatMeta", base)
        status, _ = call("/onestop/api/seatStatus", base)

        seats = (meta or [{}])[0].get("seats") or []
        data = status.get("data") if isinstance(status, dict) else status
        mask = bits(data[0] if isinstance(data, list) and data else "")

        free = 0
        for index, seat in enumerate(seats):
            if not seat.get("isExposable"):
                continue
            if not (index < len(mask) and mask[index]):
                continue
            free += 1
            by_grade[seat.get("seatGradeName") or "?"] += 1
            if len(samples) < 12:
                samples.append(
                    f"    {seat.get('seatGradeName'):<8} {seat.get('floor') or '-':<6} "
                    f"{seat.get('rowNo'):<12} {seat.get('seatNo'):>4}번   "
                    f"{(seat.get('salesPrice') or 0):>8,}원   {seat.get('seatInfoId')}"
                )
        grand_total += len(seats)
        grand_free += free
        print(f"  block {block}: {len(seats):>4} seats, {free:>4} free right now   "
              f"(Cookie header sent: {cookie_sent!r})")

    print(f"\n총 {grand_total} seats scanned, {grand_free} free")
    print(f"free by grade: {dict(by_grade)}\n")
    print("sample of seats that are bookable at this moment:")
    print(f"    {'grade':<8} {'floor':<6} {'row':<12} {'seat':>4}   {'price':>8}   seatInfoId")
    for line in samples:
        print(line)
    print("\nOpen the same show logged in and compare — these should match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
