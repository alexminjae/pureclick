from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


class SeatAutopilotError(Exception):
    """Raised when seat autopilot configuration or compatibility checks fail."""


GOODS_CODE_RE = re.compile(
    r"(?:/goods/|/ticket/products/)([A-Z0-9]{6,10})",
    re.I,
)

# Intentionally empty. `seatGrade` is a per-show ordinal, not a global scale:
# grade "1" is VIP석 on a musical, EARLY ENTRY PACKAGE on Maroon 5, and absent
# entirely on some KBO games. Any hardcoded order is wrong for most shows, so
# with no user preference we keep the show's own grade ordering.
DEFAULT_GRADE_ORDER: tuple[str, ...] = ()
# Which seat to aim for within a grade tier. Ordering by grade then row makes
# every macro converge on the same front seat; picking a different corner is how
# one instance avoids racing the crowd for it.
VALID_STRATEGIES = frozenset({"center", "left", "right"})

# Retired names, kept only so a saved config from an older build still loads.
# "stage" and "center" both now mean the same thing — closest to the stage,
# middle of the house — and "random" was a way of dodging other macros that the
# stage-first ordering makes meaningless.
LEGACY_STRATEGIES = {"stage": "center", "random": "center"}


@dataclass(frozen=True)
class SeatPreferences:
    grade_order: tuple[str, ...] = DEFAULT_GRADE_ORDER
    block_keys: tuple[str, ...] = ()
    block_names: tuple[str, ...] = ()
    # Venue coords (seatMeta posLeft/posTop). Empty = whole map.
    watch_rect: tuple[float, float, float, float] | None = None
    seat_strategy: str = "center"
    max_attempts: int = 80
    retry_ms: int = 20
    poll_ms: int = 40
    speed_ms: int = 100
    quantity: int = 1
    birth_yymmdd: str = ""
    delivery: str = "배송"
    payment: str = "무통장입금"
    discord_webhook: str = ""
    reentry: bool = False
    adjacent: bool = True
    auto_seats_after_entry: bool = False
    grade_strict: bool = False
    catch_grade_strict: bool = True
    allow_group_seats: bool = True
    auto_assign: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> SeatPreferences:
        grades = data.get("grade_order") or data.get("gradeOrder") or DEFAULT_GRADE_ORDER
        if isinstance(grades, str):
            grades = [part.strip() for part in grades.split(",") if part.strip()]
        grade_order = tuple(str(grade) for grade in grades if str(grade).strip())

        blocks = data.get("block_keys") or data.get("blockKeys") or ()
        if isinstance(blocks, str):
            blocks = [part.strip() for part in blocks.split(",") if part.strip()]
        block_names = data.get("block_names") or data.get("blockNames") or ()
        if isinstance(block_names, str):
            block_names = [part.strip() for part in block_names.split(",") if part.strip()]

        watch_rect = None
        raw_rect = data.get("watch_rect") or data.get("watchRect")
        if isinstance(raw_rect, dict):
            try:
                left = float(raw_rect["left"])
                top = float(raw_rect["top"])
                right = float(raw_rect["right"])
                bottom = float(raw_rect["bottom"])
                if right < left:
                    left, right = right, left
                if bottom < top:
                    top, bottom = bottom, top
                if right - left >= 0.5 and bottom - top >= 0.5:
                    watch_rect = (left, top, right, bottom)
            except (KeyError, TypeError, ValueError):
                watch_rect = None
        elif isinstance(raw_rect, (list, tuple)) and len(raw_rect) == 4:
            try:
                left, top, right, bottom = (float(v) for v in raw_rect)
                if right < left:
                    left, right = right, left
                if bottom < top:
                    top, bottom = bottom, top
                if right - left >= 0.5 and bottom - top >= 0.5:
                    watch_rect = (left, top, right, bottom)
            except (TypeError, ValueError):
                watch_rect = None

        strategy = str(data.get("seat_strategy") or "center").strip().lower()
        strategy = LEGACY_STRATEGIES.get(strategy, strategy)
        if strategy not in VALID_STRATEGIES:
            raise SeatAutopilotError(
                "seat_strategy must be one of " + ", ".join(sorted(VALID_STRATEGIES))
            )

        max_attempts = int(data.get("max_attempts", data.get("maxAttempts", 80)))
        retry_ms = int(data.get("retry_ms", data.get("retryMs", 20)))
        poll_ms = int(data.get("poll_ms", data.get("pollMs", 40)))
        speed_ms = int(data.get("speed_ms", data.get("speedMs", poll_ms or 100)))
        quantity = int(data.get("quantity", data.get("매수", 1)))
        birth = str(data.get("birth_yymmdd") or data.get("birth") or "").strip()
        if birth and (not birth.isdigit() or len(birth) != 6):
            raise SeatAutopilotError("생년월일 must be YYMMDD (6 digits)")
        if max_attempts < 1:
            raise SeatAutopilotError("max_attempts must be at least 1")
        # 0 means "let the autopilot pace itself".
        #
        # 취켓팅 holds a request budget that keeps the gateway quiet, and a number
        # set here can only ever slow it down. A saved 400 from an older config
        # silently halved the watch's speed, so the panel now sends 0 and the
        # autopilot uses its own floor — but the value still has to survive
        # validation to get there.
        if retry_ms < 0 or poll_ms < 0 or speed_ms < 0:
            raise SeatAutopilotError("retry_ms, poll_ms, and speed_ms cannot be negative")
        if 0 < speed_ms < 10:
            raise SeatAutopilotError("speed_ms must be 0 (auto) or at least 10")
        if quantity < 1 or quantity > 10:
            raise SeatAutopilotError("quantity must be 1–10")
        return cls(
            grade_order=grade_order,
            block_keys=tuple(str(key) for key in blocks),
            block_names=tuple(str(name) for name in block_names),
            watch_rect=watch_rect,
            seat_strategy=strategy,
            max_attempts=max_attempts,
            retry_ms=retry_ms,
            poll_ms=min(poll_ms, speed_ms) if speed_ms else poll_ms,
            speed_ms=speed_ms,
            quantity=quantity,
            birth_yymmdd=birth,
            delivery=str(data.get("delivery") or "배송"),
            payment=str(data.get("payment") or "무통장입금"),
            discord_webhook=str(data.get("discord_webhook") or "").strip(),
            reentry=bool(data.get("reentry", False)),
            adjacent=bool(data.get("adjacent", True)),
            auto_seats_after_entry=bool(data.get("auto_seats_after_entry", False)),
            grade_strict=bool(data.get("grade_strict", False)),
            catch_grade_strict=bool(data.get("catch_grade_strict", True)),
            allow_group_seats=bool(data.get("allow_group_seats", True)),
            auto_assign=bool(data.get("auto_assign", False)),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "grade_order": list(self.grade_order),
            "block_keys": list(self.block_keys),
            "block_names": list(self.block_names),
            "watch_rect": (
                {
                    "left": self.watch_rect[0],
                    "top": self.watch_rect[1],
                    "right": self.watch_rect[2],
                    "bottom": self.watch_rect[3],
                }
                if self.watch_rect
                else None
            ),
            "seat_strategy": self.seat_strategy,
            "max_attempts": self.max_attempts,
            "retry_ms": self.retry_ms,
            "poll_ms": self.poll_ms,
            "speed_ms": self.speed_ms,
            "quantity": self.quantity,
            "birth_yymmdd": self.birth_yymmdd,
            "delivery": self.delivery,
            "payment": self.payment,
            "discord_webhook": self.discord_webhook,
            "reentry": self.reentry,
            "adjacent": self.adjacent,
            "auto_seats_after_entry": self.auto_seats_after_entry,
            "grade_strict": self.grade_strict,
            "catch_grade_strict": self.catch_grade_strict,
            "allow_group_seats": self.allow_group_seats,
            "auto_assign": self.auto_assign,
        }


@dataclass(frozen=True)
class ShowCompatibility:
    goods_code: str
    goods_name: str
    is_ingredient_onestop: bool
    is_reserved_seat: bool
    genre_code: str | None
    is_sport_onestop: bool = False
    is_captcha: bool = False
    hide_remain_seat: bool = False

    @property
    def supports_seat_autopilot(self) -> bool:
        return self.is_ingredient_onestop and self.is_reserved_seat

    @property
    def flow_label(self) -> str:
        if not self.is_ingredient_onestop:
            return "legacy-poticket"
        if self.is_sport_onestop:
            return "onestop-sports"
        if not self.is_reserved_seat:
            return "onestop-general-admission"
        return "onestop-reserved"

    @property
    def flow_description(self) -> str:
        return {
            "onestop-reserved": "지정석 원스탑 — 좌석 단위 선점 가능",
            "onestop-sports": "스포츠 원스탑 — 좌석 선점 가능 (seatType=SPORTS)",
            "onestop-general-admission": "비지정석/입장권 — 좌석 선택 없이 매수만 지정",
            "legacy-poticket": "구형 poticket 엔진 — 원스탑 좌석 API 미지원",
        }[self.flow_label]

    @property
    def needs_seat_picking(self) -> bool:
        return self.flow_label in {"onestop-reserved", "onestop-sports"}

    @property
    def seat_type(self) -> str:
        return "SPORTS" if self.flow_label == "onestop-sports" else "DEFAULT"


@dataclass(frozen=True)
class SeatCandidate:
    seat_info_id: str
    seat_grade: str
    seat_grade_name: str
    row_no: str
    seat_no: str
    block_key: str | None = None
    seat_group_id: str | None = None

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
            seat_group_id=str(seat["seatGroupId"]) if seat.get("seatGroupId") else None,
        )

    @property
    def label(self) -> str:
        parts = [self.seat_grade_name or self.seat_grade, self.row_no, self.seat_no]
        return " ".join(part for part in parts if part).strip()


def parse_goods_code(value: str) -> str:
    text = value.strip()
    if not text:
        raise SeatAutopilotError("Goods code or URL is required")
    match = GOODS_CODE_RE.search(text)
    if match:
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
        is_sport_onestop=bool(summary.get("isSportOneStop")),
        is_captcha=bool(summary.get("isCaptcha")),
        hide_remain_seat="C5021" in str(summary.get("goodsQualityList") or ""),
    )


def normalize_grade_token(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def grade_rank_of(seat: SeatCandidate, grade_order: Sequence[str]) -> int | None:
    """Position of `seat` in `grade_order`, matching on code or grade name.

    Grade names are the only stable identifier across shows, so a preference
    entry matches when it equals the numeric `seatGrade`, equals the grade name,
    or appears inside it ("R석" matching "R석 (2층)"). Returns None when the seat
    matches nothing in the order.
    """
    code = normalize_grade_token(seat.seat_grade)
    name = normalize_grade_token(seat.seat_grade_name)
    for index, wanted in enumerate(grade_order):
        needle = normalize_grade_token(wanted)
        if not needle:
            continue
        if needle == code or needle == name or (name and needle in name):
            return index
    return None


def rank_seats(
    seats: Iterable[SeatCandidate],
    preferences: SeatPreferences,
) -> list[SeatCandidate]:
    grade_order = preferences.grade_order
    default_rank = len(grade_order)
    allowed_blocks = set(preferences.block_keys) if preferences.block_keys else None

    unique: dict[str, SeatCandidate] = {}
    ranks: dict[str, int] = {}
    for seat in seats:
        if allowed_blocks is not None and seat.block_key not in allowed_blocks:
            continue
        rank = grade_rank_of(seat, grade_order) if grade_order else None
        if rank is None:
            if grade_order and preferences.grade_strict:
                continue
            rank = default_rank
        unique[seat.seat_info_id] = seat
        ranks[seat.seat_info_id] = rank

    def sort_key(seat: SeatCandidate) -> tuple[int, str, str, str]:
        return (ranks[seat.seat_info_id], seat.row_no, seat.seat_no, seat.seat_info_id)

    return sorted(unique.values(), key=sort_key)


def pick_adjacent_seats(
    seats: Sequence[SeatCandidate],
    quantity: int,
) -> list[SeatCandidate]:
    """Pick `quantity` consecutive seats in the same row, else the first N ranked seats."""
    if quantity <= 1:
        return list(seats[:1])
    by_row: dict[str, list[SeatCandidate]] = {}
    for seat in seats:
        by_row.setdefault(seat.row_no or "", []).append(seat)
    for row_seats in by_row.values():
        ordered = sorted(row_seats, key=lambda item: (item.seat_no, item.seat_info_id))
        for start in range(0, len(ordered) - quantity + 1):
            window = ordered[start : start + quantity]
            numbers: list[int] = []
            consecutive = True
            for item in window:
                try:
                    numbers.append(int(re.sub(r"\D", "", item.seat_no) or "0"))
                except ValueError:
                    consecutive = False
                    break
            if consecutive and all(numbers[i] + 1 == numbers[i + 1] for i in range(len(numbers) - 1)):
                return window
    return list(seats[:quantity])


def decode_status_mask(hex_string: str) -> list[bool]:
    """Expand a seatStatus hex string into one flag per seat.

    `/onestop/api/seatStatus` answers with a bare hex string per requested block
    (e.g. `"0FFFF01FFFFC…"`), four seats per character, most-significant bit
    first, in the same order as that block's seatMeta list. A set bit means the
    seat is free right now.
    """
    flags: list[bool] = []
    for char in str(hex_string or "").strip():
        try:
            value = int(char, 16)
        except ValueError:
            continue
        flags.extend(bool((value >> shift) & 1) for shift in (3, 2, 1, 0))
    return flags


def parse_seat_status(payload: Any) -> list[list[bool]]:
    """Decode a seatStatus response into per-block masks, in request order."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    return [decode_status_mask(entry) for entry in data if isinstance(entry, str)]


def filter_api_seats(
    seat_meta_blocks: Sequence[dict[str, Any]],
    preferences: SeatPreferences,
    status_masks: Sequence[Sequence[bool]] | None = None,
) -> list[SeatCandidate]:
    """Rank seats that are actually takeable.

    `isExposable` only means the seat belongs to the sellable map — a sold-out
    show still reports it for every seat. Live availability comes from the
    seatStatus bitmap, so when a mask is supplied a seat must satisfy both.
    """
    candidates: list[SeatCandidate] = []
    for index, block in enumerate(seat_meta_blocks):
        block_key = str(block.get("blockKey") or "") or None
        mask = status_masks[index] if status_masks is not None and index < len(status_masks) else None
        for position, seat in enumerate(block.get("seats") or []):
            if not isinstance(seat, dict):
                continue
            if not seat.get("isExposable"):
                continue
            if mask is not None and not (position < len(mask) and mask[position]):
                continue
            if seat.get("seatGroupId") and not preferences.allow_group_seats:
                continue
            parsed = SeatCandidate.from_api_seat(seat, block_key=block_key)
            if parsed is not None:
                candidates.append(parsed)
    return rank_seats(candidates, preferences)


def diff_status_masks(
    previous: Sequence[bool],
    current: Sequence[bool],
) -> list[int]:
    """Seat positions that became free between two polls of the same block.

    Diffing the bitmap identifies the exact freed seat without re-reading
    seatMeta, which is what makes a cancellation catch fast.
    """
    freed: list[int] = []
    for index in range(min(len(previous), len(current))):
        if current[index] and not previous[index]:
            freed.append(index)
    return freed


def group_seat_candidates(seats: Sequence[SeatCandidate]) -> list[list[SeatCandidate]]:
    """Bundle seats sharing a `seatGroupId` into atomic units.

    Package and table seats can only be taken as a whole set — the site selects
    every sibling when one is clicked — so they must be offered to the picker as
    one unit. Ungrouped seats become single-element units. Ranking order from
    `rank_seats` is preserved by first appearance.
    """
    units: list[list[SeatCandidate]] = []
    by_group: dict[str, list[SeatCandidate]] = {}
    for seat in seats:
        if not seat.seat_group_id:
            units.append([seat])
            continue
        existing = by_group.get(seat.seat_group_id)
        if existing is None:
            existing = []
            by_group[seat.seat_group_id] = existing
            units.append(existing)
        existing.append(seat)
    return units


def select_seat_unit(
    seats: Sequence[SeatCandidate],
    quantity: int,
    *,
    adjacent: bool = True,
) -> list[SeatCandidate]:
    """Choose the next set of seats to attempt, honouring group atomicity.

    A grouped unit is only usable when its size matches the requested quantity,
    since partial group selection is rejected server-side.
    """
    units = group_seat_candidates(seats)
    for unit in units:
        if len(unit) > 1:
            if len(unit) == quantity:
                return list(unit)
            continue
    loose = [unit[0] for unit in units if len(unit) == 1]
    if not loose:
        return []
    if quantity <= 1:
        return loose[:1]
    return pick_adjacent_seats(loose, quantity) if adjacent else loose[:quantity]


def resolve_seat_type(goods: dict[str, Any]) -> str:
    """Sports venues use a distinct seat type on /onestop/api/seats/select.

    `kindOfGoods` alone is unreliable — some KBO/K-League products expose the
    sports flag without the genre code and vice versa — so either signal wins.
    """
    if goods.get("isSportOneStop") or goods.get("isSportsGroup"):
        return "SPORTS"
    if str(goods.get("kindOfGoods") or "") == "01007":
        return "SPORTS"
    return "DEFAULT"


def build_select_payload(
    *,
    init_data: dict[str, Any],
    seats: Sequence[SeatCandidate],
    auto_assign: bool = False,
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
    if not seats and not auto_assign:
        raise SeatAutopilotError("At least one seat candidate is required")

    return {
        "goodsCode": goods_code,
        "placeCode": place_code,
        "playSeq": play_seq_code,
        "sessionId": session_id,
        "seatType": resolve_seat_type(goods),
        "autoAssign": bool(auto_assign),
        "seats": [
            {"seatGrade": seat.seat_grade, "seatInfoId": seat.seat_info_id}
            for seat in seats
        ],
    }


def serialize_preferences(preferences: SeatPreferences) -> str:
    return json.dumps(preferences.to_mapping(), ensure_ascii=False, indent=2)


def seat_order_lines(seat: dict[str, Any]) -> list[str]:
    """What the ordering chose, and what it beat.

    "무대 가까운 순 seems random" was unanswerable: the ranking happened in the
    page and nothing about it ever reached the screen, so every explanation was
    a guess. Each attempt now publishes its top few candidates with their
    coordinates and their distance to the stage. If those distances climb down
    the list, the ordering is right whatever the winning seat looks like on the
    map; if they do not, the bug is visible on its face.
    """
    order = seat.get("seatOrder")
    if not isinstance(order, list) or not order:
        return []

    lines: list[str] = []
    stage = seat.get("stagePoint")
    if isinstance(stage, dict) and stage.get("x") is not None:
        lines.append(f"무대 기준점  ({stage['x']}, {stage['y']})")

    for index, row in enumerate(order[:5], start=1):
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip() or "?"
        x, y = row.get("x"), row.get("y")
        where = f"({x}, {y})" if x is not None and y is not None else "(좌표 없음)"
        dist = row.get("dist")
        near = f"거리 {dist}" if isinstance(dist, (int, float)) else "거리 —"
        lines.append(f"{index}. {label}  {where}  {near}")
    return lines


# The kinds of map move the watch makes, in the order they cost.
MAP_MOVE_LABELS = {
    "leaveBlock": "구역 나가기",
    "enterBlock": "구역 열기",
    "fitBlock": "화면 맞추기",
    "aim": "좌석까지 이동",
}


def map_move_lines(seat: dict[str, Any]) -> list[str]:
    """What travelling to a seat actually costs.

    Once a cancellation appears, the time between seeing it and clicking it is
    mostly spent getting the seat drawn: stepping out of one 구역, opening
    another, fitting it to the viewport. The settle budgets those run against
    (900/700/250 ms) are ceilings someone chose — the only measured figure
    anywhere was a 389 ms note in a comment. These are the real ones.
    """
    moves = seat.get("mapMoves")
    scans = seat.get("domScans") or 0
    if (not isinstance(moves, dict) or not moves) and not scans:
        return []

    lines: list[str] = []
    if scans:
        # Reading the drawn seat map costs four passes over the document, and
        # on 아레나 킨텍스 that document holds 21,460 seats. Whether that matters
        # is a measurement, and this is it.
        average = round((seat.get("domScanMs") or 0) / scans, 1)
        lines.append(
            f"좌석맵 읽기  {scans}회 · 평균 {average}ms · 최대 {seat.get('domScanWorstMs') or 0}ms"
        )
    if not isinstance(moves, dict):
        return lines
    for key, label in MAP_MOVE_LABELS.items():
        row = moves.get(key)
        if not isinstance(row, dict):
            continue
        count = row.get("n") or 0
        if not count:
            continue
        average = round((row.get("totalMs") or 0) / count)
        worst = row.get("worstMs") or 0
        note = f"{label}  {count}회 · 평균 {average}ms · 최대 {worst}ms"
        failed = row.get("failed") or 0
        if failed:
            note += f" · 실패 {failed}회"
        lines.append(note)
    return lines


# --- What the panel shows while a run is going ------------------------------
#
# These turn the status the page publishes into the lines you read. They live
# here rather than in the panel because they are pure — a status dict in, text
# out, no widgets — which is what makes them testable. running_hint was 119
# lines inside the GUI class and had never had a test; it is the line you stare
# at during an open.
CAPTCHA_LABELS = {
    "reading": "보안문자 읽는 중…",
    "read": "보안문자 인식됨",
    "unreadable": "보안문자 인식 실패 — 예매 창에서 직접 입력하세요",
    "timeout": "보안문자 인식 무응답 — 예매 창에서 직접 입력하세요",
}

# The watch's own tick, used only as a fallback until the page reports the
# one it actually measured. A tick is the sleep plus the request, and
# seatStatus measures 58ms, so 200 alone understates every sweep.
CATCH_TICK_MS = 200

def running_hint(seat: dict[str, Any], message: str) -> str:
    """The one line worth reading while it runs."""
    # A gateway block makes every other number meaningless.
    blocked = int(seat.get("blockedForMs") or 0)
    if blocked > 0:
        # Which call earned it. Until now nothing recorded that, so after the
        # fact there was no way to tell whether the queue or the seat path had
        # been throttled — and those need different answers.
        where = str(seat.get("blockedEndpoint") or "").strip()
        cause = f" ({where})" if where else ""
        return (
            f"접속 차단 중 — {blocked // 1000}초 남음{cause}. "
            "차단 중에는 좌석을 잡을 수 없고, 계속 시도하면 차단이 길어집니다."
        )

    # A captcha blocks everything else, so it outranks the rest.
    captcha = seat.get("captcha") or {}
    label = CAPTCHA_LABELS.get(str(captcha.get("state") or ""))
    if label:
        detail = str(captcha.get("detail") or "")
        return f"{label} · {detail}" if detail else label

    # Losing seats to other buyers is the normal texture of a busy open, and
    # it must not read like a stuck macro — it is the macro working. Ranked
    # above the error-dialog line because during an open it is the common
    # case and the more informative one.
    conflicts = seat.get("takenConflicts") or 0
    if conflicts:
        cooling = seat.get("cooldownSeats") or 0
        tail = f" · 대기 중인 자리 {cooling}석" if cooling else ""
        return f"다른 사람이 먼저 잡은 자리 {conflicts}회 — 바로 다음 자리로 넘어갑니다{tail}"

    # A dialog was covering the map and has been cleared. Worth saying: the
    # run looked frozen for a long time before anything cleared these.
    overlays = seat.get("overlaysDismissed") or 0
    unknown = str(seat.get("unknownDialog") or "").strip()
    if overlays and unknown:
        return f"안내창 {overlays}회 닫고 계속합니다 — {unknown[:52]}"
    if unknown:
        return f"처음 보는 안내창이 떴습니다: {unknown[:70]}"

    opened = str(seat.get("blockEntered") or "").strip()
    misses = seat.get("blockEntryMisses") or 0
    if misses:
        return f"구역을 여는 데 {misses}번 실패했습니다 — 예매 창에서 구역을 직접 열어 주세요."
    if opened:
        return f"구역 {opened} 을(를) 열고 그 안에서 자리를 찾는 중입니다."

    misses = seat.get("aimMisses") or 0
    if misses:
        return f"화면에 그려지지 않아 건너뛴 자리 {misses}석 — 맵을 확대하면 줄어듭니다."

    dialogs = seat.get("seatErrorDialogs") or 0
    if dialogs:
        return f"좌석맵 오류창 {dialogs}회 자동으로 닫았습니다."

    skipped = seat.get("skippedByMap") or 0
    rejects = seat.get("consecutiveRejects") or 0
    if rejects >= 3:
        return (
            f"연속 {rejects}회 거절 — 좌석맵이 보여주는 빈자리를 서버가 거부하고 있습니다. "
            "계속되면 자동으로 멈춥니다."
        )
    if skipped:
        return f"좌석맵이 선택 불가로 표시한 {skipped}석은 건너뛰었습니다."
    # A drawn area holding no seats is silently replaced with the whole
    # venue — seen as "감시 구역: 지정됨 · 0석" while the watch swept
    # everything. An area that does nothing has to say so.
    note = str(seat.get("triggerNote") or "").strip()
    if seat.get("running") and note:
        return note

    if seat.get("watchRectIgnored"):
        return "감시 구역에 좌석이 없습니다 · 전체를 감시하는 중 — [범위 정하기]에서 다시 그어 주세요"

    # The only latency that decides whether a seat is yours: from the
    # moment it opened to the moment we clicked it.
    caught = seat.get("catchLatencyMs") or 0
    if caught:
        via_freed = {"page": " · 예매 창 통신으로 먼저 발견", "poll": ""}.get(
            str(seat.get("lastFreedVia") or ""), ""
        )
        won = {"api": "API 선점", "click": "맵 클릭"}.get(str(seat.get("wonVia") or ""), "")
        verdict = "빠릅니다" if caught <= 400 else "느립니다 — 범위를 좁혀 보세요"
        tail = f" · {won}" if won else ""
        return f"빈자리 발견 후 {caught}ms 만에 잡음{tail}{via_freed} · {verdict}"

    # The gap this whole design turns on: how long the 예매 창 takes to agree
    # that a seat the server already freed is actually free. If this is
    # small the page keeps up on its own; if it is large, that wait is the
    # race we were losing.
    agreed = seat.get("domAgreedMs") or 0
    if agreed and seat.get("running"):
        worst = seat.get("domAgreedWorstMs") or agreed
        nudges = seat.get("nudges") or 0
        note = f"예매 창이 빈자리를 인식하는 데 {agreed}ms (최대 {worst}ms)"
        if nudges:
            note += f" · 맵 새로고침 {nudges}회"
        return note

    # How long one full sweep of what you are watching takes. Losing a race
    # is usually this number rather than click speed: the watch used to poll
    # two blocks per tick across the whole venue, so a 43-block stadium took
    # nearly nine seconds to come back round to any one block.
    watched = seat.get("watchedBlocks") or 0
    ticks = seat.get("sweepTicks") or 0
    if seat.get("running") and watched and ticks:
        # The measured tick, not the configured sleep. A tick is the sleep
        # *plus* the request, and seatStatus costs ~58ms — so reporting
        # ticks x 200ms understated every sweep by about a third, in the
        # one number you read when deciding whether to narrow the range.
        tick = seat.get("observedTickMs") or CATCH_TICK_MS
        sweep = ticks * tick
        note = f"감시 {watched}구역 · 한 바퀴 {sweep}ms"
        # A sweep this long is the race, not a detail. Watching the whole
        # venue means a seat freeing just behind the cursor waits a full
        # lap before we even look at it.
        if sweep >= 1000:
            note += f" · 범위를 정하면 {tick}ms로 줄어듭니다"
        return note

    # Everything after the first ' · ' is the explanation the toast carries.
    parts = message.split(" · ", 1)
    return parts[1] if len(parts) > 1 else ""


def waiting_log_lines(arm: dict[str, Any], limit: int = 12) -> list[str]:
    """What the queue endpoint said, and when, either side of the open.

    The one thing nobody has observed is what `/waiting` returns *before* a show
    opens. If it answers with nothing usable until the flip, then polling across
    the boundary is right and the only question is how densely. If it hands out
    a queue URL beforehand, then arriving at the open is already too late and
    the whole strategy has to move earlier. Those are opposite conclusions, and
    one recorded open decides between them.

    Offsets are signed milliseconds from the target, so the flip is the line
    where the outcome changes.
    """
    entries = arm.get("waitingLog")
    if not isinstance(entries, list) or not entries:
        return []

    rows = [row for row in entries if isinstance(row, dict)]
    if not rows:
        return []

    # Around the flip, not the tail: the entry that first came back usable is
    # the answer, and the few before it are the evidence.
    flip = next(
        (i for i, row in enumerate(rows) if str(row.get("outcome") or "").startswith("대기열")),
        None,
    )
    if flip is None:
        shown = rows[-limit:]
    else:
        start = max(0, flip - (limit - 2))
        shown = rows[start:flip + 2]

    lines = [f"대기열 응답 {len(rows)}회"]
    for row in shown:
        offset = row.get("offsetMs")
        stamp = f"{offset / 1000:+.2f}s" if isinstance(offset, (int, float)) else "  ?  "
        outcome = str(row.get("outcome") or "")
        mark = "  ← 열림" if outcome.startswith("대기열") or outcome.startswith("N (") else ""
        lines.append(f"  {stamp:>7}  {outcome}{mark}")
    return lines
