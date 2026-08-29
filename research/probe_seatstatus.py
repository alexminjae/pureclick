"""Compare seatMeta against seatStatus to locate the real availability signal.

seatMeta looks like a static layout description (positions, rows, grades) while
seatStatus looks like the live availability overlay. Which one actually says a
seat is takeable decides whether the picker should trust `isExposable`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pureclick_showinfo import BROWSER_UA, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": f"{ORIGIN}/onestop/seat",
}


def call(path: str, params: list[tuple[str, str]]):
    url = ORIGIN + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return error.code, error.read()[:200].decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        return None, str(error)[:160]


def inspect(goods: str, blocks: list[str]) -> None:
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    remain = sum(grade.remain for grade in catalog.grades)
    print(f"\n### {goods} {catalog.place_name}  remain(api)={remain}  flow={catalog.flow}")

    base = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]

    for block in blocks:
        meta_status, meta = call("/onestop/api/seatMeta", base + [("blockKeys", block)])
        seats = (meta or [{}])[0].get("seats") if isinstance(meta, list) and meta else []
        seats = seats or []
        exposable = sum(1 for seat in seats if seat.get("isExposable"))
        graded = sum(1 for seat in seats if seat.get("seatGrade"))
        priced = sum(1 for seat in seats if seat.get("salesPrice"))

        stat_status, stat = call("/onestop/api/seatStatus", base + [("blockKeys", block)])
        summary = ""
        if isinstance(stat, list) and stat:
            entry = stat[0]
            if isinstance(entry, dict):
                keys = list(entry.keys())
                rows = entry.get("seats") or entry.get("seatStatus") or []
                counter = Counter()
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict):
                        for field in ("status", "seatStatus", "state", "isSold", "isSelectable"):
                            if field in row:
                                counter[f"{field}={row[field]}"] += 1
                                break
                summary = f"keys={keys[:6]} rows={len(rows) if isinstance(rows, list) else '?'} {dict(counter.most_common(5))}"
        elif isinstance(stat, dict):
            summary = f"dict keys={list(stat.keys())[:8]}"
        else:
            summary = str(stat)[:120]

        print(f"  block {block}: meta[{meta_status}] seats={len(seats)} exposable={exposable} graded={graded} priced={priced}")
        print(f"                 status[{stat_status}] {summary}")
        if seats and not exposable:
            print(f"                 meta sample: {json.dumps(seats[0], ensure_ascii=False)[:220]}")
        if isinstance(stat, list) and stat:
            print(f"                 status raw:  {json.dumps(stat[0], ensure_ascii=False)[:260]}")


def main() -> int:
    inspect("26012391", ["002:002", "003:003"])
    inspect("26005128", ["001:001", "002:002"])
    inspect("26011315", ["001:001"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
