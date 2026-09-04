"""The one state the panel, the live band and the in-page overlay all read.

Before this, three places each decided what the app was doing: `live_state`
from the seat status, `_update_guidance` from the page, and the overlay from
whatever `updateOverlay` was last called with. They disagreed — 좌석 잡음 over
a 대기 중 guidance, a stale arm error over a fresh seat — and the user had to
guess which one was true. Now the mode is derived once, here, from explicit
facts, and everything else is a rendering of it.

Nothing here does I/O or knows about tkinter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

# Every mode the app can be in. The order is the order of precedence when more
# than one fact is true at once: a held seat outranks a running watch, which
# outranks an armed scheduler, and so on down to "nothing is happening".
MODES = (
    "offline",      # the 예매 창 is not being read
    "held",         # a seat is selected and locked for the user
    "grabbing",     # the seat autopilot is choosing a seat right now
    "watching",     # 취켓팅: watching the map for a freed seat
    "armed",        # the scheduler is counting down to 티켓 오픈
    "entering",     # in the queue / gate on the way to the seat map
    "on_schedule",  # on 일정 선택, the round is being applied
    "halted",       # the user pressed stop; nothing restarts on its own
    "error",        # the last thing tried failed and nothing is running
    "on_seat",      # on the seat map with nothing running
    "ready",        # on a show page, nothing armed
    "no_show",      # not on any show
)

MODE_LABELS = {
    "offline": "연결 안 됨",
    "held": "좌석 잡음",
    "grabbing": "좌석 잡는 중",
    "watching": "취켓팅 중",
    "armed": "오픈 대기 중",
    "entering": "진입 중",
    "on_schedule": "회차 맞추는 중",
    "on_seat": "좌석맵",
    "halted": "중지됨",
    "error": "문제 발생",
    "ready": "준비됨",
    "no_show": "공연 없음",
}

# Sale phases, from the show's own 티켓 오픈 time.
BEFORE_OPEN = "before_open"
OPEN = "open"
UNKNOWN = "unknown"


def sale_phase(opens: datetime | None, now: datetime) -> str:
    if opens is None:
        return UNKNOWN
    return OPEN if now >= opens else BEFORE_OPEN


def derive_mode(
    *,
    page: str,
    url: str = "",
    seat: Mapping[str, Any] | None,
    arm: Mapping[str, Any] | None,
    bridge_live: bool = True,
) -> str:
    """Which mode the facts add up to. Precedence is the order of MODES."""
    seat = seat or {}
    arm = arm or {}
    if not bridge_live:
        return "offline"
    page = str(page or "")
    url = str(url or "")
    if seat.get("locked"):
        held = seat.get("pageSelected")
        # -1 / None: the page has not counted yet; trust the lock. 0: the user
        # let the seat go and the lock is stale.
        if not (isinstance(held, (int, float)) and held == 0):
            return "held"
    if seat.get("running"):
        return "watching" if str(seat.get("runMode") or "") == "catch" else "grabbing"
    if arm.get("running") and not arm.get("fired"):
        return "armed"
    if page in {"waiting", "gates"} or (arm.get("fired") and page in {"nol", "goods"}):
        return "entering"
    if "/onestop/schedule" in url:
        return "on_schedule"
    # /onestop?key=… and /onestop/… hops between the queue and the seat map
    # are still the entry, not "no show here".
    if "/onestop" in url and page != "seat":
        return "entering"
    # A stop or a fault outranks "you are on the seat map": both say why
    # nothing is happening there, which is what the user needs to read.
    if seat.get("haltedByUser"):
        return "halted"
    if str(seat.get("lastError") or "").strip() or str(arm.get("lastError") or "").strip():
        return "error"
    if page == "seat":
        return "on_seat"
    if page in {"nol", "goods"}:
        return "ready"
    return "no_show"


@dataclass(frozen=True)
class Guidance:
    banner: str          # the mode, as the user reads it ("판매 중 · 준비됨")
    instruction: str     # one sentence: what to do now
    primary: str         # "arm" | "enter" | "catch" | "stop" | ""
    arm_reason: str      # why 오픈에 자동 진입 is off, or ""
    enter_reason: str    # why 지금 진입 is off, or ""


def guidance(
    mode: str,
    phase: str,
    *,
    round_picked: bool,
    auto_seats: bool,
    open_text: str = "",
    reason: str = "",
) -> Guidance:
    """One banner, one instruction, one primary action — from the mode alone.

    The instruction never argues with the banner: both come from the same two
    inputs, and nothing else (no page heuristics, no message text) gets a say.
    """
    label = MODE_LABELS.get(mode, mode)
    phase_text = {BEFORE_OPEN: "오픈 예정", OPEN: "판매 중", UNKNOWN: "오픈 시각 미확인"}[phase]
    before = phase == BEFORE_OPEN
    arm_off = "" if before or phase == UNKNOWN else "이미 열린 공연이라 쓰지 않습니다"
    enter_off = "아직 오픈 전이라 쓰지 않습니다 (오픈 시각에 자동으로 들어갑니다)" if before else ""

    if mode == "offline":
        return Guidance("연결 안 됨", "예매 창이 응답하지 않습니다. 아래 [예매 창 다시 열기]를 누르세요.", "",
                        "예매 창 연결 후 사용", "예매 창 연결 후 사용")
    if mode == "no_show":
        return Guidance("공연 없음", "예매 창에서 공연 페이지를 열면 여기가 자동으로 채워집니다.", "",
                        "공연을 먼저 여세요", "공연을 먼저 여세요")
    if mode == "held":
        return Guidance(f"{label}", "예매 창에서 결제만 진행하세요. 결제 버튼은 누르지 않습니다.", "stop",
                        "좌석을 잡은 뒤에는 쓰지 않습니다", "좌석을 잡은 뒤에는 쓰지 않습니다")
    if mode == "grabbing":
        return Guidance(label, "좌석을 고르는 중입니다. 보안문자가 뜨면 직접 입력하세요.", "stop",
                        "진행 중", "진행 중")
    if mode == "watching":
        return Guidance(label, "자리가 나오면 즉시 잡습니다. 멈추려면 [전부 정지].", "stop",
                        "진행 중", "진행 중")
    if mode == "armed":
        when = f" ({open_text})" if open_text else ""
        return Guidance(f"오픈 대기 중{when}", "오픈 순간 자동으로 들어갑니다. 그대로 두세요. 취소하려면 [대기 중지].", "stop",
                        "이미 대기 중", "이미 대기 중")
    if mode == "entering":
        return Guidance(label, "대기열을 통과하는 중입니다. 아무것도 누르지 마세요.", "",
                        "진행 중", "진행 중")
    if mode == "on_schedule":
        return Guidance(label, "고른 회차를 자동으로 맞추는 중입니다.", "", "진행 중", "진행 중")
    if mode == "on_seat":
        todo = ("곧 좌석을 잡습니다. 보안문자만 직접 입력하세요." if auto_seats
                else "[감시 시작]을 누르면 고른 범위에서 자리를 잡습니다.")
        return Guidance(label, todo, "catch", "좌석맵에서는 쓰지 않습니다", "좌석맵에서는 쓰지 않습니다")
    if mode == "halted":
        return Guidance(label, "멈췄습니다. 다시 하려면 [지금 진입] 또는 [감시 시작]을 누르세요.",
                        "enter" if not before else "arm", arm_off, enter_off)
    if mode == "error":
        why = reason.strip() or "마지막 시도가 실패했습니다."
        return Guidance(label, f"{why} 확인 후 다시 누르세요.", "enter" if not before else "arm",
                        arm_off, enter_off)
    # ready
    if not round_picked:
        return Guidance(f"{phase_text} · {label}", "①에서 날짜·회차를 먼저 고르세요.",
                        "arm" if before else "enter", arm_off, enter_off)
    if before:
        when = f" 티켓 오픈 {open_text}." if open_text else ""
        return Guidance(f"{phase_text} · {label}",
                        f"[오픈에 자동 진입]을 누르고 기다리세요.{when} 오픈 순간 자동으로 들어갑니다.",
                        "arm", arm_off, enter_off)
    if phase == OPEN:
        return Guidance(f"{phase_text} · {label}",
                        "[지금 진입]을 누르세요. 대기열을 지나 고른 회차의 좌석맵으로 갑니다.",
                        "enter", arm_off, enter_off)
    return Guidance(f"{phase_text} · {label}",
                    "티켓 오픈 시각을 모릅니다. ②에 시각을 넣고 [오픈에 자동 진입], 이미 열렸으면 [지금 진입].",
                    "enter", "", "")
