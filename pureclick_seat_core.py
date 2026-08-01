from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


class SeatAutopilotError(Exception):
    """Raised when seat autopilot configuration or compatibility checks fail."""


GOODS_CODE_RE = re.compile(r"/goods/([A-Z0-9]{6,10})", re.I)
DEFAULT_GRADE_ORDER = ("2", "3", "4", "1")


@dataclass(frozen=True)
class SeatPreferences:
    grade_order: tuple[str, ...] = DEFAULT_GRADE_ORDER
    max_attempts: int = 80
    retry_ms: int = 20
    poll_ms: int = 40

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> SeatPreferences:
        grades = data.get("grade_order") or data.get("gradeOrder") or DEFAULT_GRADE_ORDER
        if isinstance(grades, str):
            grades = [part.strip() for part in grades.split(",") if part.strip()]
        grade_order = tuple(str(grade) for grade in grades)
        if not grade_order:
            raise SeatAutopilotError("grade_order must include at least one seat grade")
        max_attempts = int(data.get("max_attempts", data.get("maxAttempts", 80)))
        retry_ms = int(data.get("retry_ms", data.get("retryMs", 20)))
        poll_ms = int(data.get("poll_ms", data.get("pollMs", 40)))
        if max_attempts < 1:
            raise SeatAutopilotError("max_attempts must be at least 1")
        if retry_ms < 0 or poll_ms < 1:
            raise SeatAutopilotError("retry_ms and poll_ms must be non-negative / positive")
        return cls(
            grade_order=grade_order,
            max_attempts=max_attempts,
            retry_ms=retry_ms,
            poll_ms=poll_ms,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "grade_order": list(self.grade_order),
            "max_attempts": self.max_attempts,
            "retry_ms": self.retry_ms,
            "poll_ms": self.poll_ms,
        }


@dataclass(frozen=True)
class ShowCompatibility:
    goods_code: str
    goods_name: str
    is_ingredient_onestop: bool
    is_reserved_seat: bool
    genre_code: str | None

    @property
    def supports_seat_autopilot(self) -> bool:
        return self.is_ingredient_onestop and self.is_reserved_seat

    @property
    def flow_label(self) -> str:
        if self.supports_seat_autopilot:
            return "onestop-reserved"
        if self.is_ingredient_onestop:
            return "onestop-general-admission"
        return "legacy-poticket"


@dataclass(frozen=True)
class SeatCandidate:
    seat_info_id: str
    seat_grade: str
    seat_grade_name: str
    row_no: str
    seat_no: str
    block_key: str | None = None

    @classmethod
    def from_api_seat(cls, seat: dict[str, Any], *, block_key: str | None = None) -> SeatCandidate | None:
        seat_info_id = seat.get("seatInfoId")
        seat_grade = seat.get("seatGrade")
        if not seat_info_id or not seat_grade:
            return None
        return cls(
            seat_info_id=str(seat_info_id),
            seat_grade=str(seat_grade),
            seat_grade_name=str(seat.get("seatGradeName") or ""),
            row_no=str(seat.get("rowNo") or ""),
            seat_no=str(seat.get("seatNo") or ""),
            block_key=block_key,
        )

    @classmethod
    def from_dom_id(cls, element_id: str, seat: dict[str, Any]) -> SeatCandidate | None:
        if not element_id.startswith("seat_block_"):
            return None
        seat_info_id = element_id.removeprefix("seat_block_")
        return cls.from_api_seat({**seat, "seatInfoId": seat_info_id})

    @property
    def label(self) -> str:
        parts = [self.seat_grade_name or self.seat_grade, self.row_no, self.seat_no]
        return " ".join(part for part in parts if part).strip()


def parse_goods_code(value: str) -> str:
    text = value.strip()
    if not text:
        raise SeatAutopilotError("Goods code or URL is required")
    if GOODS_CODE_RE.search(text):
        match = GOODS_CODE_RE.search(text)
        assert match is not None
        return match.group(1).upper()
    if re.fullmatch(r"[A-Z0-9]{6,10}", text, re.I):
        return text.upper()
    raise SeatAutopilotError("Could not parse an Interpark goods code from that value")


def parse_summary_compatibility(summary: dict[str, Any]) -> ShowCompatibility:
    goods_code = str(summary.get("goodsCode") or "")
    if not goods_code:
        raise SeatAutopilotError("summary response is missing goodsCode")
    return ShowCompatibility(
        goods_code=goods_code,
        goods_name=str(summary.get("goodsName") or ""),
        is_ingredient_onestop=bool(summary.get("isIngredientOnestop")),
        is_reserved_seat=bool(summary.get("isReservedSeat")),
        genre_code=str(summary.get("genreCode") or "") or None,
    )


def rank_seats(
    seats: Iterable[SeatCandidate],
    preferences: SeatPreferences,
) -> list[SeatCandidate]:
    grade_rank = {grade: index for index, grade in enumerate(preferences.grade_order)}
    default_rank = len(preferences.grade_order)

    def sort_key(seat: SeatCandidate) -> tuple[int, str, str, str]:
        return (
            grade_rank.get(seat.seat_grade, default_rank),
            seat.row_no,
            seat.seat_no,
            seat.seat_info_id,
        )

    unique: dict[str, SeatCandidate] = {}
    for seat in seats:
        unique[seat.seat_info_id] = seat
    return sorted(unique.values(), key=sort_key)


def filter_api_seats(
    seat_meta_blocks: Sequence[dict[str, Any]],
    preferences: SeatPreferences,
) -> list[SeatCandidate]:
    candidates: list[SeatCandidate] = []
    for block in seat_meta_blocks:
        block_key = str(block.get("blockKey") or "") or None
        for seat in block.get("seats") or []:
            if not isinstance(seat, dict):
                continue
            if not seat.get("isExposable"):
                continue
            if seat.get("seatGroupId"):
                continue
            parsed = SeatCandidate.from_api_seat(seat, block_key=block_key)
            if parsed is not None:
                candidates.append(parsed)
    return rank_seats(candidates, preferences)


def build_select_payload(
    *,
    init_data: dict[str, Any],
    seats: Sequence[SeatCandidate],
) -> dict[str, Any]:
    goods = init_data.get("goods") or {}
    play_seq = init_data.get("playSeq") or {}
    session_id = init_data.get("sessionId")
    if not session_id:
        raise SeatAutopilotError("initData is missing sessionId")
    goods_code = goods.get("goodsCode")
    place_code = goods.get("placeCode")
    play_seq_code = play_seq.get("playSeq")
    if not goods_code or not place_code or not play_seq_code:
        raise SeatAutopilotError("initData is missing goods/playSeq fields")
    if not seats:
        raise SeatAutopilotError("At least one seat candidate is required")

    seat_type = "DEFAULT"
    if goods.get("kindOfGoods") == "01007" and goods.get("isSportsGroup"):
        seat_type = "SPORTS"

    return {
        "goodsCode": goods_code,
        "placeCode": place_code,
        "playSeq": play_seq_code,
        "sessionId": session_id,
        "seatType": seat_type,
        "autoAssign": False,
        "seats": [
            {"seatGrade": seat.seat_grade, "seatInfoId": seat.seat_info_id}
            for seat in seats
        ],
    }


def serialize_preferences(preferences: SeatPreferences) -> str:
    return json.dumps(preferences.to_mapping(), ensure_ascii=False, indent=2)
