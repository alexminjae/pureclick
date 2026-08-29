"""A one-request answer to "did anything free anywhere in the venue?"

Measured, on 국립중앙박물관 극장 용 round 097: the whole-venue remaining count
and the seat bitmap agree exactly (202 and 202), and on round 096 they sat two
apart and stayed two apart across eleven samples — a standing offset, not drift.
A constant offset is harmless here, because a trigger cares whether the number
*moved*, not what it is.

Why it matters: seatStatus hard-caps at two blockKeys per request (n>=3 is HTTP
400, measured) and costs ~58ms, so asking the same question by sweeping a
34-block venue takes 17 requests and about 4.4 seconds. This takes one request
and about 132ms.

Why it lives on the Python side: the remains feed is served from
api-ticketfront.interpark.com with no Access-Control-Allow-Origin, so the page
cannot read it — same class of wall as the Date header the in-page clock sync
runs into.

Two traps this has to respect, both seen while establishing the above:

- A round that is not on sale reports remainCnt 0 while the map still holds
  hundreds of seats. Zero is not sold out, and a fall to zero is not a signal.
- Block keys embed the round, and seatStatus keys off the blockKey rather than
  the playSeq parameter — so the round has to be pinned on both sides or the
  two describe different things and agree by accident at best.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TriggerState:
    """What the last look at the remains feed established."""

    total: int
    changed_at: float
    seen_at: float
    usable: bool
    note: str = ""

    def to_mapping(self) -> dict[str, object]:
        return {
            "total": self.total,
            "changed_at": self.changed_at,
            "seen_at": self.seen_at,
            "usable": self.usable,
            "note": self.note,
        }


def next_trigger_state(
    previous: TriggerState | None,
    total: int | None,
    *,
    hide_remain: bool = False,
    now: float | None = None,
) -> TriggerState:
    """Fold one reading of the whole-venue remaining count into the trigger.

    `total` is None when the reading failed. A failed or unusable reading must
    leave the watch sweeping as it did before rather than going quiet — being
    unable to see is not the same as seeing nothing.
    """
    at = time.time() if now is None else now
    previous_changed = previous.changed_at if previous else 0.0

    if hide_remain:
        return TriggerState(
            total=previous.total if previous else 0,
            changed_at=previous_changed,
            seen_at=at,
            usable=False,
            note="이 공연은 잔여석을 공개하지 않습니다 — 전체 감시로 진행합니다",
        )
    if total is None:
        return TriggerState(
            total=previous.total if previous else 0,
            changed_at=previous_changed,
            seen_at=at,
            usable=False,
            note="잔여석 조회 실패 — 전체 감시로 진행합니다",
        )
    # A round that is not on sale reads 0 with a map full of seats, so zero
    # carries no information and a fall to it is not a cancellation.
    if total <= 0:
        return TriggerState(
            total=0,
            changed_at=previous_changed,
            seen_at=at,
            usable=False,
            note="잔여석 0 — 판매 중이 아닌 회차일 수 있어 전체 감시로 진행합니다",
        )

    if previous is None or not previous.usable:
        # First usable reading: a baseline, not an event.
        return TriggerState(total=total, changed_at=previous_changed, seen_at=at, usable=True)
    if total == previous.total:
        return TriggerState(
            total=total, changed_at=previous.changed_at, seen_at=at, usable=True
        )
    return TriggerState(total=total, changed_at=at, seen_at=at, usable=True)
