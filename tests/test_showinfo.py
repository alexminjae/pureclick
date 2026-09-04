from __future__ import annotations

import unittest

from core.showinfo import (
    build_warnings,
    format_open_time,
    merge_grades,
    parse_price_groups,
    parse_remain_rows,
    unwrap,
)
from core.seat import parse_summary_compatibility


class ShowInfoParsingTests(unittest.TestCase):
    def test_format_open_time(self) -> None:
        self.assertEqual(format_open_time("202608141200"), "2026-08-14 12:00:00")
        self.assertEqual(format_open_time(""), "")
        self.assertEqual(format_open_time("2026081"), "")

    def test_unwrap_handles_both_envelopes(self) -> None:
        # playSeq/remain endpoints wrap in {common, data}
        self.assertEqual(unwrap({"common": {}, "data": {"remainSeat": []}}), {"remainSeat": []})
        # prices/group returns its map at the top level
        top_level = {"VIP석": {}}
        self.assertEqual(unwrap(top_level), top_level)

    def test_parse_price_groups_real_shape(self) -> None:
        payload = {
            "VIP석": {
                "기본가": [
                    {"seatGrade": "1", "seatGradeName": "VIP석", "salesPrice": 180000},
                ],
                "조기예매": [
                    {"seatGrade": "1", "seatGradeName": "VIP석", "salesPrice": 144000},
                ],
            },
            "R석": {"기본가": [{"seatGrade": "2", "seatGradeName": "R석", "salesPrice": 150000}]},
        }
        prices = parse_price_groups(payload)
        self.assertEqual(prices["1"], {"price": 180000, "name": "VIP석"})
        self.assertEqual(prices["2"]["price"], 150000)

    def test_parse_remain_rows_collects_rounds(self) -> None:
        payload = {
            "common": {},
            "data": {
                "remainSeat": [
                    {"playSeq": "001", "seatGrade": "1", "seatGradeName": "R석", "remainCnt": 10},
                    {"playSeq": "002", "seatGrade": "1", "seatGradeName": "R석", "remainCnt": 4},
                ]
            },
        }
        rows, play_seqs = parse_remain_rows(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(play_seqs, ["001", "002"])

    def test_merge_grades_sums_rounds_and_sorts_by_price(self) -> None:
        rows = [
            {"playSeq": "001", "seatGrade": "2", "seatGradeName": "R석", "remainCnt": 3},
            {"playSeq": "002", "seatGrade": "2", "seatGradeName": "R석", "remainCnt": 5},
            {"playSeq": "001", "seatGrade": "1", "seatGradeName": "VIP석", "remainCnt": 1},
        ]
        prices = {"1": {"price": 180000, "name": "VIP석"}, "2": {"price": 150000, "name": "R석"}}
        grades = merge_grades(rows, prices)
        self.assertEqual([grade.name for grade in grades], ["VIP석", "R석"])
        self.assertEqual(grades[1].remain, 8)

    def test_merge_grades_can_filter_one_round(self) -> None:
        rows = [
            {"playSeq": "015", "seatGrade": "1", "seatGradeName": "VIP석", "remainCnt": 208},
            {"playSeq": "016", "seatGrade": "1", "seatGradeName": "VIP석", "remainCnt": 36},
            {"playSeq": "015", "seatGrade": "2", "seatGradeName": "R석", "remainCnt": 150},
            {"playSeq": "016", "seatGrade": "2", "seatGradeName": "R석", "remainCnt": 131},
        ]
        prices = {"1": {"price": 180000, "name": "VIP석"}, "2": {"price": 150000, "name": "R석"}}
        grades = merge_grades(rows, prices, play_seq="015")
        self.assertEqual(grades[0].remain, 208)
        self.assertEqual(grades[1].remain, 150)

    def test_pick_schedule_matches_play_time(self) -> None:
        from core.showinfo import normalize_play_time, pick_schedule

        schedules = [
            {"playSeq": "015", "playTime": "14:30"},
            {"playSeq": "016", "playTime": "19:30"},
        ]
        self.assertEqual(pick_schedule(schedules, play_time="14:30")["playSeq"], "015")
        self.assertEqual(pick_schedule(schedules, play_seq="016")["playSeq"], "016")
        self.assertEqual(normalize_play_time("9:05"), "09:05")
        self.assertEqual(pick_schedule(schedules, play_time="19:30")["playSeq"], "016")
        self.assertEqual(pick_schedule(schedules)["playSeq"], "015")

    def test_merge_grades_names_price_only_grades(self) -> None:
        """A grade with no remain row still shows its real name, not its code."""
        grades = merge_grades([], {"1": {"price": 90000, "name": "프리미엄석"}})
        self.assertEqual(grades[0].name, "프리미엄석")

    def test_warnings_flag_unsupported_engines(self) -> None:
        legacy = parse_summary_compatibility(
            {"goodsCode": "26009868", "isIngredientOnestop": False, "isReservedSeat": True}
        )
        # The old-engine flag is a hint, not a fault: it must never produce the
        # poticket warning that surfaced as a red 문제가 발생했습니다 box over a
        # perfectly good arm (측정: 드라큘라, 김주택 enter through onestop fine).
        self.assertFalse(any("poticket" in warning for warning in build_warnings(legacy)))

        general = parse_summary_compatibility(
            {"goodsCode": "26010333", "isIngredientOnestop": True, "isReservedSeat": False}
        )
        self.assertTrue(any("비지정석" in warning for warning in build_warnings(general)))

        reserved = parse_summary_compatibility(
            {"goodsCode": "26011315", "isIngredientOnestop": True, "isReservedSeat": True}
        )
        self.assertEqual(build_warnings(reserved), [])


if __name__ == "__main__":
    unittest.main()


class GoodsInfoRoundsTests(unittest.TestCase):
    """The panel's own round fallback: goods-info → picker-shaped rounds."""

    def test_only_rounds_on_sale_now_are_offered(self) -> None:
        from core.showinfo import goods_info_rounds
        payload = {"playSeqList": [
            {"playSeq": "041", "playDate": "20260904", "playTime": "1930",
             "saleOpenTime": "20260709140000", "saleCloseTime": "20260904163000"},
            {"playSeq": "001", "playDate": "20260804", "playTime": "1930",
             "saleOpenTime": "20260709140000", "saleCloseTime": "20260804163000"},
            {"playSeq": "120", "playDate": "20261201", "playTime": "1930",
             "saleOpenTime": "20261101140000", "saleCloseTime": "20261201163000"},
        ]}
        rounds = goods_info_rounds(payload, now_compact="20260904120000")
        self.assertEqual([r["play_seq"] for r in rounds], ["041"])
        self.assertEqual(rounds[0]["play_date"], "20260904")

    def test_a_bad_payload_is_no_rounds(self) -> None:
        from core.showinfo import goods_info_rounds
        self.assertEqual(goods_info_rounds(None), [])
        self.assertEqual(goods_info_rounds({"path": "/x", "message": "err"}), [])
