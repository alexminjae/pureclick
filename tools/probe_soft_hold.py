"""Run the soft-hold spike against the 예매 창 that is already open, and print it.

The question:

    Does a bare GraphQL preselectSeat reach the page's own 선택 좌석?

The server does not refuse the mutation — it answers true — and it is tempting
to conclude the hold is therefore usable. That is a different claim. 선택 완료
is refused unless the SPA's own state says a seat is selected, and that state is
set by the page's handler when *its* request resolves. A hold the page never
learned about is the empty-cart failure: 좌석 선택 도중 오류 / P40021, with the
seat locked on the server and counting against the account's allowance.

So this holds exactly one seat, watches every kind of proof the page could give,
hands it straight back, and never presses 선택 완료.

    python3 tools/probe_soft_hold.py

Before running: the 예매 창 must be on /onestop/seat with a 구역 open and seat
circles visible, the watch stopped, and 선택 좌석 empty.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "mac"))

import app_platform  # noqa: E402
from browser_bridge import load_state, locked_state, save_state  # noqa: E402

STATE = app_platform.user_data_dir() / ".nolsniper_browser_state.json"
LOCAL = ROOT / "mac" / ".nolsniper_browser_state.json"
TIMEOUT_S = 40


def state_path() -> Path:
    """Whichever file the running 예매 창 is actually using."""
    if STATE.exists():
        return STATE
    if LOCAL.exists():
        return LOCAL
    raise SystemExit(
        "예매 창의 상태 파일을 찾지 못했습니다. NOL Sniper를 실행하고 "
        "좌석맵을 연 뒤 다시 시도하세요."
    )


def main() -> int:
    path = state_path()
    before = load_state(path)
    seat = ((before.get("autopilot_status") or {}).get("seat")) or {}
    stamp_before = json.dumps(seat.get("softHoldProbe"), ensure_ascii=False, sort_keys=True)

    with locked_state(path):
        current = load_state(path)
        current["command"] = "probe_soft_hold"
        save_state(path, current)
    print(f"명령을 보냈습니다 ({path}). 예매 창에서 좌석 1석을 잡았다가 바로 반납합니다…")

    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(0.5)
        seat = ((load_state(path).get("autopilot_status") or {}).get("seat")) or {}
        report = seat.get("softHoldProbe")
        if report is None:
            continue
        if json.dumps(report, ensure_ascii=False, sort_keys=True) == stamp_before:
            continue  # the previous run's answer, not this one

        print()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print()
        print("=" * 72)
        print(f"판정: {report.get('verdict')}")
        cart = report.get("cartUpdated")
        if cart is True:
            print("→ 예매 창 장바구니가 API 가선점을 인식했습니다.")
        elif cart is False:
            print("→ 장바구니는 움직이지 않았습니다. 이 상태로 선택 완료를 누르면 P40021입니다.")
        released = report.get("released")
        print(f"→ 좌석 반납: {'성공' if released else '실패 — 예매 창에서 [전체삭제]를 눌러 주세요'}")
        return 0 if released is not False else 1

    print("예매 창이 시간 안에 답하지 않았습니다. 좌석맵에 있는지 확인하세요.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
