from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core.entry import NOL_BIZ_CODE


ARM_STORAGE_KEY = "nolsniper_arm_v1"
SEAT_STORAGE_KEY = "nolsniper_seat_v1"

# How far the fire may be nudged either side of 티켓 오픈. Negative is early.
#
# The bound is not a taste: past a few seconds the "보정" stops being a
# correction for server-side lag and becomes a different open time typed into
# the wrong field, which is a mistake worth refusing rather than honouring.
ENTRY_OFFSET_LIMIT_MS = 5000


def clamp_entry_offset_ms(value: object) -> int:
    """The offset as a whole number of milliseconds, inside the allowed range.

    Never raises. A blank or unparseable field means "no correction" — this is
    read on the way to a race, where refusing to arm over a stray character is
    worse than ignoring it.
    """
    try:
        ms = int(round(float(str(value).strip() or 0)))
    except (TypeError, ValueError):
        return 0
    return max(-ENTRY_OFFSET_LIMIT_MS, min(ENTRY_OFFSET_LIMIT_MS, ms))


@dataclass(frozen=True)
class ArmPayload:
    enabled: bool
    goods_code: str
    play_date: str
    play_seq: str
    target_server_unix: float
    offset_seconds: float
    dry_run: bool = False
    fired: bool = False
    use_waiting_api: bool = True
    place_code: str = ""
    channel_code: str = "pc"
    pre_sales: str = "N"
    click_x: int | None = None
    click_y: int | None = None
    auto_seats_after_entry: bool = False
    # The user's own ms correction, applied to every route the entry can take.
    #
    # Deliberately *not* folded into target_server_unix: that field is 티켓 오픈
    # itself, and the panel shows both ("오픈 20:00:00.000 → 발사 19:59:59.750").
    # Folding them would leave no way to tell a corrected open from a mistyped
    # one, on the two readings you most need to compare.
    entry_offset_ms: int = 0
    # The partner code the queue call is made under (bc=61776 in NOL's own gate
    # URL). Carried on the arm rather than hardcoded in the page so a show that
    # arrives through a different partner can be entered without a new build.
    biz_code: str = NOL_BIZ_CODE
    # HHmm, from goods-info. Needed because a multi-round show's booking session
    # is show-level — `interpark/context` carries no playSeq at all — so the
    # round is chosen on /onestop/schedule, where the only thing identifying it
    # on screen is the clock time.
    play_time: str = ""

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "goods_code": self.goods_code,
            "play_date": self.play_date,
            "play_seq": self.play_seq,
            "target_server_unix": self.target_server_unix,
            "offset_seconds": self.offset_seconds,
            "dry_run": self.dry_run,
            "fired": self.fired,
            "use_waiting_api": self.use_waiting_api,
            "place_code": self.place_code,
            "channel_code": self.channel_code,
            "pre_sales": self.pre_sales,
            "auto_seats_after_entry": self.auto_seats_after_entry,
            "entry_offset_ms": self.entry_offset_ms,
            "biz_code": self.biz_code,
            "play_time": self.play_time,
        }
        if self.click_x is not None and self.click_y is not None:
            data["click_x"] = self.click_x
            data["click_y"] = self.click_y
        return data

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> ArmPayload:
        goods_code = str(data.get("goods_code") or "").strip().upper()
        play_date = str(data.get("play_date") or "").strip()
        play_seq = str(data.get("play_seq") or "001").strip()
        if not goods_code:
            raise ValueError("goods_code is required")
        if not play_date.isdigit() or len(play_date) != 8:
            raise ValueError("play_date must be YYYYMMDD")
        if not play_seq:
            raise ValueError("play_seq is required")
        click_x = data.get("click_x")
        click_y = data.get("click_y")
        return cls(
            enabled=bool(data.get("enabled", True)),
            goods_code=goods_code,
            play_date=play_date,
            play_seq=play_seq,
            target_server_unix=float(data["target_server_unix"]),
            offset_seconds=float(data.get("offset_seconds", 0.0)),
            dry_run=bool(data.get("dry_run", False)),
            fired=bool(data.get("fired", False)),
            use_waiting_api=bool(data.get("use_waiting_api", True)),
            place_code=str(data.get("place_code") or data.get("placeCode") or "").strip(),
            channel_code=str(data.get("channel_code") or data.get("channelCode") or "pc").strip() or "pc",
            pre_sales=str(data.get("pre_sales") or data.get("preSales") or "N").strip() or "N",
            click_x=int(click_x) if click_x is not None else None,
            click_y=int(click_y) if click_y is not None else None,
            auto_seats_after_entry=bool(data.get("auto_seats_after_entry", False)),
            entry_offset_ms=clamp_entry_offset_ms(
                data.get("entry_offset_ms", data.get("entryOffsetMs", 0))
            ),
            biz_code=str(
                data.get("biz_code") or data.get("bizCode") or NOL_BIZ_CODE
            ).strip() or NOL_BIZ_CODE,
            play_time=re.sub(
                r"\D", "", str(data.get("play_time") or data.get("playTime") or "")),
        )


def serialize_arm_payload(payload: ArmPayload) -> str:
    return json.dumps(payload.to_mapping(), ensure_ascii=False, indent=2)


def browser_arm_snippet(payload: ArmPayload) -> str:
    body = serialize_arm_payload(payload)
    return (
        "// Paste in Chrome DevTools console on tickets.interpark.com\n"
        f'localStorage.setItem("{ARM_STORAGE_KEY}", {json.dumps(body)});'
    )


def browser_config_snippet(*, arm_payload: ArmPayload, seat_mapping: dict[str, Any]) -> str:
    seat_json = json.dumps(seat_mapping, ensure_ascii=False)
    arm_json = serialize_arm_payload(arm_payload)
    return "\n".join(
        [
            "// NOL Sniper — paste in Chrome DevTools console on tickets.interpark.com",
            f'localStorage.setItem("{ARM_STORAGE_KEY}", {json.dumps(arm_json)});',
            f'localStorage.setItem("{SEAT_STORAGE_KEY}", {json.dumps(seat_json)});',
        ]
    )
