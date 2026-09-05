"""The entry route, as measured against the live site on 2026-09-04.

The numbers these encode are not preferences: /v1/goods/{code}/waiting answered
401 for every show under an SSO login, and secure-url answered 200 in ~33ms with
a queue URL. These tests exist so a future edit cannot quietly go back.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.arm import ArmPayload  # noqa: E402
from core.entry import (  # noqa: E402
    BLOCKED_ERROR,
    GOODS_ORIGIN,
    NOL_BIZ_CODE,
    NOT_OPEN_ERROR,
    describe_secure_url_error,
    is_entry_origin,
    needs_parking,
    park_url,
    pick_play_seq,
    secure_url_payload,
)


def test_park_url_is_the_origin_that_holds_the_session():
    assert park_url("26012910") == "https://tickets.interpark.com/goods/26012910"
    assert park_url("  26012910 ") == "https://tickets.interpark.com/goods/26012910"


def test_a_nol_product_page_needs_parking_and_the_goods_page_does_not():
    # Measured: member-info is 401 from nol.yanolja.com because no
    # .interpark.com cookie is sent cross-site, so the product page can never
    # mint a credential no matter how long it waits.
    assert needs_parking("https://nol.yanolja.com/ticket/products/26012910")
    assert not needs_parking("https://tickets.interpark.com/goods/26012910")
    assert not needs_parking("https://tickets.interpark.com/gates/ticket/26007416?bc=61776")


def test_an_unknown_page_is_treated_as_needing_parking():
    # Not knowing where we are is not evidence that we are somewhere that works.
    assert needs_parking("")
    assert needs_parking(None)  # type: ignore[arg-type]


def test_a_lookalike_origin_is_not_the_entry_origin():
    assert not is_entry_origin("https://tickets.interpark.com.evil.example/goods/1")
    assert not is_entry_origin("https://tickets.interpark.com")  # no path, not a page


def test_secure_url_payload_carries_the_pair_and_the_seq_but_no_date():
    body = secure_url_payload(
        signature="sig.123", secure_data="data", goods_code="26012910", play_seq="001")
    assert body["signature"] == "sig.123"
    assert body["secureData"] == "data"
    assert body["playSeq"] == "001"
    assert body["bizCode"] == NOL_BIZ_CODE
    # Measured: secure-url takes no playDate at all. Sending one would be
    # inventing a field, and inventing fields is how a 400 arrives at the open.
    assert "playDate" not in body


def test_a_blank_biz_code_falls_back_rather_than_sending_nothing():
    body = secure_url_payload(
        signature="s", secure_data="d", goods_code="1", play_seq="001", biz_code="  ")
    assert body["bizCode"] == NOL_BIZ_CODE


ROUNDS = [
    {"playSeq": "001", "playDate": "20261011", "playTime": "1300"},
    {"playSeq": "002", "playDate": "20261011", "playTime": "1800"},
    {"playSeq": "003", "playDate": "20261012", "playTime": "1400"},
]


def test_an_explicit_seq_wins_over_the_date():
    assert pick_play_seq(ROUNDS, play_date="20261012", play_seq="001")["playSeq"] == "001"


def test_a_date_alone_takes_the_first_round_on_it():
    assert pick_play_seq(ROUNDS, play_date="20261011")["playSeq"] == "001"
    assert pick_play_seq(ROUNDS, play_date="2026-10-12")["playSeq"] == "003"


def test_a_round_that_is_not_there_is_none_rather_than_a_guess():
    # Entering a different round than the one on screen is worse than refusing.
    assert pick_play_seq(ROUNDS, play_seq="099") is None
    assert pick_play_seq(ROUNDS, play_date="20261225") is None
    assert pick_play_seq([], play_date="20261011") is None


def test_the_two_refusals_are_told_apart():
    assert "오픈 전" in describe_secure_url_error(NOT_OPEN_ERROR)
    assert "재시도하지" in describe_secure_url_error(BLOCKED_ERROR)
    assert "418" in describe_secure_url_error("", status=418)


def test_the_arm_carries_a_biz_code_through_the_round_trip():
    payload = ArmPayload(
        enabled=True, goods_code="26012910", play_date="20261011", play_seq="001",
        target_server_unix=1.0, offset_seconds=0.0)
    assert payload.biz_code == NOL_BIZ_CODE
    again = ArmPayload.from_mapping({**payload.to_mapping(), "biz_code": "99999"})
    assert again.biz_code == "99999"
    # camelCase, because the page writes the arm back in its own spelling. Only
    # when snake_case is absent: an explicit biz_code is the panel's own value
    # and must not be overridden by whatever the page echoed back.
    page_spelling = {k: v for k, v in payload.to_mapping().items() if k != "biz_code"}
    assert ArmPayload.from_mapping({**page_spelling, "bizCode": "12345"}).biz_code == "12345"
    assert ArmPayload.from_mapping(
        {**payload.to_mapping(), "bizCode": "12345"}).biz_code == NOL_BIZ_CODE


def test_the_goods_origin_is_the_one_the_credential_is_minted_on():
    assert GOODS_ORIGIN == "https://tickets.interpark.com"


# ── The queue's own two calls, read out of the waiting room's bundle ──────────
from core.entry import EXPIRED_RANK, LINE_UP_PATH, RANK_PATH, decide_line_up, decide_rank  # noqa: E402


def test_the_queue_paths_are_the_waiting_rooms_own():
    assert LINE_UP_PATH == "/waiting/api/line-up"
    assert RANK_PATH == "/waiting/api/rank"
    assert EXPIRED_RANK == -1


def test_a_line_up_answer_with_a_waiting_id_is_polled():
    got = decide_line_up({"exist": False, "userSeq": 17, "waitingId": "26012391:pc:17", "clientReq": {"goodsCode": "26012391"}})
    assert got["action"] == "poll"
    assert got["waiting_id"] == "26012391:pc:17"
    assert got["user_seq"] == 17
    assert got["exist"] is False


def test_the_user_seq_falls_back_to_the_waiting_id():
    # The page derives userSeq from waitingId.split(":")[2] when it resumes.
    got = decide_line_up({"waitingId": "a:b:2999"})
    assert got["user_seq"] == 2999
    assert decide_line_up({"waitingId": "no-colons"})["user_seq"] is None


def test_an_existing_line_up_is_still_polled_when_it_has_an_id():
    # exist=true is "already lined up": keep the place, never a new wait.
    got = decide_line_up({"exist": True, "userSeq": 3, "waitingId": "x:y:3"})
    assert got["action"] == "poll" and got["exist"] is True


def test_a_line_up_that_fails_falls_back_to_the_page():
    assert decide_line_up({"error": "AccessDenied_T01"})["action"] == "fallback"
    assert decide_line_up({"error": "AccessDenied_T01"})["reason"] == "AccessDenied_T01"
    assert decide_line_up({"exist": True}, 200)["action"] == "fallback"       # no waitingId to poll
    assert decide_line_up({"waitingId": "a:b:1"}, 500)["action"] == "fallback"
    assert decide_line_up({"waitingId": "a:b:1"}, 500)["reason"] == "HTTP 500"
    assert decide_line_up(None)["action"] == "fallback"


def test_a_onestop_url_is_the_turn_whatever_the_rank_says():
    got = decide_rank({"myRank": 0, "totalRank": 0, "oneStopUrl": "https://tickets.interpark.com/onestop?key=k", "sessionId": "s"})
    assert got["action"] == "go" and got["url"].startswith("https://tickets.interpark.com/onestop")
    # Even with a rank still showing, the URL wins (the page's own order).
    assert decide_rank({"myRank": 12, "totalRank": 500, "oneStopUrl": "https://x/onestop"})["action"] == "go"


def test_a_place_in_line_keeps_polling():
    got = decide_rank({"myRank": 2999, "totalRank": 3400, "oneStopUrl": "", "sessionId": "s"})
    assert got["action"] == "poll" and got["my_rank"] == 2999 and got["total_rank"] == 3400


def test_the_pages_expired_states_fall_back():
    assert decide_rank({"myRank": -1, "totalRank": -1})["reason"] == "ExpiredSession"
    assert decide_rank({"myRank": 0, "totalRank": 5, "sessionId": ""})["reason"] == "ExpiredExistedSession"
    assert decide_rank({"error": "ServiceUnavailable_R02"})["action"] == "fallback"
    assert decide_rank({"myRank": 1}, 502)["reason"] == "HTTP 502"
    # rank 0 WITH a session and no URL yet: still waiting, not expired.
    assert decide_rank({"myRank": 0, "totalRank": 5, "sessionId": "s"})["action"] == "poll"


def test_the_measured_queue_facts_are_recorded():
    # tools/probe_entry_chain.py, 2026-09-05: the session appears ~1.7s after
    # line-up, and lining the same key up twice makes two entries.
    from core.entry import LINE_UP_IDEMPOTENT, SESSION_APPEARS_AFTER_MS
    assert LINE_UP_IDEMPOTENT is False
    assert 1000 <= SESSION_APPEARS_AFTER_MS <= 3000


def test_the_signature_validity_lower_bound_is_recorded():
    from core.entry import SIGNATURE_VALID_FOR_S
    assert SIGNATURE_VALID_FOR_S >= 600
