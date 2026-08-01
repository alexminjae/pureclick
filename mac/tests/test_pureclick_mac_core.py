from __future__ import annotations

import platform
import unittest

from pureclick_mac_core import (
    cg_point_to_top_left,
    screen_height,
    top_left_to_cg_point,
)


class PureClickMacCoreTests(unittest.TestCase):
    def test_coordinate_round_trip(self) -> None:
        if platform.system() != "Darwin":
            self.skipTest("macOS coordinate tests require Darwin")
        height = screen_height()
        original = (120, 340)
        point = top_left_to_cg_point(*original)
        self.assertAlmostEqual(point.y, height - original[1])
        self.assertEqual(cg_point_to_top_left(point), original)


if __name__ == "__main__":
    unittest.main()
