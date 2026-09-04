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
