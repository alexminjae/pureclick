"""How a booking session is actually opened, as of 2026-09-04.

Measured against the live site, logged in over NOL/Naver SSO:

  GET  api-ticketfront…/v1/goods/{code}/waiting   → 401, "오랜 시간 이용하지 않아
       자동 로그아웃되었습니다" — for *every* show, open or not. That endpoint
       authenticates against the old ticketfront realm, which an SSO login never
       populates, so the route the app was built on cannot succeed at all.

  POST ent-waiting-api…/waiting/api/secure-url    → 200 in ~33ms, carrying
       {"redirectUrl": "https://tickets.interpark.com/waiting?key=…"}

So entry is two calls, not a button:

  1. GET  {GOODS_ORIGIN}/api/ticket/v2/reserve-gate/member-info?goodsCode&channelCode
     → {signature, secureData, …}. The signature is stamped with its issue time
       (`…<hex>.<unix>`), so it is minted near the fire, not at arm time.
  2. POST {ENT_WAITING_ORIGIN}/waiting/api/secure-url with that pair
     → the queue URL to navigate to.

Both must be made from an interpark.com page: member-info needs the
`.interpark.com` session cookie, and a cross-site request from nol.yanolja.com
does not send it (measured: 401 from that origin, while the request itself
completes — the block is SameSite, not CORS). Hence `needs_parking`.

Nothing here does I/O. The requests are issued by the page itself, in
browser/nolsniper_autopilot.js; this module exists so the panel and the tests
can agree on the shapes without a browser.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# Where the session lives, and the only origin the credential can be minted on.
GOODS_ORIGIN = "https://tickets.interpark.com"
# The queue host. Deliberately not the same service as api-ticketfront.
ENT_WAITING_ORIGIN = "https://ent-waiting-api.interpark.com"

MEMBER_INFO_PATH = "/api/ticket/v2/reserve-gate/member-info"
GOODS_INFO_PATH = "/api/ticket/v2/reserve-gate/goods-info"
SECURE_URL_PATH = "/waiting/api/secure-url"
# What the waiting room does with the key (read out of its bundle,
# tickets.interpark.com/waiting, 2026-09-05): line-up assigns the place in
# line, rank reports it and carries the onestop URL when the turn comes.
#
#   POST {ENT_WAITING_ORIGIN}/waiting/api/line-up   {"key": <key from redirectUrl>}
#        → {"exist": bool, "userSeq": int, "waitingId": "a:b:<userSeq>", "clientReq": {...}, "error"?: str}
#   GET  {ENT_WAITING_ORIGIN}/waiting/api/rank?waitingId=<waitingId>
#        → {"myRank": int|None, "totalRank": int, "oneStopUrl": str, "sessionId": str, "bookingRate": n, "error"?: str}
#
# The page itself makes line-up only after its ~1.3MB of JS has booted and then
# polls rank every 2–3s; the position is decided at line-up, so both are worth
# making from the goods page the instant secure-url answers.
LINE_UP_PATH = "/waiting/api/line-up"
RANK_PATH = "/waiting/api/rank"
# rank answers -1/-1 for a waiting id the server no longer knows.
EXPIRED_RANK = -1
# Measured 2026-09-05 with tools/probe_entry_chain.py on a quiet open show,
# warm connections: member-info 60ms, secure-url 9ms, line-up 14ms, rank 12–17ms.
# After line-up the booking session takes ~1.7s to exist: rank reports
# myRank 1 / totalRank 1 with no sessionId until then, and then totalRank 0,
# sessionId and oneStopUrl together. line-up is not idempotent — the same key
# lined up twice answered two userSeq — so a navigation to the waiting page
# after a line-up here lines up again, at a later place.
SESSION_APPEARS_AFTER_MS = 1700
LINE_UP_IDEMPOTENT = False
# The member-info signature was still accepted by secure-url at 121 s, 301 s and
# 601 s of age (same probe). A lower bound, not the limit.
SIGNATURE_VALID_FOR_S = 600

# The partner code NOL's own 예매하기 puts in the gate URL (bc=61776). It is the
# *partner* biz code and is not the goods' own bizCode, which goods-info reports
# separately — sending the latter is a 400.
NOL_BIZ_CODE = "61776"

# What secure-url answers with before 티켓 오픈. A refusal to retry past, not an
# error: it is the normal state of a show that has not opened yet.
NOT_OPEN_ERROR = "UnableReservationTime"
# 비정상 예매 차단. The modern spelling of the old "BL".
BLOCKED_ERROR = "AccessDenied_Blacklist"


def park_url(goods_code: str) -> str:
    """Where the 예매 창 must be sitting before a fire can be attempted."""
    return f"{GOODS_ORIGIN}/goods/{str(goods_code).strip()}"


def is_entry_origin(url: str) -> bool:
    """True when `url` is somewhere the credential can be minted."""
    return str(url or "").startswith(GOODS_ORIGIN + "/")


def needs_parking(url: str) -> bool:
    """True when the page must be moved before arming.

    A blank URL counts as needing it: an unknown page is not a page we have
    established can mint a credential, and moving is cheap.
    """
    return not is_entry_origin(url)


def member_info_url(goods_code: str, *, channel_code: str = "pm") -> str:
    return (
        f"{MEMBER_INFO_PATH}?goodsCode={str(goods_code).strip()}"
        f"&channelCode={channel_code}"
    )


def secure_url_payload(
    *,
    signature: str,
    secure_data: str,
    goods_code: str,
    play_seq: str,
    biz_code: str = NOL_BIZ_CODE,
    pre_sales: str = "N",
    lang: str = "ko",
    pass_code: str = "",
    origin: str = "NOL",
) -> dict[str, Any]:
    """The POST body, in the shape the gate's own bundle builds it.

    `playSeq` alone identifies the round — measured: secure-url takes no
    playDate at all. The date is only how a human picks the seq, which is what
    `pick_play_seq` is for.
    """
    return {
        "signature": signature,
        "secureData": secure_data,
        "lang": lang,
        "passCode": pass_code,
        "from": origin,
        "goodsCode": str(goods_code).strip(),
        "bizCode": str(biz_code).strip() or NOL_BIZ_CODE,
        "playSeq": str(play_seq).strip(),
        "preSales": pre_sales or "N",
    }


def decide_line_up(answer: Mapping[str, Any] | None, status: int = 200) -> dict[str, Any]:
    """What to do with a line-up answer: poll the rank, or fall back to the page.

    `exist` means the key was lined up already (a second window, a retry after
    a navigation): the waiting page shows "already waiting" for that and offers
    a new wait — never a place in line — so the sniper treats it as its own
    earlier line-up and polls the rank it has, or falls back to the page when
    it has none.
    """
    data = dict(answer or {})
    error = str(data.get("error") or "").strip()
    waiting_id = str(data.get("waitingId") or "").strip()
    if status != 200 or error or not waiting_id:
        return {"action": "fallback", "reason": error or (f"HTTP {status}" if status != 200 else "no waitingId"),
                "waiting_id": "", "user_seq": None, "exist": bool(data.get("exist"))}
    seq = data.get("userSeq")
    if seq is None and waiting_id.count(":") >= 2:
        try:
            seq = int(waiting_id.split(":")[2])
        except ValueError:
            seq = None
    return {"action": "poll", "reason": "", "waiting_id": waiting_id, "user_seq": seq, "exist": bool(data.get("exist"))}


def decide_rank(answer: Mapping[str, Any] | None, status: int = 200) -> dict[str, Any]:
    """What to do with a rank answer, in the page's own order of precedence.

    A onestop URL is the turn: go, whatever the rank says. -1/-1 is a waiting
    id the server has forgotten — nothing more to poll. myRank 0 without a URL
    and without a session is the page's ExpiredExistedSession. Anything else is
    a place in line: keep polling.
    """
    data = dict(answer or {})
    error = str(data.get("error") or "").strip()
    if status != 200 or error:
        return {"action": "fallback", "reason": error or f"HTTP {status}", "url": "", "my_rank": None, "total_rank": None}
    url = str(data.get("oneStopUrl") or "").strip()
    my_rank = data.get("myRank")
    total = data.get("totalRank")
    if url.startswith(("http://", "https://")):
        return {"action": "go", "reason": "", "url": url, "my_rank": my_rank, "total_rank": total}
    if my_rank == EXPIRED_RANK and total == EXPIRED_RANK:
        return {"action": "fallback", "reason": "ExpiredSession", "url": "", "my_rank": my_rank, "total_rank": total}
    if my_rank == 0 and not str(data.get("sessionId") or "").strip():
        return {"action": "fallback", "reason": "ExpiredExistedSession", "url": "", "my_rank": my_rank, "total_rank": total}
    return {"action": "poll", "reason": "", "url": "", "my_rank": my_rank, "total_rank": total}


def pick_play_seq(
    play_seq_list: Iterable[Mapping[str, Any]],
    *,
    play_date: str = "",
    play_seq: str = "",
) -> dict[str, Any] | None:
    """The round to enter for, out of goods-info's `playSeqList`.

    An explicit `play_seq` wins outright — that is the panel's choice and second
    guessing it on a date mismatch would enter a different round than the one on
    screen. Otherwise the first round on the wanted date, and failing that
    nothing: guessing a round is worse than saying the date is not on sale.
    """
    rounds = [dict(row) for row in play_seq_list or [] if isinstance(row, Mapping)]
    wanted_seq = str(play_seq or "").strip()
    if wanted_seq:
        for row in rounds:
            if str(row.get("playSeq") or "").strip() == wanted_seq:
                return row
        return None
    wanted_date = str(play_date or "").strip().replace("-", "")
    if not wanted_date:
        return None
    for row in rounds:
        if str(row.get("playDate") or "").strip() == wanted_date:
            return row
    return None


def describe_secure_url_error(error: str, status: int = 0) -> str:
    """What to put on the panel when a fire is refused, in Korean.

    The two that matter are told apart because the responses to them differ: one
    is "wait", the other is "stop and do not retry".
    """
    code = str(error or "").strip()
    if code == NOT_OPEN_ERROR:
        return "아직 오픈 전입니다 (UnableReservationTime)"
    if code == BLOCKED_ERROR:
        return "비정상 예매로 차단되었습니다 — 재시도하지 마세요"
    if code:
        return f"대기열 거절 · {code}"
    return f"대기열 거절 · HTTP {status}" if status else "대기열 거절"
