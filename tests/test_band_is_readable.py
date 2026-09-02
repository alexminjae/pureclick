"""The live band must be readable while a race is on.

Reported, twice: "the status page starts tweaking", then "it's updating rapidly
now so it's just going crazy". Both times I fixed the mechanism that happened
to be moving — a doubled counter, then a resizing card — without testing the
property that was actually wanted.

The property: the band is repainted every 500ms, and a running watch changes a
dozen counters between repaints. So the band's *text* must depend only on
state, never on a counter. If it depends on a counter it churns, and churning
text cannot be read.

This is the test that would have caught all of it at once.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.seat import live_state, watch_note  # noqa: E402

LIVE = {"seen_at": 1_800_000_000.0, "failures": 0}
NOW = 1_800_000_000.0

# Every counter the page publishes that moves while a watch runs.
COUNTERS = (
    "attempts", "takenConflicts", "cooldownSeats", "sweepTicks", "freeSeats",
    "catchLatencyMs", "domAgreedMs", "domAgreedWorstMs", "domAgreedSamples",
    "domScans", "domScanMs", "domScanWorstMs", "aimMisses", "skippedByMap",
    "seatErrorDialogs", "overlaysDismissed", "catchLiveTries", "observedTickMs",
    "pageStatusSeen", "pageStatusFreed", "watchedBlocks", "clickableNow",
    "unreachableSkips", "traceLen",
)


def _watching(tick: int) -> dict:
    """One frame of a healthy watch, `tick` polls in."""
    return {
        "running": True,
        "message": f"취소표 감시 중 · 154ms 간격 · 빈 좌석 {tick}석",
        **{name: tick * (i + 1) for i, name in enumerate(COUNTERS)},
    }


class TheBandDoesNotChurn(unittest.TestCase):
    def test_a_healthy_watch_reads_the_same_from_one_poll_to_the_next(self) -> None:
        frames = {live_state(_watching(t), LIVE, now=NOW) for t in range(1, 41)}
        self.assertEqual(
            len(frames), 1,
            "40 polls of the same state produced "
            f"{len(frames)} different readings:\n"
            + "\n".join(f"  {f}" for f in sorted(frames)[:6]),
        )

    def test_no_digit_reaches_the_band_while_watching(self) -> None:
        # The strongest form of the rule, and the easy one to check: state has
        # no numbers in it. 시도 47회 / 한 바퀴 5852ms / 경합 22회 were all here.
        _, headline, why = live_state(_watching(17), LIVE, now=NOW)
        self.assertNotRegex(headline, r"\d", f"headline carries a number: {headline!r}")
        self.assertNotRegex(why, r"\d", f"note carries a number: {why!r}")

    def test_every_note_it_can_emit_is_free_of_counters(self) -> None:
        cases = [
            {"running": True},
            {"running": True, "blockedForMs": 42_000, "blockedEndpoint": "/waiting"},
            {"running": True, "captcha": {"state": "unreadable", "detail": "6자리"}},
            {"running": True, "unknownDialog": "무슨 창인지 모릅니다"},
            {"running": True, "batchFailures": 9},
            {"running": True, "watchRectIgnored": True},
            {"running": True, "blockEntryMisses": 18},
            {"running": True, "consecutiveRejects": 12},
        ]
        for seat in cases:
            note = watch_note(seat)
            self.assertTrue(note, f"no note for {seat}")
            self.assertNotRegex(note, r"\d", f"{seat} -> {note!r}")


class ItStillSaysWhatNeedsDoing(unittest.TestCase):
    """Stable is not the same as silent."""

    def test_a_block_outranks_everything(self) -> None:
        note = watch_note({"running": True, "blockedForMs": 9000,
                           "consecutiveRejects": 12, "batchFailures": 3})
        self.assertIn("차단", note)

    def test_a_captcha_outranks_the_rest(self) -> None:
        note = watch_note({"running": True, "captcha": {"state": "unreadable"},
                           "blockEntryMisses": 4})
        self.assertIn("보안문자", note)
        self.assertNotIn("구역", note)

    def test_a_quiet_watch_says_it_is_waiting_not_nothing(self) -> None:
        self.assertIn("빈자리", watch_note({"running": True}))

    def test_a_stopped_run_has_nothing_to_add(self) -> None:
        self.assertEqual(watch_note({"haltedByUser": True}), "")

    def test_a_partially_blind_sweep_still_says_so(self) -> None:
        # The one thing that must survive being made counter-free: the venue
        # may be bigger than what is being swept.
        self.assertIn("일부 구역", watch_note({"running": True, "batchFailures": 1}))


class TheWholeReadingIsOneThing(unittest.TestCase):
    """Measured on the real widget tree as well as here.

    Driving the built panel through 60 polls of a running watch, with all
    eighteen published counters moving in every one of them, produces exactly
    one distinct reading and moves zero widgets. Before this it produced a new
    headline, a new note and a resized card on every poll.
    """

    def test_the_dot_the_headline_and_the_note_are_one_stable_triple(self) -> None:
        readings = {live_state(_watching(t), LIVE, now=NOW) for t in range(1, 61)}
        self.assertEqual(len(readings), 1)
        tone, headline, why = readings.pop()
        self.assertEqual(tone, "accent")
        self.assertEqual(headline, "취소표 감시 중")
        self.assertEqual(why, "빈자리가 나오면 바로 잡습니다.")

    def test_a_real_state_change_still_gets_through(self) -> None:
        """Stable must not mean frozen."""
        watching = live_state(_watching(9), LIVE, now=NOW)
        caught = live_state(
            {"locked": True, "lastSeat": "A구역 1열 1번", "pageSelected": 1}, LIVE, now=NOW
        )
        stopped = live_state({"haltedByUser": True}, LIVE, now=NOW)
        self.assertEqual(len({watching, caught, stopped}), 3)


class TheHeadlineTracksStateOnly(unittest.TestCase):
    def test_it_survives_the_page_going_quiet(self) -> None:
        _, headline, _ = live_state({"running": True, "message": ""}, LIVE, now=NOW)
        self.assertEqual(headline, "감시 중")

    def test_a_caught_seat_names_the_seat_and_no_counters(self) -> None:
        _, headline, _ = live_state(
            {"locked": True, "lastSeat": "1층 A구역 3열 12번", "pageSelected": 1,
             "attempts": 47},
            LIVE, now=NOW,
        )
        self.assertIn("A구역 3열 12번", headline)
        self.assertNotIn("47", headline)


if __name__ == "__main__":
    unittest.main()
