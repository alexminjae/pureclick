"""Find how many blockKeys /onestop/api/seatMeta will accept per request.

Scan speed on a large venue depends entirely on this: too few and a stadium
takes hundreds of round trips, too many and the endpoint silently returns
nothing instead of erroring.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
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


def seat_meta(goods: str, place: str, seq: str, keys: list[str]):
    params = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]
    params += [("blockKeys", key) for key in keys]
    url = ORIGIN + "/onestop/api/seatMeta?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return error.code, None
    except Exception:  # noqa: BLE001
        return None, None


def main() -> int:
    goods = sys.argv[1] if len(sys.argv) > 1 else "26012391"
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    print(f"{goods} {catalog.place_name} place={place} seq={seq}\n")

    known = [f"{i:03d}:{i:03d}" for i in range(1, 13)]

    print("single-key sanity check:")
    for key in known[:4]:
        status, payload = seat_meta(goods, place, seq, [key])
        seats = len((payload or [{}])[0].get("seats") or []) if payload else 0
        print(f"  {key} -> [{status}] seats={seats}")
        time.sleep(0.3)

    print("\nbatch size sweep:")
    for size in (1, 2, 3, 4, 6, 8, 10, 12):
        status, payload = seat_meta(goods, place, seq, known[:size])
        if payload is None:
            print(f"  n={size:<3} [{status}] no payload")
        else:
            blocks = len(payload)
            seats = sum(len(block.get("seats") or []) for block in payload)
            print(f"  n={size:<3} [{status}] blocks={blocks} seats={seats}")
        time.sleep(0.6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
