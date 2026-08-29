"""Unwrap seatStatus and test whether bizCode changes what seatMeta reveals."""

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
    request = urllib.request.Request(
        ORIGIN + path + "?" + urllib.parse.urlencode(params), headers=HEADERS
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return error.code, None
    except Exception:  # noqa: BLE001
        return None, None


def show_status(goods: str, block: str) -> None:
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    base = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]
    status, payload = call("/onestop/api/seatStatus", base + [("blockKeys", block)])
    print(f"\n### seatStatus {goods} {catalog.place_name} block={block} [{status}]")
    print("  raw:", json.dumps(payload, ensure_ascii=False)[:700])
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list) and data:
        first = data[0]
        print("  first entry keys:", list(first.keys()) if isinstance(first, dict) else type(first))
        rows = first.get("seats") if isinstance(first, dict) else None
        if isinstance(rows, list) and rows:
            print("  seat row sample:", json.dumps(rows[0], ensure_ascii=False)[:300])
            counter = Counter(json.dumps(sorted(r.keys()), ensure_ascii=False) for r in rows if isinstance(r, dict))
            print("  row shapes:", dict(counter.most_common(3)))


def bizcode_sweep(goods: str, block: str) -> None:
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    print(f"\n### bizCode sweep {goods} {catalog.place_name} block={block}")
    for biz in ["WEBBR", "", "MOBILE", "WEB", "NOL", "29283", "TICKETLINK"]:
        params = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq)]
        if biz:
            params.append(("bizCode", biz))
        params.append(("blockKeys", block))
        status, payload = call("/onestop/api/seatMeta", params)
        seats = (payload or [{}])[0].get("seats") if isinstance(payload, list) and payload else []
        seats = seats or []
        exposable = sum(1 for seat in seats if seat.get("isExposable"))
        graded = sum(1 for seat in seats if seat.get("seatGrade"))
        print(f"  bizCode={biz or '(omitted)':<10} [{status}] seats={len(seats):<5} exposable={exposable:<5} graded={graded}")


def playseq_sweep(goods: str, block: str) -> None:
    catalog = fetch_show_catalog(goods)
    place = catalog.place_code
    print(f"\n### playSeq sweep {goods} {catalog.place_name} block={block} known={catalog.play_seqs}")
    for seq in ["001", "002", "003"]:
        params = [
            ("goodsCode", goods), ("placeCode", place), ("playSeq", seq),
            ("bizCode", "WEBBR"), ("blockKeys", block),
        ]
        status, payload = call("/onestop/api/seatMeta", params)
        seats = (payload or [{}])[0].get("seats") if isinstance(payload, list) and payload else []
        seats = seats or []
        exposable = sum(1 for seat in seats if seat.get("isExposable"))
        print(f"  playSeq={seq} [{status}] seats={len(seats):<5} exposable={exposable}")


def main() -> int:
    show_status("26005128", "001:001")
    show_status("26012391", "002:002")
    bizcode_sweep("26012391", "002:002")
    playseq_sweep("26012391", "002:002")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
