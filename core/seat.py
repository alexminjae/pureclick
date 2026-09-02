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

# The watch's own sleep, used only as a fallback until the page reports the tick
# it actually measured. A tick is the sleep plus the request — seatStatus
# measures 29ms for two blocks — so this alone understates every sweep.
CATCH_TICK_MS = 100

def watch_note(seat: dict[str, Any]) -> str:
    """The one line under the headline: what needs your attention, or nothing.

    This replaced `running_hint`, which had twenty-one branches and put a
    counter in most of them — 시도 47회, 경합 22회, 한 바퀴 5852ms, 빈자리 발견 후
    380ms. Every one of those moves while the watch runs, and the band is
    repainted twice a second, so the panel churned continuously and the user
    could not read it: "it's updating rapidly now so it's just going crazy".

    A status line is for *state*, and state does not change five times a
    second. So: no numbers, and a sentence only when there is something to do
    about it. The measurements still exist in the published status for
    debugging; they are simply not something to watch while racing.

    Ranked, because when several are true at once only the top one is worth
    acting on.
    """
    seat = seat or {}

    # Nothing can be caught while blocked, and trying extends it.
    if int(seat.get("blockedForMs") or 0) > 0:
        return "접속이 차단되었습니다 — 풀리면 자동으로 다시 시도합니다."

    # A captcha stops everything else until a human answers it.
    label = CAPTCHA_LABELS.get(str((seat.get("captcha") or {}).get("state") or ""))
    if label:
        return label

    unknown = str(seat.get("unknownDialog") or "").strip()
    if unknown:
        return "예매 창에 처음 보는 안내창이 떴습니다 — 확인해 주세요."

    # The venue may be bigger than what is being swept.
    if int(seat.get("batchFailures") or 0):
        return "일부 구역을 못 보고 있습니다 — [감시 중지] 후 다시 시작해 주세요."

    if seat.get("watchRectIgnored"):
        return "감시 구역에 좌석이 없어 전체를 보고 있습니다 — [범위 정하기]에서 다시 그어 주세요."

    if int(seat.get("blockEntryMisses") or 0):
        return "구역을 여는 데 실패하고 있습니다 — 예매 창에서 구역을 직접 열어 주세요."

    if int(seat.get("consecutiveRejects") or 0) >= 3:
        return "좌석맵이 보여주는 빈자리를 서버가 거부하고 있습니다."

    # The trigger's own words when it cannot see the venue's remaining count.
    note = str(seat.get("triggerNote") or "").strip()
    if seat.get("running") and note:
        return note

    if seat.get("running"):
        return "빈자리가 나오면 바로 잡습니다."
    return ""


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


# Why a run stopped, in words. Published as `lastExit` and never once drawn, so
# a run that ended just ended — the panel went quiet and left you guessing
# whether it was still working.
EXIT_REASONS = {
    # Running, not ended — said here so a live run does not read as a silent one.
    "started": "",
    "haltedByUser": "직접 멈춘 상태입니다 — 다시 시작하려면 버튼을 누르세요",
    "soldOut": "빈 좌석이 없어 멈췄습니다 — 취소표를 기다리려면 [감시 시작]",
    "blocked": "접속 차단으로 멈췄습니다",
    "superseded": "새 실행이 시작되어 이전 실행을 멈췄습니다",
    "reservedUserContinues": "좌석을 잡았습니다 — 예매 창에서 결제를 이어가세요",
    "overSelected": "선택된 좌석이 매수를 넘어 멈췄습니다",
    # Losing a seat to someone else is the normal texture of a busy open, and it
    # must not read like a fault.
    "takenByAnother": "다른 사람이 먼저 가져가 다음 자리로 넘어갑니다",
    "advanceWithNoSeat": "가선점이 확인되지 않아 [선택 완료]를 누르지 않았습니다",
    "awaitingManualConfirm": "[선택 완료]를 직접 눌러 주세요",
    "confirmPreselectionInvalid": "가선점 확정에 실패해 좌석을 놓쳤습니다",
    "alreadyHolding": "이미 좌석을 잡고 있어 새 감시를 시작하지 않았습니다",
}

# Past this, the browser is not being read and nothing below it can be trusted.
BRIDGE_STALE_SECONDS = 3.0

# How long a pressed button may go unanswered before the panel says so.
# `browser_host.apply_state` fires every command as `window.PureClick && …` and
# then drops it from the state file whether or not anything was there to run it,
# so a press against a page with no autopilot on it vanished without a trace.
ACK_SECONDS = 3.0

# A trailing "(47)" on an overlay head — the catch loop's attempt counter.
_COUNTER_TAIL = re.compile(r"\s*\(\d+\)\s*$")

# The live band's four tones, as names. `core` must not know the panel's
# palette — the panel maps these onto its own colours.
TONE_GREEN = "green"
TONE_ACCENT = "accent"
TONE_AMBER = "amber"
TONE_FAINT = "faint"


def bridge_status(health: dict[str, Any], now: float | None = None) -> tuple[str, str]:
    """Whether the 예매 창 is being read *right now*, and what to say if not.

    Split out of `bridge_line` so the masthead line and the live band decide it
    the same way. They used to disagree, and only one of them was right: the
    masthead knew the bridge was dead while the status band went on repainting
    whatever it had last been handed, in full colour, under a green dot.

    Returns (state, detail), where state is one of:

      waiting  nothing heard from the host yet — it is still starting up
      lost     the read loop has stopped; `seen_at` is no longer moving
      failing  the loop is alive but cannot read the page — a navigation, an
               origin the autopilot has not been injected into yet, a window
               closing mid-read. `seen_at` keeps moving, so age alone calls
               this healthy; it is not.
      live     the page is being read
    """
    import time as _time

    at = _time.time() if now is None else now
    seen_at = float(health.get("seen_at") or 0)
    if not seen_at:
        return "waiting", "예매 창 연결 대기 중…"

    age = max(0.0, at - seen_at)
    if age > BRIDGE_STALE_SECONDS:
        return "lost", (
            f"예매 창 응답 없음 {age:.0f}초 — 창이 닫혔거나 페이지를 읽지 못하고 있습니다"
        )

    failures = int(health.get("failures") or 0)
    if failures:
        why = str(health.get("last_error") or "").strip()
        return "failing", f"예매 창 읽기 실패 {failures}회{f' · {why}' if why else ''}"

    return "live", f"예매 창 연결됨 · {age:.1f}초 전"


def bridge_line(health: dict[str, Any], context: dict[str, Any] | None = None,
                seat: dict[str, Any] | None = None, now: float | None = None) -> str:
    """One line: is the 예매 창 still being heard from, and what is it showing?

    The panel had no way to answer this. Every failure this session — buttons
    not enabling, the seat table not moving, a button that "doesn't do anything"
    — looks exactly like a frozen bridge from the screen, and there was nothing
    to tell them apart. `poll_context` swallowed its errors and nothing
    timestamped what it published, so a panel reading twenty-minute-old state
    was indistinguishable from a working one.
    """
    state, detail = bridge_status(health, now)
    if state != "live":
        return detail

    where = _describe_page(context or {})
    exit_note = EXIT_REASONS.get(str((seat or {}).get("lastExit") or ""), "").strip()
    line = f"{detail} · {where}"
    if exit_note:
        line = f"{line} · {exit_note}"

    # The platform self-check outranks the rest of this line.
    #
    # Document-start injection is the one hook whose failure is invisible from
    # the screen: the fallback still loads the autopilot on `loaded`, so the
    # panel fills in and everything looks right while the popup shim is missing
    # and the first few hundred milliseconds on the seat map are gone — the one
    # race the macro exists to win. On a machine neither of us has tested, this
    # is the difference between "it doesn't work" and a specific answer.
    hooks = platform_note(health)
    return f"{line} · {hooks}" if hooks else line


def platform_note(health: dict[str, Any] | None) -> str:
    """What the platform hooks did, when any of them did not work."""
    health = health or {}
    trouble: list[str] = []

    if str(health.get("document_start") or "") == "failed":
        why = str(health.get("document_start_error") or "").strip()
        trouble.append("사전 주입 실패 — 좌석 선점이 느려집니다" + (f" ({why[:60]})" if why else ""))

    source = str(health.get("autopilot_source") or "")
    if source.startswith("bundled ("):
        trouble.append(source[len("bundled ("):].rstrip(")"))

    return " · ".join(trouble)


def live_state(
    seat: dict[str, Any] | None,
    health: dict[str, Any] | None = None,
    *,
    asked: tuple[str, float] | None = None,
    now: float | None = None,
) -> tuple[str, str, str]:
    """What the live band should say: (tone, headline, why).

    This is the panel's one claim about what the 예매 창 is doing, and it was
    wrong in three separate ways — every one of which showed up as the same
    symptom: catch a seat, cancel it, press 감시 시작, and the band stays green
    on 좌석 잡음 forever.

    1. The renderer opened with `if not message: return`. `seatState.message` is
       "" on every fresh injection of the autopilot and is only ever written by
       `updateOverlay`, so a reloaded seat page published a perfectly truthful
       "I am idle" and the panel *discarded it* and repainted its last frame.
       There is no early return here. Silence is a state, and it is drawn.

    2. `locked` was tested first, so a press that the page received and
       *refused* — the alreadyHolding branch sets lastError and returns without
       ever setting running — rendered as the green it was refusing to disturb.
       The refusal now outranks the flag, because the refusal explains the flag.

    3. Nothing consulted the bridge's age, so a closed window rendered minutes
       of stale truth in full colour. Nothing below rung 1 is a claim about the
       present unless the browser is actually being read.

    `asked` is (button label, seconds since the press) — see ACK_SECONDS.
    """
    seat = seat or {}
    attempts = int(seat.get("attempts") or 0)
    message = str(seat.get("message") or "").strip()
    tries = f" · 시도 {attempts}회" if attempts else ""

    # 1. Can the browser be seen at all? Everything below is a claim about a
    #    page we may no longer be reading — including a green. A seat "held" by
    #    a window that has closed is not held.
    state, detail = bridge_status(health or {}, now)
    if state != "live":
        if state == "waiting":
            return TONE_FAINT, detail, "예매 창이 아직 시작되지 않았습니다."
        return TONE_AMBER, detail, "아래 숫자는 마지막으로 받은 기록입니다 — 지금 상태가 아닙니다."

    # 2. A press the page never answered.
    if asked:
        label, pending = asked
        if pending > ACK_SECONDS:
            return (
                TONE_AMBER,
                f"[{label}]에 예매 창이 반응하지 않았습니다",
                "예매 창이 좌석맵에 있는지 확인하고 다시 눌러 주세요.",
            )

    # 3. The press arrived and was refused. Above `locked` deliberately: the
    #    refusal *is* about the locked flag, and drawing the flag instead of the
    #    refusal told you the opposite of what had just happened to your press.
    if str(seat.get("lastExit") or "") == "alreadyHolding":
        return (
            TONE_AMBER,
            "감시를 시작하지 않았습니다",
            str(seat.get("lastError") or EXIT_REASONS["alreadyHolding"]),
        )

    if seat.get("locked"):
        raw_held = seat.get("pageSelected")
        held = int(raw_held) if isinstance(raw_held, (int, float)) else -1
        # 4. The page's own cart is empty, so whatever was caught is gone —
        #    cancelled in the 예매 창, or released by the site. This is the
        #    abandon → 취켓팅 moment, and calling it 좌석 잡음 is the stuck green.
        if held == 0:
            return (
                TONE_FAINT,
                "잡았던 좌석을 놓았습니다",
                "[감시 시작]을 누르면 취켓팅을 이어갑니다.",
            )
        # 5. Holding, or unable to tell. `selectedSeatCount()` answers -1 far
        #    more often than 0 on a seat page, so the panel says what it knows
        #    and names the way out rather than performing a certainty it does
        #    not have — a user-initiated run clears a stale lock by itself.
        why = "예매 창에서 결제만 진행하세요. 결제 버튼은 누르지 않습니다."
        if held < 0:
            why += (
                " 좌석을 취소했다면 [감시 시작]을 다시 누르세요 — "
                "잡은 기록을 지우고 이어서 감시합니다."
            )
        return TONE_GREEN, f"좌석 잡음 · {seat.get('lastSeat') or ''}".strip(" ·"), why

    # 6. Running. The head used to be sliced out of `message`, and an empty one
    #    took the whole band down with it (defect 1).
    if seat.get("running"):
        head = message.split(" · ")[0] if message else "감시 중"
        # Strip the page's own attempt count, and do not add ours. The catch
        # loop polls at 100ms while the panel repaints at 500ms, so a running
        # headline read "좌석맵 대기 (47) · 시도 47회" — the same number twice,
        # both jumping by five every frame. A headline names a state; numbers
        # that move belong in watch_vitals, which is monospace and so does not
        # reflow the line every time a digit changes width. The static rungs
        # below keep their count: nothing is incrementing there.
        return TONE_ACCENT, _COUNTER_TAIL.sub("", head), watch_note(seat)

    if seat.get("haltedByUser"):
        return TONE_FAINT, f"멈춤{tries}", "다시 하려면 위 버튼을 누르세요."

    if seat.get("lastError"):
        return TONE_AMBER, "중단됨", str(seat["lastError"])

    return TONE_FAINT, f"대기 중{tries}", ""


def watch_vitals(seat: dict[str, Any] | None) -> str:
    """The watch's pulse, in one line.

    Every number here is published on every tick and none of it was ever drawn,
    so "is it actually watching?" could only be answered by waiting for the
    headline to change — which is precisely what it does not do while the watch
    is sweeping a venue where nothing has happened yet.
    """
    seat = seat or {}
    parts: list[str] = []

    attempts = int(seat.get("attempts") or 0)
    if attempts:
        parts.append(f"시도 {attempts}회")
    blocks = int(seat.get("watchedBlocks") or 0)
    if blocks:
        parts.append(f"구역 {blocks}")
    free = seat.get("freeSeats")
    if isinstance(free, int) and free >= 0:
        parts.append(f"빈자리 {free}")
    ticks = int(seat.get("sweepTicks") or 0)
    if ticks:
        parts.append(f"스윕 {ticks}회")
    conflicts = int(seat.get("takenConflicts") or 0)
    if conflicts:
        parts.append(f"경합 {conflicts}회")
    cooling = int(seat.get("cooldownSeats") or 0)
    if cooling:
        parts.append(f"식힘 {cooling}석")

    return " · ".join(parts)


def _describe_page(context: dict[str, Any]) -> str:
    page = str(context.get("page") or "")
    label = {
        "nol": "공연 페이지",
        "goods": "공연 페이지",
        "seat": "좌석맵",
        "waiting": "대기열",
        "gates": "대기열",
    }.get(page, "다른 페이지")
    goods = str(context.get("goods_code") or "").strip()
    seq = str(context.get("play_seq") or "").strip()
    if goods and seq:
        return f"{label} ({goods} 회차 {seq})"
    return f"{label} ({goods})" if goods else label
