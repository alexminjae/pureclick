"""Can we enter over the API instead of clicking 예매하기?

The queue endpoint sends CORS headers only to https://tickets.interpark.com
(measured). From the NOL product page the burst is impossible, so entry falls
back to clicking 예매하기 — and before a show opens that button does not exist.
Hence the folk remedy of moving the machine's clock forward until it appears,
which invalidates the session's own time-checked tokens and logs you out.

There is a tickets.interpark.com page for the same show and it answers 200. Two
things decide whether the burst can run from there, and both are facts about a
live session rather than things to reason out:

  1. does the 예매 창 stay on that origin, or get bounced back to NOL?
  2. does the login session reach it?

This parks the window there and spends exactly one /waiting request to settle
both. It never enters and never presses anything.

    python3 tools/probe_queue_origin.py [goodsCode]

The show is taken from whatever the panel last armed, unless you name one.
"""

from __future__ import annotations

import json
import re
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
GOODS_ORIGIN = "https://tickets.interpark.com"


def state_path() -> Path:
    for path in (STATE, LOCAL):
        if path.exists():
            return path
    raise SystemExit("예매 창의 상태 파일을 찾지 못했습니다. NOL Sniper를 먼저 실행하세요.")


def show_from(state: dict, override: str | None) -> dict:
    """goods/date/seq for the probe, from the last arm or the current page."""
    arm = state.get("arm") or {}
    ctx = state.get("page_context") or {}
    goods = override or str(arm.get("goods_code") or ctx.get("goods_code") or "")
    if not goods:
        # Last resort: the goods code is in most booking URLs.
        found = re.search(r"/(?:goods|products|ticket)/(\d{6,})", str(ctx.get("url") or ""))
        goods = found.group(1) if found else ""
    if not goods:
        raise SystemExit(
            "공연을 알 수 없습니다. 조작판에서 공연을 한 번 연 뒤 다시 실행하거나, "
            "python3 tools/probe_queue_origin.py 26007442 처럼 상품코드를 직접 주세요."
        )
    return {
        "goods_code": goods,
        "play_date": str(arm.get("play_date") or ctx.get("play_date") or ""),
        "play_seq": str(arm.get("play_seq") or ctx.get("play_seq") or "001"),
        "place_code": str(arm.get("place_code") or ctx.get("place_code") or ""),
    }


def main() -> int:
    path = state_path()
    show = show_from(load_state(path), sys.argv[1] if len(sys.argv) > 1 else None)
    if not re.fullmatch(r"\d{8}", show["play_date"]):
        raise SystemExit(
            f"공연일을 알 수 없습니다 (goods {show['goods_code']}). "
            "조작판에서 날짜·회차를 고른 뒤 다시 실행하세요."
        )

    url = f"{GOODS_ORIGIN}/goods/{show['goods_code']}"
    print(f"예매 창을 {url} 로 보냅니다…")
    with locked_state(path):
        current = load_state(path)
        current["navigate_url"] = url
        save_state(path, current)

    # Let it land and let the autopilot boot on the new origin.
    time.sleep(6)

    # The show, disabled. apply_state pushes this into the new origin's
    # localStorage so the probe can read it — and `enabled: false` means the
    # scheduler refuses to run, so nothing can enter off the back of it.
    print("공연 정보를 새 출처로 보냅니다 (예약은 꺼진 채로)…")
    with locked_state(path):
        current = load_state(path)
        current["arm"] = {
            **show,
            "enabled": False,
            "fired": False,
            "dry_run": False,
            "use_waiting_api": True,
            "channel_code": "pc",
            "pre_sales": "N",
            "target_server_unix": 0,
            "offset_seconds": 0,
        }
        current["command"] = "probe_queue_origin"
        save_state(path, current)

    for _ in range(40):
        time.sleep(0.5)
        seat = ((load_state(path).get("autopilot_status") or {}).get("seat")) or {}
        report = seat.get("queueOriginProbe")
        if not report:
            continue
        print()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("=" * 72)
        print(f"판정: {report.get('verdict')}")
        if report.get("readable"):
            print("→ 이 페이지에서 API 진입이 됩니다. 오픈 전 주차 지점으로 쓸 수 있습니다.")
            return 0
        print("→ 여기서는 API 진입이 안 됩니다. 기존 경로(SSO/게이트)를 씁니다.")
        return 1

    print("예매 창이 시간 안에 답하지 않았습니다.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
