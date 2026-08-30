"""The panel's 공연 table.

The table is the top of the screen and the basis for every choice below it, but
it is also the easiest thing to get quietly wrong: a show that hides its
remaining counts reports `0` for every grade, and rendering that as "0석" tells
you the show is sold out when it is not.

These once drove a hand-kept copy of the panel's rendering logic. The copy went
stale the first time the real method changed and the tests still passed, so the
logic now lives in `core.showinfo.seat_table_lines` and is imported here —
there is nothing left to drift.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.showinfo import seat_table_lines as render

MAC_DIR = Path(__file__).resolve().parent.parent / "mac"


GRADES = [
    {"name": "VIP석", "price": 180000, "remain": 12},
    {"name": "R석", "price": 150000, "remain": 50},
    {"name": "S석", "price": 120000, "remain": 58},
]


class ShowTableTests(unittest.TestCase):
    def test_grades_prices_and_a_total(self) -> None:
        lines = render(GRADES, hide_remain=False)
        self.assertIn("VIP석", lines[0])
        self.assertIn("180,000원", lines[0])
        self.assertIn("12석", lines[0])
        self.assertIn("120석", lines[-1], "12 + 50 + 58 should be totalled")

    def test_hidden_remaining_is_not_shown_as_zero(self) -> None:
        """Some shows hide remaining counts and report 0 for every grade.

        Rendering that as "0석" would read as sold out when seats are free — the
        availability bitmap has been measured showing 228 free seats on a show
        whose API said 0.
        """
        hidden = [dict(row, remain=0) for row in GRADES]
        lines = render(hidden, hide_remain=True)
        for line in lines:
            self.assertNotIn("0석", line)
        self.assertTrue(all("비공개" in line for line in lines))
        # No total, because nothing is known to total.
        self.assertNotIn("합계", "\n".join(lines))

    def test_hidden_remaining_yields_to_a_live_count(self) -> None:
        """비공개 must not sit beside a real number.

        A show reporting 비공개 while the bitmap knows 398 seats are free printed
        both, one line above the other, which reads as a broken panel. The live
        figure is the truthful one and is the only one that should appear.
        """
        hidden = [{"name": "지정석", "price": 132000, "remain": 0}]
        joined = "\n".join(render(hidden, hide_remain=True, live_free=398))
        self.assertNotIn("비공개", joined, "the live count supersedes 비공개")
        self.assertIn("398석", joined)
        self.assertIn("132,000원", joined, "price is still worth showing")

    def test_live_count_replaces_the_api_total(self) -> None:
        """Once a seat session exists the bitmap is the truthful number."""
        lines = render(GRADES, hide_remain=False, live_free=126)
        joined = "\n".join(lines)
        self.assertIn("지금 빈 좌석", joined)
        self.assertIn("126석", joined)
        self.assertNotIn("합계", joined)

    def test_no_show_loaded(self) -> None:
        self.assertEqual(len(render([], hide_remain=False)), 1)

    def test_panel_delegates_instead_of_duplicating(self) -> None:
        """The panel must call the shared builder, not grow its own copy."""
        source = (MAC_DIR / "pureclick.py").read_text(encoding="utf-8")
        start = source.index("def _render_seat_table")
        body = source[start : source.index("\n    def ", start + 1)]
        self.assertIn("seat_table_lines(", body)
        self.assertNotIn("비공개", body, "rendering rules belong in one place")



class HiddenCountsTest(unittest.TestCase):
    """A show that will not say how many seats are left.

    Measured on 26012217 (뮤지컬 드라큘라 - 대구): its own
    /onestop/api/seats/grades answered `remainCount: 0` for every grade with
    `"isVisibleSeatCount": false` — a truthful "we do not publish this". The
    panel rendered those zeros as 0석 while the same recording showed
    freeSeats 710, clickableNow 352, and the macro booking a VIP seat.

    The bitmap is the answer: seatMeta carries each seat's grade, seatStatus
    carries the free bits, and both are already fetched.
    """

    ROWS = [
        {"name": "VIP석", "price": 180000, "remain": 0},
        {"name": "R석", "price": 150000, "remain": 0},
        {"name": "S석", "price": 120000, "remain": 0},
        {"name": "A석", "price": 90000, "remain": 0},
    ]

    def test_the_bitmap_beats_a_published_zero(self) -> None:
        # These sum to 710 on purpose — the freeSeats the live capture reported
        # for this exact show while all four grades published 0.
        lines = render(self.ROWS, hide_remain=False,
                       free_by_grade={"VIP석": 122, "R석": 310, "S석": 208, "A석": 70})
        self.assertIn("122석", lines[0])
        self.assertIn("310석", lines[1])
        # Not a substring check: "70석" contains "0석". No row may end in a
        # bare zero where a real count belongs.
        for line in lines[:4]:
            self.assertFalse(line.rstrip().endswith(" 0석"), line)
        self.assertIn("710석", lines[-1], "and the total is the number we measured")

    def test_it_totals_what_it_counted(self) -> None:
        lines = render(self.ROWS, hide_remain=False, free_by_grade={"VIP석": 122, "R석": 310})
        self.assertIn("432", "\n".join(lines))

    def test_a_grade_the_bitmap_has_not_seen_falls_back(self) -> None:
        # Only the watched blocks are polled, so a grade can legitimately have
        # no count yet. That must read as the old value, not as zero.
        text = "\n".join(render(
            [{"name": "VIP석", "price": 180000, "remain": 0},
             {"name": "R석", "price": 150000, "remain": 42}],
            hide_remain=False, free_by_grade={"VIP석": 122}))
        self.assertIn("122석", text)
        self.assertIn("42석", text, "an unseen grade keeps the API's number")

    def test_hidden_counts_still_say_so_when_nothing_is_measured(self) -> None:
        self.assertIn("비공개", "\n".join(render(self.ROWS, hide_remain=True)))

    def test_no_bitmap_yet_behaves_exactly_as_before(self) -> None:
        before = render(self.ROWS, hide_remain=False)
        after = render(self.ROWS, hide_remain=False, free_by_grade=None)
        self.assertEqual(before, after, "the new argument must not change the old path")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
