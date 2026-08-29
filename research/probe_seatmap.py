"""Check whether onestop seat-map endpoints answer without a booking session.

Layout claims should rest on observed data, not on one show plus bundle reading.
This probes block-data / seatMeta across structurally different venues (small
theatre, arena, concert hall, baseball stadium) and reports exactly what each
endpoint returns when unauthenticated.
"""

from __future__ import annotations

import sys
import urllib.error
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

TARGETS = [
    ("26012391", "소극장 (화성예술의전당)"),
    ("26011315", "아레나 (킨텍스)"),
    ("26012258", "콘서트홀 (예술의전당)"),
    ("26004566", "야구장 (잠실)"),
    ("26005128", "대극장 (극장 용)"),
]


def get(url: str) -> tuple[int | None, str]:
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return response.status, response.read()[:300].decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read()[:200].decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001 - diagnostic output only
        return None, str(error)[:160]


def main() -> int:
    for code, desc in TARGETS:
        try:
            catalog = fetch_show_catalog(code)
        except Exception as error:  # noqa: BLE001
            print(f"{code} {desc}: summary failed ({error})")
            continue
        place = catalog.place_code
        seq = (catalog.play_seqs or ["001"])[0]
        print(f"\n### {desc}  goods={code} place={place} playSeq={seq}")
        endpoints = {
            "block-data": f"/onestop/api/seats/block-data?goodsCode={code}&placeCode={place}&playSeq={seq}",
            "seatMeta": (
                f"/onestop/api/seatMeta?goodsCode={code}&placeCode={place}"
                f"&playSeq={seq}&bizCode=WEBBR&blockKeys=001%3A001"
            ),
            "grades": (
                f"/onestop/api/seats/grades?goodsCode={code}&placeCode={place}"
                f"&playSeq={seq}&bizCode=WEBBR"
            ),
        }
        for name, path in endpoints.items():
            status, body = get(ORIGIN + path)
            print(f"  [{status}] {name:11s} {body[:120]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
