from __future__ import annotations

import unittest

from pureclick_arm_core import ArmPayload, browser_config_snippet


class PureClickArmCoreTests(unittest.TestCase):
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
        self.assertIn("pureclick_arm_v1", snippet)
        self.assertIn("pureclick_seat_v1", snippet)


if __name__ == "__main__":
    unittest.main()
