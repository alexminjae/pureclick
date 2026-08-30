"""How many on-sale shows expose their seat map without a login?

The unauthenticated read works on some products and returns an ungraded skeleton
on others, so the planning features can only be promised where it actually
works. This samples the live catalogue and reports the split.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pureclick_catalog import GENRES, list_all_products  # noqa: E402
from pureclick_showinfo import BROWSER_UA, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": f"{ORIGIN}/onestop/seat",
}
PROBE_BLOCKS = ["001:001", "002:002"]


def call(path: str, params):
    request = urllib.request.Request(
        ORIGIN + path + "?" + urllib.parse.urlencode(params), headers=HEADERS
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def bits(hex_string: str):
    out = []
    for char in str(hex_string or ""):
        try:
            value = int(char, 16)
        except ValueError:
            continue
        out.extend(bool((value >> s) & 1) for s in (3, 2, 1, 0))
    return out


def probe(goods: str):
    try:
        catalog = fetch_show_catalog(goods)
    except Exception:  # noqa: BLE001
        return None
    if catalog.flow not in {"onestop-reserved", "onestop-sports"}:
        return None

    seats = graded = free = 0
    for block in PROBE_BLOCKS:
        base = [
            ("goodsCode", goods), ("placeCode", catalog.place_code),
            ("playSeq", (catalog.play_seqs or ["001"])[0]),
            ("bizCode", "WEBBR"), ("blockKeys", block),
        ]
        meta = call("/onestop/api/seatMeta", base)
        rows = (meta or [{}])[0].get("seats") if isinstance(meta, list) and meta else []
        rows = rows or []
        if not rows:
            continue
        status = call("/onestop/api/seatStatus", base)
        data = status.get("data") if isinstance(status, dict) else status
        mask = bits(data[0] if isinstance(data, list) and data else "")
        seats += len(rows)
        for index, seat in enumerate(rows):
            if seat.get("seatGrade"):
                graded += 1
            if seat.get("isExposable") and index < len(mask) and mask[index]:
                free += 1

    remain = sum(grade.remain for grade in catalog.grades)
    return {
        "goods": goods,
        "name": catalog.goods_name[:34],
        "flow": catalog.flow,
        "seats": seats,
        "graded": graded,
        "free": free,
        "remain_api": remain,
    }


def main() -> int:
    codes = [code for codes in list_all_products().values() for code in codes]
    print(f"probing {len(codes)} catalogue entries (2 blocks each)\n")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [item for item in pool.map(probe, codes) if item]

    usable = [r for r in results if r["graded"] > 0]
    skeleton = [r for r in results if r["seats"] > 0 and r["graded"] == 0]
    empty = [r for r in results if r["seats"] == 0]

    print(f"seat-pickable shows probed : {len(results)}")
    print(f"  full data without login  : {len(usable)}")
    print(f"  ungraded skeleton only   : {len(skeleton)}")
    print(f"  no seat data             : {len(empty)}\n")

    print("full data (first 12):")
    for row in usable[:12]:
        print(f"  {row['goods']:<10} seats={row['seats']:<5} free={row['free']:<5} "
              f"remain_api={row['remain_api']:<6} {row['name']}")
    if skeleton:
        print("\nskeleton only (first 8):")
        for row in skeleton[:8]:
            print(f"  {row['goods']:<10} seats={row['seats']:<5} remain_api={row['remain_api']:<6} {row['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
