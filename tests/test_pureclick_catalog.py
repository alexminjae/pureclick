from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from pureclick_core import KST
from pureclick_catalog import (
    GENRES,
    PRODUCT_LINK_RE,
    UpcomingShow,
    format_countdown,
    parse_open_datetime,
)


class CatalogTests(unittest.TestCase):
    def test_genre_slugs_are_the_verified_set(self) -> None:
        """NOL 404s on any other slug; keep the list honest."""
        self.assertEqual(
            set(GENRES),
            {"concert", "musical", "play", "classic", "exhibition", "sports", "family"},
        )

    def test_product_link_extraction(self) -> None:
        html = (
            '<a href="/ticket/products/26012391">A</a>'
            '<a href="/ticket/products/26012391">dup</a>'
            '<a href="/ticket/products/L0000142">B</a>'
            '<a href="/ticket/genre/concert">not a product</a>'
        )
        codes = []
        for code in PRODUCT_LINK_RE.findall(html):
            if code not in codes:
                codes.append(code)
        self.assertEqual(codes, ["26012391", "L0000142"])

    def test_parse_open_datetime(self) -> None:
        parsed = parse_open_datetime("202608141200")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.year, parsed.month, parsed.day, parsed.hour), (2026, 8, 14, 12))
        self.assertEqual(parsed.tzinfo, KST)
        self.assertIsNone(parse_open_datetime(""))
        self.assertIsNone(parse_open_datetime("20260899XXXX"))

    def test_format_countdown(self) -> None:
        self.assertEqual(format_countdown(None), "미정")
        self.assertEqual(format_countdown(0), "판매중")
        self.assertEqual(format_countdown(-5), "판매중")
        self.assertEqual(format_countdown(45), "45초")
        self.assertEqual(format_countdown(3 * 60 + 5), "3분 5초")
        self.assertEqual(format_countdown(2 * 3600 + 30 * 60), "2시간 30분")
        self.assertEqual(format_countdown(3 * 86400 + 4 * 3600), "3일 4시간")

    def test_seconds_until_open(self) -> None:
        soon = datetime.now(KST) + timedelta(hours=2)
        show = UpcomingShow(
            goods_code="26012391",
            goods_name="테스트",
            place_name="장소",
            genre="콘서트",
            flow="onestop-reserved",
            opens_at=soon,
            opens_at_text="",
            needs_seat_picking=True,
            is_captcha=False,
        )
        seconds = show.seconds_until_open
        self.assertIsNotNone(seconds)
        assert seconds is not None
        self.assertTrue(7000 < seconds <= 7200)
        self.assertEqual(show.to_mapping()["goods_code"], "26012391")

    def test_show_without_open_time(self) -> None:
        show = UpcomingShow(
            goods_code="X",
            goods_name="n",
            place_name="p",
            genre="g",
            flow="legacy-poticket",
            opens_at=None,
            opens_at_text="",
            needs_seat_picking=False,
            is_captcha=False,
        )
        self.assertIsNone(show.seconds_until_open)
        self.assertEqual(format_countdown(show.seconds_until_open), "미정")


if __name__ == "__main__":
    unittest.main()
