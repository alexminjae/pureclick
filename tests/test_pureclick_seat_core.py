from __future__ import annotations

import unittest

from pureclick_seat_core import (
    SeatCandidate,
    SeatPreferences,
    build_select_payload,
    filter_api_seats,
    parse_goods_code,
    parse_summary_compatibility,
    rank_seats,
)


class PureClickSeatCoreTests(unittest.TestCase):
    def test_parse_goods_code_from_url(self) -> None:
        self.assertEqual(
            parse_goods_code("https://tickets.interpark.com/goods/26006325"),
            "26006325",
        )
        self.assertEqual(parse_goods_code("L0000142"), "L0000142")

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

    def test_filter_api_seats_skips_grouped_and_hidden(self) -> None:
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
        self.assertEqual(len(seats), 1)
        self.assertEqual(seats[0].seat_no, "1")

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


if __name__ == "__main__":
    unittest.main()
