"""Verify that the seatStatus hex string is a per-seat availability bitmap.

If bit i of the decoded mask lines up with seat i of the seatMeta list, a catcher
can poll ~140 bytes instead of a full seat dump and identify exactly which seat
freed up, with no re-fetch.
"""

from __future__ import annotations

import json
import sys
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
    """Expand hex to bits, most-significant bit of each nibble first."""
    out: list[int] = []
    for char in hex_string.strip():
        try:
            value = int(char, 16)
        except ValueError:
            continue
        out.extend((value >> shift) & 1 for shift in (3, 2, 1, 0))
    return out


def check(goods: str, blocks: list[str]) -> None:
    catalog = fetch_show_catalog(goods)
    place, seq = catalog.place_code, (catalog.play_seqs or ["001"])[0]
    base = [("goodsCode", goods), ("placeCode", place), ("playSeq", seq), ("bizCode", "WEBBR")]
    print(f"\n### {goods} {catalog.place_name}  remain(api)={sum(g.remain for g in catalog.grades)}")

    for block in blocks:
        meta = call("/onestop/api/seatMeta", base + [("blockKeys", block)])
        seats = (meta or [{}])[0].get("seats") if isinstance(meta, list) and meta else []
        seats = seats or []
        status = call("/onestop/api/seatStatus", base + [("blockKeys", block)])
        data = status.get("data") if isinstance(status, dict) else status
        hex_string = data[0] if isinstance(data, list) and data else ""

        bits = bits_msb(hex_string)
        exposable_flags = [1 if seat.get("isExposable") else 0 for seat in seats]
        ones = sum(bits)
        aligned = bits[: len(seats)] == exposable_flags

        print(f"  block {block}: seats={len(seats)} hexlen={len(hex_string)} bits={len(bits)} "
              f"ones={ones} exposable={sum(exposable_flags)}")
        print(f"    bit-count matches exposable: {ones == sum(exposable_flags)}")
        print(f"    positional match (MSB-first): {aligned}")
        if not aligned and seats:
            first_diff = next(
                (i for i in range(min(len(bits), len(seats))) if bits[i] != exposable_flags[i]),
                None,
            )
            print(f"    first mismatch at index {first_diff}")
            print(f"    mask head : {''.join(map(str, bits[:40]))}")
            print(f"    expose head: {''.join(map(str, exposable_flags[:40]))}")


def main() -> int:
    check("26005128", ["001:001", "002:002"])
    check("26011315", ["001:001"])
    check("26012391", ["002:002"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
