from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


ARM_STORAGE_KEY = "pureclick_arm_v1"
SEAT_STORAGE_KEY = "pureclick_seat_v1"


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
    click_x: int | None = None
    click_y: int | None = None

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
            click_x=int(click_x) if click_x is not None else None,
            click_y=int(click_y) if click_y is not None else None,
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
            "// PureClick — paste in Chrome DevTools console on tickets.interpark.com",
            f'localStorage.setItem("{ARM_STORAGE_KEY}", {json.dumps(arm_json)});',
            f'localStorage.setItem("{SEAT_STORAGE_KEY}", {json.dumps(seat_json)});',
        ]
    )
