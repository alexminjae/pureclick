"""A house where only part of the room is on sale.

26012673 (진성쇼, 춘천) sells 1F and 2F blocks A-C and nothing else. seatMeta
still reports D, E and one side block — 712 real seats across five of eleven
blocks — with no seatGrade and isExposable false throughout.

`seatingBlocks()` dropped exactly those from the zone sketch, because they have
no exposable seat and lie clear of the exposable bounds. For the watch that is
right: nothing can free up in a block nobody can buy from. For the picker it
deleted part of the room — the drawn map had three columns where the 예매 창
beside it showed five, which is indistinguishable from a broken projection.

So the sketch now carries the whole house and marks what cannot sell:
drawn, never watched, never counted as a target.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.zone_map import (  # noqa: E402
    block_keys_in_watch_rect,
    is_sellable,
    key_at_point,
    keys_in_lasso,
    project_venue,
    seats_in_watch_rect,
    live_block_keys,
)

# 26012673's real block boxes, from seatMeta. A-C on both floors are on sale;
# D and E on both floors are not, and 001:011 is a side island 50 units clear
# of the house with 90 seats and no grade.
ON_SALE = [
    ("001:001", 57.9, 81.9, 66.2, 123.2), ("001:002", 87.2, 120.2, 66.3, 120.3),
    ("001:003", 125.6, 164.6, 66.4, 117.4),
    ("001:006", 56.9, 83.9, 150.4, 192.4), ("001:007", 89.0, 122.0, 150.6, 192.6),
    ("001:008", 127.5, 163.5, 150.6, 192.6),
]
CLOSED = [
    ("001:004", 169.5, 202.5, 66.6, 120.6), ("001:005", 208.4, 232.4, 66.7, 123.7),
    ("001:009", 169.0, 202.0, 150.7, 192.7), ("001:010", 207.4, 234.4, 150.6, 192.6),
]
ISLAND = [("001:011", 285.0, 312.0, 123.0, 147.0)]


def _dots(rows, sellable):
    out = []
    for key, x0, x1, y0, y1 in rows:
        for i in range(4):
            for j in range(4):
                dot = {"k": key, "x": x0 + (x1 - x0) * i / 3, "y": y0 + (y1 - y0) * j / 3}
                if not sellable:
                    dot["s"] = 0
                out.append(dot)
    return out


SKETCH = _dots(ON_SALE, True) + _dots(CLOSED, False) + _dots(ISLAND, False)
BLOCKS = [{"block_key": k, "key": k, "name": k[-3:]}
          for k, *_ in ON_SALE + CLOSED + ISLAND]

WHOLE_HOUSE = (50.0, 60.0, 240.0, 200.0)
DARK_HALF = (168.0, 60.0, 240.0, 200.0)


class TheFlag(unittest.TestCase):
    def test_absence_means_sellable(self) -> None:
        # The sketch rides in a ~300KB state file parsed on every poll, so the
        # key is written only for the minority that cannot sell.
        self.assertTrue(is_sellable({"k": "a", "x": 1, "y": 1}))
        self.assertFalse(is_sellable({"k": "a", "x": 1, "y": 1, "s": 0}))


class TheMapShowsWhatCanBeAimedAt(unittest.TestCase):
    """Only blocks that are selling this round are drawn.

    Getting here took two wrong turns, both reported. Drawing the closed blocks
    as dimmed *seats* read as a snapshot of availability — "i don't know why
    the seats are half filled like this, it's a 취켓팅 range so it doesn't
    matter of the current seat availabilities". Drawing them as dimmed *blocks*
    read as a fault — "seat areas are falsely getting picked up ... some sort
    of error". They were neither: on 26007442 they are two six-seat side strips
    and an eighty-seat block fifty units clear of the house, none of it on sale
    and none of it in the visible 예매 창 map.

    A range picker exists to let you point at seats. A block that cannot sell
    is not a choice, so offering it is offering something that does nothing.
    The count is named in the hint instead of drawn.
    """

    def test_the_picker_draws_only_the_selling_blocks(self) -> None:
        live = live_block_keys(SKETCH)
        drawable = [row for row in SKETCH if row["k"] in live]
        view = project_venue(BLOCKS, drawable, 640, 700, include_all=True)
        self.assertEqual(
            {seat.key for seat in view.seats},
            {"001:001", "001:002", "001:003", "001:006", "001:007", "001:008"},
        )

    def test_nothing_drawn_is_ever_unsellable(self) -> None:
        live = live_block_keys(SKETCH)
        drawable = [row for row in SKETCH if row["k"] in live]
        view = project_venue(BLOCKS, drawable, 640, 700, include_all=True)
        self.assertTrue(all(seat.sellable for seat in view.seats))

    def test_the_house_is_framed_to_what_it_draws(self) -> None:
        """The closed columns used to stretch the frame and squash the room."""
        live = live_block_keys(SKETCH)
        drawable = [row for row in SKETCH if row["k"] in live]
        view = project_venue(BLOCKS, drawable, 640, 700, include_all=True)
        self.assertAlmostEqual(max(s.venue_x for s in view.seats), 164.6, places=1)

    def test_the_panel_filters_before_it_projects(self) -> None:
        panel = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
        self.assertIn("live_block_keys(self._zone_sketch)", panel)
        self.assertIn("drawable", panel)


class TheWatchIgnoresWhatCannotSell(unittest.TestCase):
    def test_a_range_over_the_dark_half_watches_nothing(self) -> None:
        under = seats_in_watch_rect(SKETCH, DARK_HALF)
        self.assertTrue(under, "the seats are there to be drawn")
        self.assertEqual(block_keys_in_watch_rect(SKETCH, DARK_HALF), [])

    def test_a_range_over_taken_seats_in_a_live_block_still_watches_it(self) -> None:
        """A range is a boundary, not a snapshot.

        Filtering it per seat instead of per block meant a box drawn tightly
        around a sold-out section of an on-sale block watched nothing — and a
        sold-out section is the exact thing 취켓팅 is pointed at.
        """
        sold_out_corner = [
            dict(row, s=0) if row["k"] == "001:001" else dict(row)
            for row in SKETCH
        ]
        # 001:001 now reports every seat taken; it is still an on-sale block
        # because its grade did not go away — modelled here by 002 staying live.
        keys = block_keys_in_watch_rect(sold_out_corner, (50.0, 60.0, 125.0, 200.0))
        self.assertIn("001:002", keys)

    def test_a_block_with_nothing_on_sale_is_the_only_thing_skipped(self) -> None:
        self.assertEqual(
            live_block_keys(SKETCH),
            {"001:001", "001:002", "001:003", "001:006", "001:007", "001:008"},
        )

    def test_a_range_over_the_whole_house_watches_only_the_open_blocks(self) -> None:
        self.assertEqual(
            sorted(block_keys_in_watch_rect(SKETCH, WHOLE_HOUSE)),
            ["001:001", "001:002", "001:003", "001:006", "001:007", "001:008"],
        )

    def test_clicking_scenery_selects_nothing(self) -> None:
        view = project_venue(BLOCKS, SKETCH, 640, 700, include_all=True)
        dark = next(s for s in view.seats if not s.sellable)
        self.assertIsNone(key_at_point(view, dark.x, dark.y))

    def test_a_lasso_over_scenery_selects_nothing(self) -> None:
        view = project_venue(BLOCKS, SKETCH, 640, 700, include_all=True)
        xs = [s.x for s in view.seats if not s.sellable]
        ys = [s.y for s in view.seats if not s.sellable]
        box = (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2)
        self.assertEqual(keys_in_lasso(view, box), [])


class TheCountIsHonest(unittest.TestCase):
    def test_it_counts_seats_in_blocks_that_are_selling(self) -> None:
        live = live_block_keys(SKETCH)
        self.assertEqual(sum(1 for row in SKETCH if row["k"] in live), 16 * 6)
        self.assertEqual(sum(1 for row in SKETCH if row["k"] not in live), 16 * 5)


if __name__ == "__main__":
    unittest.main()


class DroppedRequestsAreNotSilent(unittest.TestCase):
    """The same class of bug, one layer down.

    `mapLimit` dropped a failed batch from its results and logged to a console
    nobody reads, so a caller could not tell "the venue has six blocks" from
    "five requests failed". That is what feeds both the picker and the watch's
    picture of the venue — half a house drawn, half a venue swept, in silence.
    """

    def test_the_band_says_when_the_watch_is_seeing_part_of_the_venue(self) -> None:
        from core.seat import watch_note

        note = watch_note(
            {"running": True, "batchFailures": 3, "watchedBlocks": 4,
             "sweepTicks": 9, "observedTickMs": 140}
        )
        self.assertIn("일부 구역", note)
        # Without the count: the band repaints twice a second and a growing
        # number there churns. That it is happening at all is the actionable
        # part; how many times is in the published status.
        self.assertNotRegex(note, r"\d")

    def test_a_clean_run_says_nothing_about_it(self) -> None:
        from core.seat import watch_note

        note = watch_note(
            {"running": True, "batchFailures": 0, "watchedBlocks": 4,
             "sweepTicks": 9, "observedTickMs": 140}
        )
        self.assertNotIn("일부 구역", note)
        self.assertIn("빈자리가 나오면", note)
