"""Pin down what a set bit in seatStatus means, and whether it moves in real time."""

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


def call(path: str, params: list[tuple[str, str]]):
    request = urllib.request.Request(
        ORIGIN + path + "?" + urllib.parse.urlencode(params), headers=HEADERS
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def bits_msb(hex_string: str) -> list[int]:
    out: list[int] = []
    for char in hex_string.strip():
        try:
            value = int(char, 16)
        except ValueError:
            continue
        out.extend((value >> shift) & 1 for shift in (3, 2, 1, 0))
    return out


def analyse(goods: str, block: str, samples: int = 3, gap: float = 4.0) -> None:
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    base = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]

    meta = call("/onestop/api/seatMeta", base + [("blockKeys", block)])
    seats = (meta or [{}])[0].get("seats") if isinstance(meta, list) and meta else []
    seats = seats or []
    exposable = [1 if seat.get("isExposable") else 0 for seat in seats]

    print(f"\n### {goods} {catalog.place_name} block={block} seats={len(seats)} exposable={sum(exposable)}")

    previous: list[int] | None = None
    for sample in range(samples):
        status = call("/onestop/api/seatStatus", base + [("blockKeys", block)])
        data = status.get("data") if isinstance(status, dict) else status
        bits = bits_msb(data[0] if isinstance(data, list) and data else "")[: len(seats)]

        free_not_exposable = sum(1 for i, bit in enumerate(bits) if bit and not exposable[i])
        exposable_not_free = sum(1 for i, bit in enumerate(bits) if exposable[i] and not bit)
        print(f"  sample {sample}: set={sum(bits):<5} "
              f"set_but_not_exposable={free_not_exposable:<4} exposable_but_unset={exposable_not_free}")

        if previous is not None:
            flipped = [i for i in range(min(len(bits), len(previous))) if bits[i] != previous[i]]
            if flipped:
                print(f"    changed indices ({len(flipped)}): {flipped[:10]}")
                for index in flipped[:3]:
                    seat = seats[index]
                    direction = "freed" if bits[index] else "taken"
                    print(f"      [{index}] {direction}: {seat.get('rowNo')} {seat.get('seatNo')} "
                          f"grade={seat.get('seatGradeName')}")
            else:
                print("    no change")
        previous = bits
        if sample < samples - 1:
            time.sleep(gap)


def main() -> int:
    analyse("26005128", "001:001")
    analyse("26011315", "001:001", samples=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
