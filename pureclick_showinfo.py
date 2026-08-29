"""Show metadata lookup against the public Interpark ticketfront API.

The seat map itself is session-bound, but everything needed to *configure* a
snipe — engine type, grade names, remaining counts, round list, open time — is
available unauthenticated. Fetching it here rather than from the page matters
for two reasons:

* `api-ticketfront.interpark.com` only sends CORS headers for
  `https://tickets.interpark.com`; an in-page fetch from a NOL product page is
  rejected with 403.
* NOL product pages do not embed `ticketOpenDate` or the engine flags at all,
  so scraping the HTML cannot recover them.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from pureclick_seat_core import (
    ShowCompatibility,
    parse_goods_code,
    parse_summary_compatibility,
)

API_ORIGIN = "https://api-ticketfront.interpark.com"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# The API rejects requests without an interpark referer.
API_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Referer": "https://tickets.interpark.com/",
    "Origin": "https://tickets.interpark.com",
}


class ShowInfoError(Exception):
    """Raised when show metadata cannot be retrieved."""


@dataclass
class GradeInfo:
    grade: str
    name: str
    price: int = 0
    remain: int = 0

    def to_mapping(self) -> dict[str, Any]:
        return {"grade": self.grade, "name": self.name, "price": self.price, "remain": self.remain}


@dataclass
class ShowCatalog:
    goods_code: str
    goods_name: str = ""
    place_name: str = ""
    place_code: str = ""
    genre_name: str = ""
    play_start_date: str = ""
    play_end_date: str = ""
    ticket_open_raw: str = ""
    ticket_open_kst: str = ""
    compatibility: ShowCompatibility | None = None
    grades: list[GradeInfo] = field(default_factory=list)
    play_seqs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def flow(self) -> str:
        return self.compatibility.flow_label if self.compatibility else "unknown"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "goods_code": self.goods_code,
            "goods_name": self.goods_name,
            "place_name": self.place_name,
            "place_code": self.place_code,
            "genre_name": self.genre_name,
            "play_start_date": self.play_start_date,
            "play_end_date": self.play_end_date,
            "ticket_open_date": self.ticket_open_raw,
            "ticket_open_kst": self.ticket_open_kst,
            "flow": self.flow,
            "flow_description": self.compatibility.flow_description if self.compatibility else "",
            "needs_seat_picking": bool(self.compatibility and self.compatibility.needs_seat_picking),
            "supports_seat_autopilot": bool(self.compatibility and self.compatibility.supports_seat_autopilot),
            "is_captcha": bool(self.compatibility and self.compatibility.is_captcha),
            "hide_remain_seat": bool(self.compatibility and self.compatibility.hide_remain_seat),
            "grades": [grade.to_mapping() for grade in self.grades],
            "play_seqs": list(self.play_seqs),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def format_open_time(raw: str) -> str:
    """Turn a `yyyyMMddHHmm` ticketOpenDate into `YYYY-MM-DD HH:MM:SS`."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) < 12:
        return ""
    return (
        f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} "
        f"{digits[8:10]}:{digits[10:12]}:00"
    )


def unwrap(payload: Any) -> Any:
    """Return the body of a ticketfront response.

    Most endpoints wrap results in `{common, data}`, but `prices/group` returns
    its grade map at the top level, so both shapes have to be accepted.
    """
    if isinstance(payload, dict) and "data" in payload and "common" in payload:
        return payload.get("data")
    return payload


def parse_price_groups(payload: Any) -> dict[str, dict[str, Any]]:
    """Map seatGrade code to its name and highest base price.

    `prices/group` is keyed by grade name, then by price-type name
    (기본가/조기예매 etc.), each holding a list of rows. The highest price wins so
    discount tiers do not mask the headline price.
    """
    prices: dict[str, dict[str, Any]] = {}
    source = unwrap(payload)
    if not isinstance(source, dict):
        return prices
    for group_name, by_type in source.items():
        if not isinstance(by_type, dict):
            continue
        for rows in by_type.values():
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                grade = str(row.get("seatGrade") or "")
                if not grade:
                    continue
                price = int(row.get("salesPrice") or 0)
                entry = prices.setdefault(
                    grade,
                    {"price": 0, "name": str(row.get("seatGradeName") or group_name or grade)},
                )
                if price > entry["price"]:
                    entry["price"] = price
    return prices


def parse_remain_rows(payload: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract remaining-seat rows and the distinct rounds they cover."""
    rows = []
    source = unwrap(payload)
    for row in (source.get("remainSeat") or []) if isinstance(source, dict) else []:
        if isinstance(row, dict):
            rows.append(row)
    play_seqs = sorted({str(row.get("playSeq") or "") for row in rows} - {""})
    return rows, play_seqs


def merge_grades(
    remain_rows: list[dict[str, Any]],
    prices: dict[str, dict[str, Any]],
    *,
    play_seq: str | None = None,
) -> list[GradeInfo]:
    """Collapse remain rows into one entry per grade.

    When ``play_seq`` is set, only that round is counted — summing every
    round on a date made multi-show days look wrong, and using the run's
    first date alone often showed all zeros while a later night had seats.
    Without a seq, remaining counts are still summed across rounds.
    """
    merged: dict[str, GradeInfo] = {}
    for row in remain_rows:
        if play_seq and str(row.get("playSeq") or "") != str(play_seq):
            continue
        grade = str(row.get("seatGrade") or "")
        if not grade:
            continue
        entry = merged.get(grade)
        if entry is None:
            merged[grade] = GradeInfo(
                grade=grade,
                name=str(row.get("seatGradeName") or prices.get(grade, {}).get("name") or grade),
                price=int(prices.get(grade, {}).get("price") or 0),
                remain=int(row.get("remainCnt") or 0),
            )
        else:
            entry.remain += int(row.get("remainCnt") or 0)
    for grade, info in prices.items():
        if grade not in merged:
            merged[grade] = GradeInfo(
                grade=grade,
                name=str(info.get("name") or grade),
                price=int(info.get("price") or 0),
                remain=0,
            )
    return sorted(merged.values(), key=lambda item: (-item.price, item.grade))


def to_iso_date(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        return ""
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"


def normalize_play_time(value: str | None) -> str:
    match = re.match(r"^(\d{1,2}):(\d{2})$", str(value or "").strip())
    if not match:
        return ""
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def fetch_nol_schedules(
    goods_code: str,
    place_code: str,
    play_date: str,
    *,
    timeout: float = 12.0,
) -> list[dict[str, Any]]:
    """NOL product schedules for one day (yyyy-MM-dd window)."""
    iso = to_iso_date(play_date)
    if not goods_code or not place_code or not iso:
        return []
    key = f"{goods_code}:{place_code}"
    url = (
        "https://nol.yanolja.com/ticket/products/api/schedules"
        f"?goodsKey={urllib.parse.quote(key)}"
        f"&playStartDate={iso}&playEndDate={iso}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
            "Referer": f"https://nol.yanolja.com/ticket/products/{goods_code}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise ShowInfoError(f"schedules → {error}") from error
    content = payload.get("content") if isinstance(payload, dict) else None
    return [row for row in content or [] if isinstance(row, dict)]


def pick_schedule(
    schedules: list[dict[str, Any]],
    *,
    play_seq: str | None = None,
    play_time: str | None = None,
) -> dict[str, Any] | None:
    if not schedules:
        return None
    if play_seq:
        for row in schedules:
            if str(row.get("playSeq") or row.get("playSequence") or "") == str(play_seq):
                return row
    want = normalize_play_time(play_time)
    if want:
        for row in schedules:
            if normalize_play_time(str(row.get("playTime") or "")) == want:
                return row
    return schedules[0]


def fetch_round_remains(
    goods_code: str,
    play_date: str,
    *,
    place_code: str = "",
    play_seq: str | None = None,
    play_time: str | None = None,
    prices: dict[str, dict[str, Any]] | None = None,
    timeout: float = 12.0,
) -> tuple[list[GradeInfo], str, str]:
    """Remains for the round the 예매판 is showing.

    Returns ``(grades, resolved_play_seq, resolved_play_time)``.
    """
    code = parse_goods_code(goods_code)
    date = re.sub(r"\D", "", str(play_date or ""))[:8]
    if len(date) != 8:
        raise ShowInfoError("공연일이 없습니다")

    resolved_seq = str(play_seq or "")
    resolved_time = normalize_play_time(play_time)
    if place_code and (not resolved_seq or play_time):
        try:
            schedules = fetch_nol_schedules(code, place_code, date, timeout=timeout)
            picked = pick_schedule(schedules, play_seq=resolved_seq or None, play_time=resolved_time or None)
            if picked:
                resolved_seq = str(picked.get("playSeq") or picked.get("playSequence") or resolved_seq)
                resolved_time = normalize_play_time(str(picked.get("playTime") or "")) or resolved_time
        except ShowInfoError:
            pass

    remain_rows, play_seqs = parse_remain_rows(
        _get_json(f"/v1/goods/{code}/playSeq/PlayDate/{date}/ALL", timeout=timeout)
    )
    if not resolved_seq and play_seqs:
        resolved_seq = play_seqs[0]
    grade_prices = prices or {}
    grades = merge_grades(remain_rows, grade_prices, play_seq=resolved_seq or None)
    return grades, resolved_seq, resolved_time


def build_warnings(compatibility: ShowCompatibility) -> list[str]:
    warnings: list[str] = []
    flow = compatibility.flow_label
    if flow == "legacy-poticket":
        warnings.append(
            "이 공연은 구형 poticket 엔진입니다. 원스탑 좌석 API가 없어 자동 선점을 지원하지 않습니다."
        )
    elif flow == "onestop-general-admission":
        warnings.append(
            "비지정석/입장권 상품입니다. 좌석 선택 단계가 없으므로 매수만 지정해 결제 단계로 진행합니다."
        )
    elif flow == "onestop-sports":
        warnings.append("스포츠 상품입니다. seatType=SPORTS로 선점하며 구역 이름이 구단 존 이름입니다.")
    if compatibility.is_captcha:
        warnings.append("보안문자(캡차)가 있는 공연입니다. 실패 시 수동 입력이 필요할 수 있습니다.")
    if compatibility.hide_remain_seat:
        warnings.append("잔여석이 상품 페이지에 숨겨져 있습니다. 목록의 0은 매진이 아니며, 실제 빈자리는 좌석맵 기준입니다.")
    return warnings


def _get_json(path: str, *, timeout: float = 12.0) -> Any:
    request = urllib.request.Request(API_ORIGIN + path, headers=API_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raise ShowInfoError(f"{path} → HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ShowInfoError(f"{path} → {error}") from error
    except json.JSONDecodeError as error:
        raise ShowInfoError(f"{path} → invalid JSON") from error


def fetch_show_catalog(value: str, *, timeout: float = 12.0) -> ShowCatalog:
    """Look up a show by goods code or product URL."""
    goods_code = parse_goods_code(value)
    payload = _get_json(f"/v1/goods/{goods_code}/summary", timeout=timeout)
    summary = (payload or {}).get("data") or {}
    if not summary.get("goodsCode"):
        raise ShowInfoError(f"{goods_code} 공연 정보를 찾을 수 없습니다")

    compatibility = parse_summary_compatibility(summary)
    catalog = ShowCatalog(
        goods_code=goods_code,
        goods_name=str(summary.get("goodsName") or ""),
        place_name=str(summary.get("placeName") or ""),
        place_code=str(summary.get("placeCode") or ""),
        genre_name=str(summary.get("genreName") or ""),
        play_start_date=str(summary.get("playStartDate") or ""),
        play_end_date=str(summary.get("playEndDate") or ""),
        ticket_open_raw=str(summary.get("ticketOpenDate") or ""),
        compatibility=compatibility,
    )
    catalog.ticket_open_kst = format_open_time(catalog.ticket_open_raw)
    catalog.warnings = build_warnings(compatibility)

    prices: dict[str, int] = {}
    try:
        prices = parse_price_groups(_get_json(f"/v1/goods/{goods_code}/prices/group", timeout=timeout))
    except ShowInfoError as error:
        catalog.errors.append(str(error))

    remain_rows: list[dict[str, Any]] = []
    if catalog.play_start_date:
        try:
            remain_rows, catalog.play_seqs = parse_remain_rows(
                _get_json(
                    f"/v1/goods/{goods_code}/playSeq/PlayDate/{catalog.play_start_date}/ALL",
                    timeout=timeout,
                )
            )
        except ShowInfoError as error:
            catalog.errors.append(str(error))

    catalog.grades = merge_grades(remain_rows, prices)
    if not catalog.grades:
        catalog.errors.append("등급 정보를 가져오지 못했습니다 (오픈 전이거나 비공개 상품일 수 있습니다)")
    return catalog


def seat_table_lines(
    rows: list[dict], hide_remain: bool, live_free: int | None = None
) -> list[str]:
    """The 공연 table as text: grade, price, and what is left.

    Lives here rather than in the panel so the tests can exercise it without a
    display. It used to be duplicated into the test as a hand-kept copy, and the
    copy drifted the first time the real one changed.

    `remain` comes from the ticketfront API before a seat session exists. Once
    one does, the availability bitmap is the truthful number — the API has been
    observed reporting 0 remaining while 228 seats were free — so `live_free`
    supersedes it, and a 비공개 column beside a real count is dropped rather than
    left to contradict it.
    """
    lines: list[str] = []
    total = 0
    known = False
    for row in rows:
        price = int(row.get("price") or 0)
        remain = int(row.get("remain") or 0)
        if hide_remain:
            left = "" if live_free is not None else "비공개"
        elif remain:
            left = f"{remain}석"
            total += remain
            known = True
        else:
            left = "0석"
            known = True
        name = str(row.get("name") or "?")[:14]
        lines.append(f"{name:<14} {price:>9,}원  {left:>7}")

    if not lines:
        return ["공연을 불러오면 등급과 잔여가 표시됩니다."]
    if live_free is not None:
        lines.append("─" * 34)
        lines.append(f"{'지금 빈 좌석':<14} {'':>9}   {live_free}석")
    elif known:
        lines.append("─" * 34)
        lines.append(f"{'합계':<14} {'':>9}   {total}석")
    return lines
