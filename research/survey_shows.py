"""Survey live NOL/Interpark shows to measure how much seat-layout variety exists.

Pulls the realtime ranking feed, then fetches the public summary + price-group +
remaining-seat endpoints for each show and classifies the booking flow. Output is
written to research/show_survey.json so the seat targeting logic can be validated
against real structural diversity instead of a single arena concert.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api-ticketfront.interpark.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://tickets.interpark.com/",
}
OUT = Path(__file__).resolve().parent / "show_survey.json"


def get(path: str, timeout: float = 15.0):
    request = urllib.request.Request(API + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def ranking_codes() -> list[dict]:
    payload = get("/v1/ranking/realtime")
    data = (payload or {}).get("data") or {}
    seen: dict[str, dict] = {}
    for genre_bucket, rows in data.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            code = str(row.get("goodsCode") or "")
            if not code or code in seen:
                continue
            seen[code] = {
                "goodsCode": code,
                "bucket": genre_bucket,
                "goodsName": row.get("goodsName"),
                "placeName": row.get("placeName"),
                "genreCode": row.get("genreCode"),
            }
    return list(seen.values())


def classify(summary: dict) -> str:
    if not summary.get("isIngredientOnestop"):
        return "legacy-poticket"
    if summary.get("isSportOneStop"):
        return "onestop-sports"
    if not summary.get("isReservedSeat"):
        return "onestop-general-admission"
    return "onestop-reserved"


def probe(entry: dict) -> dict:
    code = entry["goodsCode"]
    result = dict(entry)

    summary = (get(f"/v1/goods/{code}/summary") or {}).get("data") or {}
    if not summary:
        result["error"] = "no summary"
        return result

    quality = summary.get("goodsQualityList") or ""
    result.update(
        {
            "goodsName": summary.get("goodsName") or entry.get("goodsName"),
            "placeName": summary.get("placeName"),
            "placeCode": summary.get("placeCode"),
            "genreCode": summary.get("genreCode"),
            "genreName": summary.get("genreName"),
            "isIngredientOnestop": bool(summary.get("isIngredientOnestop")),
            "isReservedSeat": bool(summary.get("isReservedSeat")),
            "isSportOneStop": bool(summary.get("isSportOneStop")),
            "isCaptcha": bool(summary.get("isCaptcha")),
            "isPaymentSeparate": bool(summary.get("isPaymentSeparate")),
            "playStartDate": summary.get("playStartDate"),
            "playEndDate": summary.get("playEndDate"),
            "ticketOpenDate": summary.get("ticketOpenDate"),
            "hideRemainSeat": "C5021" in quality,
            "hasWaitingFlag": "C5027" in quality,
            "flow": classify(summary),
        }
    )

    prices = (get(f"/v1/goods/{code}/prices/group") or {}).get("data") or {}
    grades = []
    rows = prices if isinstance(prices, list) else prices.get("priceGrade") or prices.get("prices") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        grades.append(
            {
                "seatGrade": row.get("seatGrade") or row.get("seatGradeCode"),
                "name": row.get("seatGradeName") or row.get("name") or row.get("priceGradeName"),
                "price": row.get("salesPrice") or row.get("price"),
            }
        )
    result["grade_count"] = len(grades)
    result["grades"] = grades[:40]

    play_start = summary.get("playStartDate")
    if play_start:
        remain = (get(f"/v1/goods/{code}/playSeq/PlayDate/{play_start}/ALL") or {}).get("data") or {}
        seats = remain.get("remainSeat") or []
        result["remain_rows"] = len(seats)
        result["remain_sample"] = seats[:8]
        result["playSeq_count"] = len({str(s.get("playSeq")) for s in seats if isinstance(s, dict)})
    return result


def main() -> int:
    entries = ranking_codes()
    print(f"ranking returned {len(entries)} unique shows", file=sys.stderr)
    started = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, entries))
    print(f"probed in {time.time() - started:.1f}s", file=sys.stderr)

    flows = Counter(r.get("flow", "error") for r in results)
    genres = Counter(f"{r.get('genreCode')} {r.get('genreName')}" for r in results)
    OUT.write_text(
        json.dumps(
            {
                "surveyed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "count": len(results),
                "flow_breakdown": dict(flows),
                "genre_breakdown": dict(genres),
                "shows": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(dict(flows), ensure_ascii=False, indent=2))
    print(json.dumps(dict(genres), ensure_ascii=False, indent=2))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
