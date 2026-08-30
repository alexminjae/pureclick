from __future__ import annotations

import unittest

from core.zone_map import (
    block_keys_in_watch_rect,
    is_click,
    parse_watch_rect,
    primary_frame_seats,
    project_venue,
    seat_pitch,
    seats_in_watch_rect,
)


BLOCKS = [
    {"key": "001:101", "name": "본좌석", "left": 0, "top": 0, "right": 100, "bottom": 80},
    {"key": "001:102", "name": "옆섬", "left": 200, "top": 40, "right": 240, "bottom": 70},
]

# Main house + far side island (like 002 on Othello).
SEATS = [
    *[{"k": "001:101", "x": x, "y": y} for x in range(10, 90, 4) for y in range(20, 70, 4)],
    *[{"k": "001:102", "x": x, "y": y} for x in range(210, 235, 3) for y in range(45, 65, 4)],
]


class ZoneMapTests(unittest.TestCase):
    def test_primary_frame_drops_far_side_island(self) -> None:
        points = [(s["k"], s["x"], s["y"]) for s in SEATS]
        framed = primary_frame_seats(points)
        self.assertTrue(all(key == "001:101" for key, _, _ in framed))
        self.assertGreater(len(framed), 40)

    def test_projected_copy_hides_side_island(self) -> None:
        view = project_venue(BLOCKS, SEATS, 400, 300)
        keys = {seat.key for seat in view.seats}
        self.assertEqual(keys, {"001:101"})
        self.assertIsNotNone(view.stage)

    def test_selection_surface_draws_every_seat(self) -> None:
        """A seat that is not drawn cannot be dragged over.

        Framing follows the main house by default so a far island cannot shrink
        it, but the 취켓팅 picker needs the whole venue: measured against the
        saved maps, framing-only drawing reached 3 blocks of 17 on 26011315 and
        1 of 3 on 26005128, silently putting most of the venue out of reach.
        """
        framed = project_venue(BLOCKS, SEATS, 400, 300)
        every = project_venue(BLOCKS, SEATS, 400, 300, include_all=True)

        self.assertEqual({seat.key for seat in framed.seats}, {"001:101"})
        self.assertEqual({seat.key for seat in every.seats}, {"001:101", "001:102"})
        self.assertEqual(len(every.seats), len(SEATS))

        # Still selectable: a box over the island must catch its seats.
        island = [s for s in SEATS if s["k"] == "001:102"]
        placed = [s for s in every.seats if s.key == "001:102"]
        box = (
            min(s.x for s in placed) - 2,
            min(s.y for s in placed) - 2,
            max(s.x for s in placed) + 2,
            max(s.y for s in placed) + 2,
        )
        inside = seats_in_watch_rect(SEATS, every.canvas_rect_to_venue(box))
        self.assertEqual(len(inside), len(island))

    def test_drag_rect_selects_seats_not_whole_blocks(self) -> None:
        view = project_venue(BLOCKS, SEATS, 400, 300)
        # Lasso the left half of the house in canvas space.
        seats = list(view.seats)
        left = min(s.x for s in seats)
        top = min(s.y for s in seats)
        right = (left + max(s.x for s in seats)) / 2
        bottom = max(s.y for s in seats)
        venue = view.canvas_rect_to_venue((left, top, right, bottom))
        inside = seats_in_watch_rect(SEATS, venue)
        self.assertGreater(len(inside), 5)
        self.assertLess(len(inside), len([s for s in SEATS if s["k"] == "001:101"]))
        self.assertEqual(block_keys_in_watch_rect(SEATS, venue), ["001:101"])

    def test_stage_sits_over_the_seats_not_the_bounding_box(self) -> None:
        """The drawn stage has to track the seating, not the extents.

        seatMeta carries no stage, so it is inferred. It used to be a fixed box
        35% of the venue width pinned to the middle of the bounding box. Give it
        a house on the left and a far side block on the right and that midpoint
        lands in the empty gap between them — the stage floats over nothing,
        which is exactly what it did on a real show.
        """
        house = [{"k": "main", "x": x, "y": y} for x in range(0, 101, 5) for y in range(0, 51, 5)]
        island = [{"k": "side", "x": x, "y": y} for x in range(300, 321, 5) for y in range(40, 51, 5)]
        view = project_venue([], house + island, 400, 300, include_all=True)

        self.assertIsNotNone(view.stage)
        left, _top, right, _bottom = view.stage
        placed = {seat.key: [s for s in view.seats if s.key == seat.key] for seat in view.seats}
        main = [s for s in view.seats if s.key == "main"]

        # The stage must cover the front of the house...
        self.assertLessEqual(left, min(s.x for s in main) + 1)
        self.assertGreaterEqual(right, max(s.x for s in main) - 1)
        # ...and must not stretch across the empty gap to the far island.
        side = [s for s in view.seats if s.key == "side"]
        self.assertLess(right, min(s.x for s in side), "stage must stop before the side block")

        # And it must actually be above seats rather than hanging over the gap.
        under = [s for s in view.seats if left <= s.x <= right]
        self.assertGreater(len(under), len(main) * 0.8)

    def test_stage_spans_a_symmetric_house(self) -> None:
        seats = [{"k": "a", "x": x, "y": y} for x in range(0, 101, 5) for y in range(0, 51, 5)]
        view = project_venue([], seats, 400, 300, include_all=True)
        left, _t, right, _b = view.stage
        xs = [s.x for s in view.seats]
        self.assertLessEqual(abs(left - min(xs)), 1.5)
        self.assertLessEqual(abs(right - max(xs)), 1.5)

    def test_seat_pitch_is_measured_inside_a_block(self) -> None:
        """Pooling every block's coordinates destroys the measurement.

        Blocks sit at fractional offsets from each other. Pool them and the
        combined list of distinct positions fills with near-zero gaps that say
        nothing about seat spacing — and with enough blocks those gaps outnumber
        the real ones and take the median. On a real 1913-seat venue whose every
        block had a 13.95px pitch, the pooled median came out at 0.24px and
        every seat was drawn at the 1px floor: specks, where the real map has
        touching circles.

        Twenty blocks on a shared 10px grid, each nudged 0.3px off the last, is
        that venue in miniature.
        """
        seats = []
        for index in range(20):
            offset = index * 0.3
            seats.extend(
                {"k": f"b{index}", "x": offset + col * 10.0, "y": offset + row * 10.0}
                for col in range(6)
                for row in range(6)
            )
        view = project_venue([], seats, 600, 600, include_all=True)
        pitch = seat_pitch(view.seats)
        spacing = view.scale * 10.0

        self.assertAlmostEqual(
            pitch, spacing, delta=spacing * 0.2,
            msg=f"pitch {pitch:.2f} should track the 10px grid ({spacing:.2f} on canvas)",
        )
        # The pooled median lands on the 0.3px nudges, an order of magnitude out.
        self.assertGreater(
            pitch, spacing * 0.5,
            "a venue-wide median collapses onto the gaps between blocks",
        )

    def test_seat_pitch_survives_a_single_seat_block(self) -> None:
        seats = [{"k": "main", "x": x * 10.0, "y": 0.0} for x in range(5)]
        seats.append({"k": "lonely", "x": 200.0, "y": 0.0})
        view = project_venue([], seats, 600, 400, include_all=True)
        self.assertGreater(seat_pitch(view.seats), 0.0)

    def test_watch_rect_parse(self) -> None:
        self.assertEqual(
            parse_watch_rect({"left": 10, "top": 20, "right": 5, "bottom": 40}),
            (5, 20, 10, 40),
        )
        self.assertIsNone(parse_watch_rect({"left": 1, "top": 1, "right": 1.1, "bottom": 1.1}))

    def test_tiny_motion_counts_as_a_click(self) -> None:
        self.assertTrue(is_click(10, 10, 12, 11))
        self.assertFalse(is_click(10, 10, 40, 10))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
