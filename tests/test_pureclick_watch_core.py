from __future__ import annotations

import unittest

from pureclick_watch_core import (
    ConfirmTracker,
    FrameResult,
    WatchRegion,
    WatchSettings,
    classify_frame,
    grid_from_bgra,
    grid_point_to_screen,
)


def make_bgra(width: int, height: int, color: tuple[int, int, int]) -> bytearray:
    r, g, b = color
    return bytearray((b, g, r, 255) * (width * height))


def paint(
    buffer: bytearray,
    width: int,
    left: int,
    top: int,
    box_width: int,
    box_height: int,
    color: tuple[int, int, int],
) -> None:
    r, g, b = color
    for y in range(top, top + box_height):
        for x in range(left, left + box_width):
            index = (y * width + x) * 4
            buffer[index : index + 4] = bytes((b, g, r, 255))


class GridTests(unittest.TestCase):
    def test_grid_shape_and_packing(self) -> None:
        data = make_bgra(10, 6, (10, 20, 30))
        grid = grid_from_bgra(bytes(data), 10, 6, stride=2)
        self.assertEqual(grid.cols, 5)
        self.assertEqual(grid.rows, 3)
        self.assertEqual(len(grid.pixels), 15)
        self.assertTrue(all(pixel == (10 << 16) | (20 << 8) | 30 for pixel in grid.pixels))

    def test_rejects_short_buffer(self) -> None:
        with self.assertRaises(ValueError):
            grid_from_bgra(b"\x00" * 10, 10, 6, stride=2)


class ClassifyTests(unittest.TestCase):
    WIDTH = 40
    HEIGHT = 30

    def _grids(self, base_color, blob=None, blob_color=(230, 60, 60)):
        base = make_bgra(self.WIDTH, self.HEIGHT, base_color)
        current = bytearray(base)
        if blob:
            paint(current, self.WIDTH, *blob, blob_color)
        baseline_grid = grid_from_bgra(bytes(base), self.WIDTH, self.HEIGHT, stride=2)
        current_grid = grid_from_bgra(bytes(current), self.WIDTH, self.HEIGHT, stride=2)
        return baseline_grid, current_grid

    def test_identical_frames_are_quiet(self) -> None:
        baseline, current = self._grids((120, 120, 120))
        result = classify_frame(
            baseline, current, tolerance=40, min_points=3, max_fraction=0.35
        )
        self.assertEqual(result.kind, "quiet")
        self.assertEqual(result.changed, 0)

    def test_small_blob_is_candidate_with_target_inside(self) -> None:
        # 8x8 red blob at (20, 10) on a grey background.
        baseline, current = self._grids((120, 120, 120), blob=(20, 10, 8, 8))
        result = classify_frame(
            baseline, current, tolerance=40, min_points=3, max_fraction=0.35
        )
        self.assertEqual(result.kind, "candidate")
        assert result.point is not None
        region = WatchRegion(left=100, top=200, width=self.WIDTH, height=self.HEIGHT)
        x, y = grid_point_to_screen(region, result.point, 2)
        self.assertTrue(100 + 20 <= x < 100 + 28)
        self.assertTrue(200 + 10 <= y < 200 + 18)

    def test_change_below_tolerance_is_quiet(self) -> None:
        baseline, current = self._grids(
            (120, 120, 120), blob=(20, 10, 8, 8), blob_color=(140, 130, 125)
        )
        result = classify_frame(
            baseline, current, tolerance=40, min_points=3, max_fraction=0.35
        )
        self.assertEqual(result.kind, "quiet")

    def test_full_frame_change_is_unstable(self) -> None:
        baseline, _ = self._grids((120, 120, 120))
        _, white = self._grids((250, 250, 250))
        result = classify_frame(
            baseline, white, tolerance=40, min_points=3, max_fraction=0.35
        )
        self.assertEqual(result.kind, "unstable")

    def test_two_blobs_target_snaps_to_a_changed_point(self) -> None:
        base = make_bgra(self.WIDTH, self.HEIGHT, (120, 120, 120))
        current = bytearray(base)
        paint(current, self.WIDTH, 2, 2, 6, 6, (230, 60, 60))
        paint(current, self.WIDTH, 32, 22, 6, 6, (230, 60, 60))
        baseline_grid = grid_from_bgra(bytes(base), self.WIDTH, self.HEIGHT, stride=2)
        current_grid = grid_from_bgra(bytes(current), self.WIDTH, self.HEIGHT, stride=2)
        result = classify_frame(
            baseline_grid, current_grid, tolerance=40, min_points=3, max_fraction=0.35
        )
        self.assertEqual(result.kind, "candidate")
        assert result.point is not None
        # The centroid falls between the blobs; the target must be a real
        # changed point, i.e. inside one of the blobs.
        gx, gy = result.point
        in_first = 1 <= gx <= 3 and 1 <= gy <= 3
        in_second = 16 <= gx <= 18 and 11 <= gy <= 13
        self.assertTrue(in_first or in_second)


class ConfirmTrackerTests(unittest.TestCase):
    def test_requires_consecutive_candidates(self) -> None:
        tracker = ConfirmTracker(2)
        candidate = FrameResult(kind="candidate", changed=5, point=(3, 4))
        quiet = FrameResult(kind="quiet", changed=0)
        self.assertIsNone(tracker.update(candidate))
        self.assertIsNone(tracker.update(quiet))  # streak broken
        self.assertIsNone(tracker.update(candidate))
        self.assertEqual(tracker.update(candidate), (3, 4))

    def test_single_frame_mode_fires_immediately(self) -> None:
        tracker = ConfirmTracker(1)
        candidate = FrameResult(kind="candidate", changed=5, point=(1, 2))
        self.assertEqual(tracker.update(candidate), (1, 2))


class SettingsTests(unittest.TestCase):
    def test_round_trip_with_region(self) -> None:
        settings = WatchSettings(
            tolerance=55,
            min_points=4,
            confirm_frames=3,
            poll_ms=80,
            refresh_seconds=5.0,
            region=WatchRegion(left=10, top=20, width=300, height=200),
        )
        restored = WatchSettings.from_mapping(settings.to_mapping())
        self.assertEqual(restored, settings)

    def test_rejects_tiny_region(self) -> None:
        with self.assertRaises(ValueError):
            WatchRegion(left=0, top=0, width=4, height=4)

    def test_rejects_bad_tolerance(self) -> None:
        with self.assertRaises(ValueError):
            WatchSettings(tolerance=0)


if __name__ == "__main__":
    unittest.main()
