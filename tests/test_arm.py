from __future__ import annotations

import unittest

from core.arm import ENTRY_OFFSET_LIMIT_MS, ArmPayload, browser_config_snippet, default_entry_offset_ms


class NolSniperArmCoreTests(unittest.TestCase):
    def test_arm_payload_round_trip(self) -> None:
        payload = ArmPayload(
            enabled=True,
            goods_code="26006325",
            play_date="20260630",
            play_seq="001",
            target_server_unix=1730000000.5,
            offset_seconds=0.04,
            dry_run=True,
        )
        restored = ArmPayload.from_mapping(payload.to_mapping())
        self.assertEqual(restored.goods_code, "26006325")
        self.assertTrue(restored.dry_run)
        self.assertEqual(restored.channel_code, "pc")

    def test_arm_payload_nol_fields(self) -> None:
        payload = ArmPayload.from_mapping(
            {
                "goods_code": "26011315",
                "play_date": "20270127",
                "play_seq": "001",
                "target_server_unix": 1730000000.5,
                "place_code": "26000914",
                "channel_code": "pc",
                "pre_sales": "N",
            }
        )
        self.assertEqual(payload.place_code, "26000914")
        self.assertEqual(payload.pre_sales, "N")

    def test_browser_config_snippet_contains_keys(self) -> None:
        payload = ArmPayload(
            enabled=True,
            goods_code="26006325",
            play_date="20260630",
            play_seq="001",
            target_server_unix=1730000000.5,
            offset_seconds=0.04,
        )
        snippet = browser_config_snippet(
            arm_payload=payload,
            seat_mapping={"enabled": True, "grade_order": ["2", "3"]},
        )
        self.assertIn("nolsniper_arm_v1", snippet)
        self.assertIn("nolsniper_seat_v1", snippet)

    def test_the_entry_offset_survives_the_round_trip(self) -> None:
        # It crosses the bridge as JSON and is read back by the autopilot, so a
        # correction that does not survive serialisation is a correction that
        # silently does nothing on the one day it matters.
        payload = ArmPayload(
            enabled=True,
            goods_code="26006325",
            play_date="20260630",
            play_seq="001",
            target_server_unix=1730000000.5,
            offset_seconds=0.04,
            entry_offset_ms=-250,
        )
        again = ArmPayload.from_mapping(payload.to_mapping())
        self.assertEqual(again.entry_offset_ms, -250)
        # 티켓 오픈 itself must not move. The panel shows the published open and
        # the corrected fire side by side, and folding them would leave no way
        # to tell a tuned open from a mistyped one.
        self.assertEqual(again.target_server_unix, 1730000000.5)

    def test_a_nonsense_offset_is_ignored_rather_than_refusing_to_arm(self) -> None:
        base = {
            "goods_code": "26006325",
            "play_date": "20260630",
            "play_seq": "001",
            "target_server_unix": 1730000000.5,
        }
        self.assertEqual(ArmPayload.from_mapping({**base, "entry_offset_ms": "abc"}).entry_offset_ms, 0)
        self.assertEqual(ArmPayload.from_mapping({**base, "entry_offset_ms": ""}).entry_offset_ms, 0)
        self.assertEqual(ArmPayload.from_mapping({**base, "entry_offset_ms": "-250"}).entry_offset_ms, -250)

    def test_the_offset_is_a_correction_not_a_second_open_time(self) -> None:
        # Past a few seconds this stops being a correction for server-side lag
        # and becomes an open time typed into the wrong field.
        base = {
            "goods_code": "26006325",
            "play_date": "20260630",
            "play_seq": "001",
            "target_server_unix": 1730000000.5,
        }
        self.assertEqual(
            ArmPayload.from_mapping({**base, "entry_offset_ms": -3600_000}).entry_offset_ms,
            -ENTRY_OFFSET_LIMIT_MS,
        )
        self.assertEqual(
            ArmPayload.from_mapping({**base, "entry_offset_ms": 999_999}).entry_offset_ms,
            ENTRY_OFFSET_LIMIT_MS,
        )


if __name__ == "__main__":
    unittest.main()


class EntryLeadTests(unittest.TestCase):
    def test_default_lead_is_150ms_early(self) -> None:
        self.assertEqual(default_entry_offset_ms(0), -150)
        self.assertEqual(default_entry_offset_ms(None), -150)

    def test_a_longer_round_trip_leads_by_the_round_trip(self) -> None:
        self.assertEqual(default_entry_offset_ms(230), -230)

    def test_the_lead_is_capped(self) -> None:
        self.assertEqual(default_entry_offset_ms(5000), -600)
