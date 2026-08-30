"""Dump and compare real seat-map structure across structurally different venues.

`/onestop/api/seatMeta` answers without a booking session, so actual layouts can
be inspected directly instead of inferred from one show. This walks blockKeys for
each target venue and reports field coverage, grade population, group usage and
row/seat naming so the picker can be validated against reality.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pureclick_showinfo import BROWSER_UA, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": f"{ORIGIN}/onestop/seat",
}
OUTDIR = Path(__file__).resolve().parent.parent / "seatmaps"

TARGETS = [
    ("26012391", "소극장 화성예술의전당"),
    ("26011315", "아레나 킨텍스"),
    ("26012258", "콘서트홀 예술의전당"),
    ("26004566", "야구장 잠실"),
    ("26005128", "대극장 극장용"),
    ("26011375", "체육관 장충 ROADFC"),
    ("24014659", "전용관 터치파이브"),
]


def get_json(path: str, timeout: float = 15.0):
    request = urllib.request.Request(ORIGIN + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def seat_meta(goods: str, place: str, seq: str, block_keys: list[str]):
    params = [
        ("goodsCode", goods),
        ("placeCode", place),
        ("playSeq", seq),
        ("bizCode", "WEBBR"),
    ] + [("blockKeys", key) for key in block_keys]
    return get_json("/onestop/api/seatMeta?" + urllib.parse.urlencode(params))


def discover_blocks(goods: str, place: str, seq: str, limit: int = 60) -> list[dict]:
    """Walk `NNN:NNN` block keys until they stop returning seats.

    block-data needs a session, but seatMeta will answer for any key, so the
    block list can be recovered by probing the numbering space directly.
    """
    found: list[dict] = []
    misses = 0
    for index in range(1, limit + 1):
        key = f"{index:03d}:{index:03d}"
        blocks = seat_meta(goods, place, seq, [key]) or []
        seats = blocks[0].get("seats") if blocks and isinstance(blocks[0], dict) else None
        if seats:
            found.append({"blockKey": key, "seats": seats})
            misses = 0
        else:
            misses += 1
            if misses >= 6 and found:
                break
    return found


def summarize(name: str, goods: str, blocks: list[dict]) -> dict:
    seats = [seat for block in blocks for seat in block["seats"]]
    fields = Counter()
    for seat in seats:
        for key, value in seat.items():
            if value not in (None, "", []):
                fields[key] += 1
    grades = Counter(str(seat.get("seatGradeName") or seat.get("seatGrade") or "NULL") for seat in seats)
    return {
        "name": name,
        "goodsCode": goods,
        "blocks": len(blocks),
        "seats": len(seats),
        "exposable": sum(1 for seat in seats if seat.get("isExposable")),
        "graded": sum(1 for seat in seats if seat.get("seatGrade")),
        "grouped": sum(1 for seat in seats if seat.get("seatGroupId")),
        "grades": dict(grades.most_common(8)),
        "populated_fields": dict(fields.most_common(20)),
        "sample": seats[0] if seats else None,
    }


def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    def run(target: tuple[str, str]) -> dict | None:
        goods, name = target
        try:
            catalog = fetch_show_catalog(goods)
        except Exception:  # noqa: BLE001
            return None
        seq = (catalog.play_seqs or ["001"])[0]
        blocks = discover_blocks(goods, catalog.place_code, seq)
        if not blocks:
            return {"name": name, "goodsCode": goods, "blocks": 0, "seats": 0}
        # Never slice the encoded JSON. A character cap cuts mid-token and
        # writes a file that can never be parsed — which is exactly what
        # happened to 아레나 킨텍스 (21,460 seats), the one venue big enough to
        # hit it and the most interesting one in the set. It sat committed and
        # unreadable. If these ever need bounding, bound the seats, not the
        # string.
        (OUTDIR / f"{goods}.json").write_text(
            json.dumps(blocks, ensure_ascii=False), encoding="utf-8"
        )
        return summarize(name, goods, blocks)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [item for item in pool.map(run, TARGETS) if item]

    (OUTDIR / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for item in results:
        print(f"\n### {item['name']} ({item['goodsCode']})")
        print(f"  blocks={item['blocks']} seats={item['seats']} "
              f"exposable={item.get('exposable')} graded={item.get('graded')} grouped={item.get('grouped')}")
        print(f"  grades: {item.get('grades')}")
        if item.get("sample"):
            print(f"  sample: {json.dumps(item['sample'], ensure_ascii=False)[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
