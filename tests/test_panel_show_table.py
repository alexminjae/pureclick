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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
