"""Browse NOL's show catalogue and find shows whose ticket sale has not opened.

NOL renders its genre pages server-side, so the product list can be read
straight out of the HTML without a login. Combining that with the public
ticketfront summary lookup gives a "what opens next" view, which is the thing a
sniper actually needs to arm against.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from pureclick_core import KST
from pureclick_showinfo import BROWSER_UA, ShowCatalog, fetch_show_catalog

NOL_ORIGIN = "https://nol.yanolja.com"

# Slugs verified live; NOL 404s on anything else (leisure/dance/kids do not exist).
GENRES: dict[str, str] = {
    "concert": "콘서트",
    "musical": "뮤지컬",
    "play": "연극",
    "classic": "클래식/오페라",
    "exhibition": "전시/행사",
    "sports": "스포츠",
    "family": "아동/가족",
}

PRODUCT_LINK_RE = re.compile(r"/ticket/products/([0-9A-Z]{6,10})")


class CatalogError(Exception):
    """Raised when the NOL catalogue cannot be read."""


@dataclass(frozen=True)
class UpcomingShow:
    goods_code: str
    goods_name: str
    place_name: str
    genre: str
    flow: str
    opens_at: datetime | None
    opens_at_text: str
    needs_seat_picking: bool
    is_captcha: bool

    @property
    def seconds_until_open(self) -> float | None:
        if self.opens_at is None:
            return None
        return (self.opens_at - datetime.now(KST)).total_seconds()

    def to_mapping(self) -> dict:
        return {
            "goods_code": self.goods_code,
            "goods_name": self.goods_name,
            "place_name": self.place_name,
            "genre": self.genre,
            "flow": self.flow,
            "opens_at_text": self.opens_at_text,
            "seconds_until_open": self.seconds_until_open,
            "needs_seat_picking": self.needs_seat_picking,
            "is_captcha": self.is_captcha,
        }


def _fetch_html(url: str, timeout: float = 20.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        raise CatalogError(f"{url} → {error}") from error


def list_genre_products(slug: str, *, timeout: float = 20.0) -> list[str]:
    """Goods codes linked from a NOL genre page, in page order."""
    if slug not in GENRES:
        raise CatalogError(f"알 수 없는 장르: {slug}")
    html = _fetch_html(f"{NOL_ORIGIN}/ticket/genre/{slug}", timeout=timeout)
    seen: list[str] = []
    for code in PRODUCT_LINK_RE.findall(html):
        if code not in seen:
            seen.append(code)
    return seen


def list_all_products(
    slugs: Iterable[str] | None = None,
    *,
    max_workers: int = 7,
) -> dict[str, list[str]]:
    """Goods codes per genre slug. Genres that fail are omitted rather than fatal."""
    targets = list(slugs) if slugs is not None else list(GENRES)

    def grab(slug: str) -> tuple[str, list[str]]:
        try:
            return slug, list_genre_products(slug)
        except CatalogError:
            return slug, []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return {slug: codes for slug, codes in pool.map(grab, targets) if codes}


def parse_open_datetime(raw: str) -> datetime | None:
    """`yyyyMMddHHmm` ticketOpenDate to an aware KST datetime."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) < 12:
        return None
    try:
        return datetime(
            int(digits[0:4]), int(digits[4:6]), int(digits[6:8]),
            int(digits[8:10]), int(digits[10:12]), tzinfo=KST,
        )
    except ValueError:
        return None


def to_upcoming(catalog: ShowCatalog, genre: str) -> UpcomingShow:
    opens_at = parse_open_datetime(catalog.ticket_open_raw)
    return UpcomingShow(
        goods_code=catalog.goods_code,
        goods_name=catalog.goods_name,
        place_name=catalog.place_name,
        genre=genre,
        flow=catalog.flow,
        opens_at=opens_at,
        opens_at_text=catalog.ticket_open_kst,
        needs_seat_picking=bool(catalog.compatibility and catalog.compatibility.needs_seat_picking),
        is_captcha=bool(catalog.compatibility and catalog.compatibility.is_captcha),
    )


def scan_shows(
    *,
    slugs: Iterable[str] | None = None,
    upcoming_only: bool = True,
    max_workers: int = 8,
    progress: Callable[[int, int], None] | None = None,
) -> list[UpcomingShow]:
    """Read every genre page, resolve each show, and sort by soonest ticket open.

    With `upcoming_only`, shows whose sale already started are dropped — those
    are 취켓팅 targets rather than open-day targets.
    """
    by_genre = list_all_products(slugs)
    jobs = [(code, GENRES.get(slug, slug)) for slug, codes in by_genre.items() for code in codes]
    total = len(jobs)
    done = 0

    def resolve(job: tuple[str, str]) -> UpcomingShow | None:
        code, genre = job
        try:
            return to_upcoming(fetch_show_catalog(code), genre)
        except Exception:  # noqa: BLE001 - a single bad show must not stop the scan
            return None

    results: list[UpcomingShow] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for show in pool.map(resolve, jobs):
            done += 1
            if progress is not None:
                progress(done, total)
            if show is not None:
                results.append(show)

    if upcoming_only:
        results = [show for show in results if (show.seconds_until_open or -1) > 0]

    def sort_key(show: UpcomingShow) -> tuple[int, float, str]:
        seconds = show.seconds_until_open
        return (0, seconds, show.goods_name) if seconds is not None else (1, 0.0, show.goods_name)

    return sorted(results, key=sort_key)


def format_countdown(seconds: float | None) -> str:
    if seconds is None:
        return "미정"
    if seconds <= 0:
        return "판매중"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"
