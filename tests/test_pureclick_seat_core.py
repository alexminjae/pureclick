from __future__ import annotations

import unittest

from pureclick_seat_core import (
    SeatAutopilotError,
    SeatCandidate,
    SeatPreferences,
    build_select_payload,
    decode_status_mask,
    diff_status_masks,
    filter_api_seats,
    group_seat_candidates,
    parse_goods_code,
    parse_seat_status,
    parse_summary_compatibility,
    rank_seats,
    resolve_seat_type,
    map_move_lines,
    seat_order_lines,
    select_seat_unit,
)


class PureClickSeatCoreTests(unittest.TestCase):
    def test_parse_goods_code_from_url(self) -> None:
        self.assertEqual(
            parse_goods_code("https://tickets.interpark.com/goods/26006325"),
            "26006325",
        )
        self.assertEqual(
            parse_goods_code("https://nol.yanolja.com/ticket/products/26011315"),
            "26011315",
        )

    def test_pick_adjacent_seats(self) -> None:
        from pureclick_seat_core import pick_adjacent_seats

        seats = [
            SeatCandidate("a", "5", "지정석 P", "A열", "1"),
            SeatCandidate("b", "5", "지정석 P", "A열", "2"),
            SeatCandidate("c", "5", "지정석 P", "A열", "4"),
            SeatCandidate("d", "5", "지정석 P", "B열", "1"),
        ]
        picked = pick_adjacent_seats(seats, 2)
        self.assertEqual([seat.seat_no for seat in picked], ["1", "2"])

    def test_adjacent_flag_in_preferences(self) -> None:
        prefs = SeatPreferences.from_mapping({"adjacent": False, "quantity": 2})
        self.assertFalse(prefs.adjacent)
        self.assertEqual(prefs.quantity, 2)

    def test_parse_summary_compatibility(self) -> None:
        summary = {
            "goodsCode": "26006325",
            "goodsName": "유미",
            "isIngredientOnestop": True,
            "isReservedSeat": True,
            "genreCode": "01011",
        }
        compatibility = parse_summary_compatibility(summary)
        self.assertTrue(compatibility.supports_seat_autopilot)
        self.assertEqual(compatibility.flow_label, "onestop-reserved")

    def test_rank_seats_prefers_grade_order(self) -> None:
        preferences = SeatPreferences(grade_order=("3", "2"))
        seats = [
            SeatCandidate("a", "2", "R석", "1열", "1"),
            SeatCandidate("b", "3", "S석", "1열", "2"),
            SeatCandidate("c", "4", "A석", "1열", "3"),
        ]
        ranked = rank_seats(seats, preferences)
        self.assertEqual([seat.seat_grade for seat in ranked], ["3", "2", "4"])

    def test_filter_api_seats_skips_hidden_but_keeps_groups(self) -> None:
        preferences = SeatPreferences()
        blocks = [
            {
                "blockKey": "001:002",
                "seats": [
                    {
                        "seatInfoId": "26006325:26000200:001:10",
                        "seatGrade": "2",
                        "seatGradeName": "R석",
                        "rowNo": "A블록 1열",
                        "seatNo": "1",
                        "isExposable": True,
                        "seatGroupId": None,
                    },
                    {
                        "seatInfoId": "26006325:26000200:001:11",
                        "seatGrade": "2",
                        "seatGradeName": "R석",
                        "rowNo": "A블록 1열",
                        "seatNo": "2",
                        "isExposable": False,
                        "seatGroupId": None,
                    },
                    {
                        "seatInfoId": "26006325:26000200:001:12",
                        "seatGrade": "2",
                        "seatGradeName": "R석",
                        "rowNo": "A블록 1열",
                        "seatNo": "3",
                        "isExposable": True,
                        "seatGroupId": "pair",
                    },
                ],
            }
        ]
        seats = filter_api_seats(blocks, preferences)
        self.assertEqual([seat.seat_no for seat in seats], ["1", "3"])
        self.assertEqual(seats[1].seat_group_id, "pair")

        opted_out = filter_api_seats(blocks, SeatPreferences(allow_group_seats=False))
        self.assertEqual([seat.seat_no for seat in opted_out], ["1"])

    def test_build_select_payload(self) -> None:
        init_data = {
            "sessionId": "26006325_M0000001377301782438152",
            "goods": {
                "goodsCode": "26006325",
                "placeCode": "26000200",
                "kindOfGoods": "01011",
                "isSportsGroup": False,
            },
            "playSeq": {"playSeq": "001"},
        }
        seat = SeatCandidate("26006325:26000200:001:93", "2", "R석", "A블록 12열", "9")
        payload = build_select_payload(init_data=init_data, seats=[seat])
        self.assertEqual(payload["sessionId"], init_data["sessionId"])
        self.assertEqual(payload["seats"][0]["seatInfoId"], seat.seat_info_id)

    def test_block_keys_filter_zones(self) -> None:
        preferences = SeatPreferences(grade_order=("2",), block_keys=("001:002",))
        seats = [
            SeatCandidate("a", "2", "R", "1", "1", block_key="001:002"),
            SeatCandidate("b", "2", "R", "1", "2", block_key="003:004"),
        ]
        ranked = rank_seats(seats, preferences)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].block_key, "001:002")

    def test_seat_strategy_round_trips_and_rejects_junk(self) -> None:
        """Which side of the house to prefer once the stage has been aimed at."""
        self.assertEqual(SeatPreferences.from_mapping({}).seat_strategy, "center")
        for strategy in ("center", "left", "right"):
            prefs = SeatPreferences.from_mapping({"seat_strategy": strategy})
            self.assertEqual(prefs.seat_strategy, strategy)
            self.assertEqual(prefs.to_mapping()["seat_strategy"], strategy)
        with self.assertRaises(SeatAutopilotError):
            SeatPreferences.from_mapping({"seat_strategy": "diagonal"})

    def test_a_config_from_an_older_build_still_opens(self) -> None:
        """The retired names must migrate, not raise.

        Every mode now aims at the stage first, so "stage" and "center" mean
        the same thing, and "random" — a way of dodging other macros by picking
        anywhere — has nothing left to mean. A saved config naming either of
        them must still load rather than refusing to start.
        """
        for old_name in ("stage", "random"):
            prefs = SeatPreferences.from_mapping({"seat_strategy": old_name})
            self.assertEqual(prefs.seat_strategy, "center", f"{old_name} should migrate")

    def test_catch_grade_strict_defaults_on(self) -> None:
        """취켓팅 takes one seat and stops, so a wrong grade ends the watch."""
        self.assertTrue(SeatPreferences.from_mapping({}).catch_grade_strict)
        self.assertFalse(
            SeatPreferences.from_mapping({"catch_grade_strict": False}).catch_grade_strict
        )

    def test_grade_order_matches_by_name_across_shows(self) -> None:
        """Grade codes are per-show ordinals, so names must drive the ordering."""
        preferences = SeatPreferences(grade_order=("S석", "VIP석"))
        musical = [
            SeatCandidate("a", "1", "VIP석", "1열", "1"),
            SeatCandidate("b", "2", "R석", "1열", "2"),
            SeatCandidate("c", "3", "S석", "1열", "3"),
        ]
        self.assertEqual(
            [seat.seat_grade_name for seat in rank_seats(musical, preferences)],
            ["S석", "VIP석", "R석"],
        )

        # Same preference, a show where grade "1" means something entirely different.
        concert = [
            SeatCandidate("d", "1", "EARLY ENTRY PACKAGE", "1열", "1"),
            SeatCandidate("e", "3", "스탠딩 P", "1열", "2"),
            SeatCandidate("f", "7", "지정석 S석", "1열", "3"),
        ]
        self.assertEqual(rank_seats(concert, preferences)[0].seat_grade_name, "지정석 S석")

    def test_grade_strict_drops_unmatched_grades(self) -> None:
        seats = [
            SeatCandidate("a", "1", "VIP석", "1열", "1"),
            SeatCandidate("b", "2", "R석", "1열", "2"),
        ]
        loose = rank_seats(seats, SeatPreferences(grade_order=("VIP석",)))
        self.assertEqual(len(loose), 2)
        strict = rank_seats(seats, SeatPreferences(grade_order=("VIP석",), grade_strict=True))
        self.assertEqual([seat.seat_grade_name for seat in strict], ["VIP석"])

    def test_empty_grade_order_keeps_show_order(self) -> None:
        prefs = SeatPreferences.from_mapping({"grade_order": ""})
        self.assertEqual(prefs.grade_order, ())
        seats = [
            SeatCandidate("b", "2", "R석", "1열", "2"),
            SeatCandidate("a", "1", "VIP석", "1열", "1"),
        ]
        self.assertEqual(len(rank_seats(seats, prefs)), 2)

    def test_group_seats_are_atomic_units(self) -> None:
        seats = [
            SeatCandidate("g1", "1", "VIP PACKAGE", "1열", "1", seat_group_id="pkg"),
            SeatCandidate("g2", "1", "VIP PACKAGE", "1열", "2", seat_group_id="pkg"),
            SeatCandidate("s1", "2", "R석", "2열", "1"),
            SeatCandidate("s2", "2", "R석", "2열", "2"),
        ]
        units = group_seat_candidates(seats)
        self.assertEqual([len(unit) for unit in units], [2, 1, 1])

        pair = select_seat_unit(seats, 2)
        self.assertEqual([seat.seat_info_id for seat in pair], ["g1", "g2"])

        # A 2-seat package cannot satisfy a single-ticket request.
        single = select_seat_unit(seats, 1)
        self.assertEqual([seat.seat_info_id for seat in single], ["s1"])

    def test_group_of_wrong_size_is_skipped(self) -> None:
        seats = [
            SeatCandidate("g1", "1", "테이블석", "1열", "1", seat_group_id="t4"),
            SeatCandidate("g2", "1", "테이블석", "1열", "2", seat_group_id="t4"),
            SeatCandidate("g3", "1", "테이블석", "1열", "3", seat_group_id="t4"),
            SeatCandidate("s1", "2", "일반석", "2열", "1"),
            SeatCandidate("s2", "2", "일반석", "2열", "2"),
        ]
        picked = select_seat_unit(seats, 2)
        self.assertEqual([seat.seat_info_id for seat in picked], ["s1", "s2"])

    def test_flow_classification_covers_every_engine(self) -> None:
        cases = {
            "onestop-reserved": {"isIngredientOnestop": True, "isReservedSeat": True},
            "onestop-sports": {
                "isIngredientOnestop": True,
                "isReservedSeat": True,
                "isSportOneStop": True,
            },
            "onestop-general-admission": {"isIngredientOnestop": True, "isReservedSeat": False},
            "legacy-poticket": {"isIngredientOnestop": False, "isReservedSeat": True},
        }
        for expected, flags in cases.items():
            compatibility = parse_summary_compatibility({"goodsCode": "26011315", **flags})
            self.assertEqual(compatibility.flow_label, expected)
            self.assertTrue(compatibility.flow_description)
        self.assertFalse(
            parse_summary_compatibility(
                {"goodsCode": "1", "isIngredientOnestop": True, "isReservedSeat": False}
            ).needs_seat_picking
        )

    def test_sports_seat_type(self) -> None:
        self.assertEqual(resolve_seat_type({"isSportOneStop": True}), "SPORTS")
        self.assertEqual(resolve_seat_type({"kindOfGoods": "01007"}), "SPORTS")
        self.assertEqual(resolve_seat_type({"kindOfGoods": "01011"}), "DEFAULT")

    def test_decode_status_mask_is_msb_first_nibbles(self) -> None:
        self.assertEqual(decode_status_mask("8"), [True, False, False, False])
        self.assertEqual(decode_status_mask("1"), [False, False, False, True])
        self.assertEqual(decode_status_mask("F0"), [True] * 4 + [False] * 4)
        self.assertEqual(decode_status_mask(""), [])
        # Four seats per hex character, as observed on live blocks.
        self.assertEqual(len(decode_status_mask("0FFFF01FFFFC")), 48)

    def test_parse_seat_status_envelope(self) -> None:
        masks = parse_seat_status({"data": ["F0", "0F"]})
        self.assertEqual(len(masks), 2)
        self.assertEqual(masks[0][:4], [True] * 4)
        self.assertEqual(masks[1][:4], [False] * 4)
        self.assertEqual(parse_seat_status(None), [])

    def test_sold_out_show_yields_no_candidates(self) -> None:
        """isExposable stays true on a sold-out show; only the bitmap says otherwise.

        Modelled on Maroon 5, where every seat reports isExposable but the
        status bitmap has a single bit set.
        """
        seats = [
            {
                "seatInfoId": f"g:p:001:{i}",
                "seatGrade": "1",
                "seatGradeName": "스탠딩 P",
                "rowNo": "A구역",
                "seatNo": str(i),
                "isExposable": True,
            }
            for i in range(8)
        ]
        blocks = [{"blockKey": "001:001", "seats": seats}]

        without_mask = filter_api_seats(blocks, SeatPreferences())
        self.assertEqual(len(without_mask), 8)

        # "10" -> 0001 0000: only seat index 3 is actually free.
        masks = parse_seat_status({"data": ["10"]})
        with_mask = filter_api_seats(blocks, SeatPreferences(), masks)
        self.assertEqual([seat.seat_no for seat in with_mask], ["3"])

    def test_mask_shorter_than_block_is_not_assumed_free(self) -> None:
        seats = [
            {
                "seatInfoId": f"s{i}",
                "seatGrade": "1",
                "seatGradeName": "R석",
                "rowNo": "A",
                "seatNo": str(i),
                "isExposable": True,
            }
            for i in range(8)
        ]
        masks = [[True, True]]
        picked = filter_api_seats([{"blockKey": "b", "seats": seats}], SeatPreferences(), masks)
        self.assertEqual([seat.seat_no for seat in picked], ["0", "1"])

    def test_diff_status_masks_finds_freed_seats(self) -> None:
        previous = [False, True, False, False]
        current = [True, True, False, True]
        self.assertEqual(diff_status_masks(previous, current), [0, 3])
        # A seat being taken is not a catch.
        self.assertEqual(diff_status_masks([True, True], [False, True]), [])
        self.assertEqual(diff_status_masks([], [True]), [])

    def test_auto_assign_payload_allows_empty_seats(self) -> None:
        init_data = {
            "sessionId": "s",
            "goods": {"goodsCode": "1", "placeCode": "2", "kindOfGoods": "01007"},
            "playSeq": {"playSeq": "001"},
        }
        payload = build_select_payload(init_data=init_data, seats=[], auto_assign=True)
        self.assertTrue(payload["autoAssign"])
        self.assertEqual(payload["seatType"], "SPORTS")
        self.assertEqual(payload["seats"], [])


class SeatOrderLinesTest(unittest.TestCase):
    """The readout that makes "무대 가까운 순 is random" checkable.

    Before this, the ranking happened entirely inside the page and nothing
    about it reached the screen, so a seat that looked wrong on the map could
    not be told apart from an ordering that was wrong.
    """

    def test_lists_the_winner_and_what_it_beat(self) -> None:
        lines = seat_order_lines({
            "stagePoint": {"x": 288, "y": 60},
            "seatOrder": [
                {"label": "[R석] A열 12", "x": 288, "y": 60, "dist": 0},
                {"label": "[R석] B열 12", "x": 288, "y": 66, "dist": 6},
            ],
        })
        self.assertEqual(lines[0], "무대 기준점  (288, 60)")
        self.assertIn("1. [R석] A열 12", lines[1])
        self.assertIn("거리 0", lines[1])
        self.assertIn("2. [R석] B열 12", lines[2])

    def test_shows_at_most_five_runners_up(self) -> None:
        order = [{"label": f"s{i}", "x": i, "y": 0, "dist": i} for i in range(9)]
        lines = seat_order_lines({"seatOrder": order})
        self.assertEqual(len(lines), 5, "a long list would push the buttons off screen")

    def test_a_seat_without_coordinates_still_appears(self) -> None:
        # It sorts last, but hiding it would make an ordering with unplaced
        # seats look shorter than it is.
        lines = seat_order_lines({
            "seatOrder": [{"label": "어딘가", "x": None, "y": None, "dist": None}],
        })
        self.assertEqual(lines, ["1. 어딘가  (좌표 없음)  거리 —"])

    def test_nothing_ranked_yet_renders_nothing(self) -> None:
        for status in ({}, {"seatOrder": []}, {"seatOrder": "nonsense"}):
            self.assertEqual(seat_order_lines(status), [], status)


class MapMoveLinesTest(unittest.TestCase):
    """What travelling to a seat costs, once it is measured rather than assumed."""

    def test_reports_each_kind_of_move_with_its_real_cost(self) -> None:
        lines = map_move_lines({"mapMoves": {
            "enterBlock": {"n": 3, "totalMs": 1200, "worstMs": 620, "failed": 1},
            "fitBlock": {"n": 5, "totalMs": 900, "worstMs": 300, "failed": 0},
        }})
        self.assertIn("구역 열기  3회 · 평균 400ms · 최대 620ms · 실패 1회", lines)
        self.assertIn("화면 맞추기  5회 · 평균 180ms · 최대 300ms", lines)
        self.assertNotIn("실패", lines[1], "a move that never failed says nothing about failure")

    def test_a_move_never_made_is_not_listed(self) -> None:
        # A zero-count row would divide by zero, and an empty line tells you
        # nothing anyway.
        self.assertEqual(map_move_lines({"mapMoves": {"aim": {"n": 0, "totalMs": 0}}}), [])

    def test_nothing_measured_yet_renders_nothing(self) -> None:
        for status in ({}, {"mapMoves": {}}, {"mapMoves": "nonsense"}):
            self.assertEqual(map_move_lines(status), [], status)

    def test_it_reports_what_reading_the_seat_map_costs(self) -> None:
        # Four querySelectorAll passes over a document that on 아레나 킨텍스
        # holds 21,460 seats, at 26 call sites, several per tick. Whether that
        # is worth reducing is a measurement, not an opinion.
        lines = map_move_lines({"domScans": 240, "domScanMs": 1080.0, "domScanWorstMs": 31})
        self.assertEqual(lines, ["좌석맵 읽기  240회 · 평균 4.5ms · 최대 31ms"])

    def test_scan_cost_shows_even_with_no_map_moves(self) -> None:
        # A watch inside one 구역 never travels, and its scan cost is still the
        # thing worth knowing.
        self.assertTrue(map_move_lines({"domScans": 5, "domScanMs": 20.0, "domScanWorstMs": 6}))


if __name__ == "__main__":
    unittest.main()
