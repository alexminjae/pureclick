"""Check that seatMeta and seatStatus responses follow the requested block order.

The status payload is a bare list of hex strings with no block labels, so it can
only be joined to seats positionally. That join has to be proven, not assumed.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.showinfo import BROWSER_UA, fetch_show_catalog  # noqa: E402

ORIGIN = "https://tickets.interpark.com"
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": f"{ORIGIN}/onestop/seat",
}


def call(path: str, params):
    request = urllib.request.Request(
        ORIGIN + path + "?" + urllib.parse.urlencode(params), headers=HEADERS
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def main() -> int:
    goods = "26005128"
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]

    for order in (["001:001", "002:002"], ["002:002", "001:001"]):
        base = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]
        base += [("blockKeys", key) for key in order]
        meta = call("/onestop/api/seatMeta", base)
        status = call("/onestop/api/seatStatus", base)
        data = status.get("data") if isinstance(status, dict) else status

        print(f"requested {order}")
        print(f"  meta blockKeys : {[b.get('blockKey') for b in meta]}")
        print(f"  meta seatcounts: {[len(b.get('seats') or []) for b in meta]}")
        print(f"  status entries : {len(data)}  hexlens={[len(x) for x in data]}  "
              f"implied seats={[len(x) * 4 for x in data]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
