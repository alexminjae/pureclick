"""What the 조작판's live band says, and when it is allowed to say it.

Written from a reported failure: turn on 들어가면 곧바로 좌석까지 잡기, let it
catch a seat, cancel that seat in the 예매 창, press [감시 시작] — and the band
stays green on 좌석 잡음 forever. The panel is supposed to be watching the 예매
창 continuously; it was replaying a cache.

Three separate defects produced that one symptom, and each has a case below:

  1. `_apply_autopilot_status` opened with `if not message: return`, so the
     browser's truthful "I am idle" after a reload was discarded and the last
     frame repainted.
  2. `locked` was tested before `lastError`, so a press the page received and
     *refused* rendered as the green it was refusing to disturb.
  3. Nothing consulted the bridge's age, so a dead browser rendered stale state
     in full colour.

`live_state` is pure, so all of it runs without a display.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.seat import (  # noqa: E402
    ACK_SECONDS,
    TONE_ACCENT,
    TONE_AMBER,
    TONE_FAINT,
    TONE_GREEN,
    live_state,
    watch_vitals,
)

NOW = 1_800_000_000.0
LIVE = {"seen_at": NOW - 0.2, "failures": 0}

# What the page publishes the moment it has locked a seat.
CAUGHT = {
    "running": False,
    "locked": True,
    "lastSeat": "1층 A구역 3열 12번",
    "message": "좌석 선점 완료",
    "attempts": 4,
    "pageSelected": 1,
}


class TheReportedBug(unittest.TestCase):
    def test_a_caught_seat_is_green(self) -> None:
        tone, headline, _ = live_state(CAUGHT, LIVE, now=NOW)
        self.assertEqual(tone, TONE_GREEN)
        self.assertIn("좌석 잡음", headline)
        self.assertIn("A구역 3열 12번", headline)

    def test_a_reloaded_page_clears_the_green(self) -> None:
        """Defect 1, and the whole reason this file exists.

        Cancelling navigates, the autopilot is re-injected, and `seatState` is
        fresh: message "", locked false, attempts 0. That is a complete and
        truthful report of an idle page. The panel used to throw it away
        because the message was empty and go on painting 좌석 잡음.
        """
        fresh = {"running": False, "locked": False, "message": "", "attempts": 0}
        tone, headline, _ = live_state(fresh, LIVE, now=NOW)
        self.assertEqual(tone, TONE_FAINT)
        self.assertIn("대기 중", headline)
        self.assertNotIn("좌석 잡음", headline)

    def test_a_refused_press_says_it_was_refused(self) -> None:
        """Defect 2 — cancelling without a reload.

        The alreadyHolding branch sets lastError and returns without ever
        setting `running`, so `locked` is still true. Testing `locked` first
        drew a green congratulation over a press that had just been rejected.
        """
        refused = {
            **CAUGHT,
            "lastExit": "alreadyHolding",
            "lastError": "이미 좌석 1석을 잡고 있습니다. 예매 창에서 결제를 마치거나 "
                         "좌석을 비운 뒤 다시 [감시 시작]을 누르세요.",
        }
        tone, headline, why = live_state(refused, LIVE, now=NOW)
        self.assertEqual(tone, TONE_AMBER)
        self.assertNotIn("좌석 잡음", headline)
        self.assertIn("시작하지 않았습니다", headline)
        self.assertIn("좌석을 비운 뒤", why, "the page's own words are the useful ones")

    def test_the_watch_restarting_is_drawn_immediately(self) -> None:
        """The end of the sequence: the press lands and the watch runs."""
        watching = {
            "running": True,
            "locked": False,
            "message": "취소표 감시 중 · 구역 4",
            "attempts": 12,
        }
        tone, headline, _ = live_state(watching, LIVE, now=NOW)
        self.assertEqual(tone, TONE_ACCENT)
        self.assertIn("취소표 감시 중", headline)


class AHeadlineHoldsStill(unittest.TestCase):
    """The catch loop polls at 100ms and the panel repaints at 500ms, so
    anything counting in the headline moves five at a time — and the no-seat
    path put the same count in twice, once from the page's own overlay text and
    once from here. Three lines all churning on the same tick is what "the
    status bar tweaks" was."""

    @staticmethod
    def _frame(attempts: int) -> tuple[str, str]:
        seat = {"running": True, "attempts": attempts,
                "message": f"좌석맵 대기 ({attempts}) · 후보 없음 · 화면 좌석 0개"}
        _, headline, _ = live_state(seat, LIVE, now=NOW)
        return headline, watch_vitals(seat)

    def test_the_headline_is_identical_across_frames(self) -> None:
        heads = {self._frame(n)[0] for n in (0, 5, 10, 15, 20, 25)}
        self.assertEqual(heads, {"좌석맵 대기"}, "a headline names a state, not a count")

    def test_the_count_is_not_lost_only_moved(self) -> None:
        self.assertIn("시도 25회", self._frame(25)[1])

    def test_a_stopped_run_still_shows_its_count_in_the_headline(self) -> None:
        # Nothing is incrementing there, so it costs no churn and it is the
        # number you want when a run has ended.
        _, headline, _ = live_state(
            {"haltedByUser": True, "attempts": 31}, LIVE, now=NOW
        )
        self.assertIn("시도 31회", headline)


class AbandoningASeat(unittest.TestCase):
    """Seeing the user give a seat up and go back to 취켓팅."""

    def test_an_empty_cart_means_the_seat_is_gone(self) -> None:
        released = {**CAUGHT, "pageSelected": 0}
        tone, headline, why = live_state(released, LIVE, now=NOW)
        self.assertEqual(tone, TONE_FAINT)
        self.assertIn("놓았습니다", headline)
        self.assertIn("감시 시작", why, "and what to press next")

    def test_an_unreadable_cart_keeps_the_green_but_names_the_way_out(self) -> None:
        """`selectedSeatCount()` answers -1 more often than 0 on a seat page.

        The panel genuinely cannot tell, so it must not perform certainty in
        either direction — but it can say what to press, and a user-initiated
        run does clear a stale lock by itself.
        """
        unsure = {**CAUGHT, "pageSelected": -1}
        tone, _, why = live_state(unsure, LIVE, now=NOW)
        self.assertEqual(tone, TONE_GREEN)
        self.assertIn("취소했다면", why)
        self.assertIn("감시 시작", why)

    def test_a_held_seat_is_not_nagged_about(self) -> None:
        tone, _, why = live_state(CAUGHT, LIVE, now=NOW)
        self.assertEqual(tone, TONE_GREEN)
        self.assertNotIn("취소했다면", why)


class NothingBelowATrustedBridge(unittest.TestCase):
    """Defect 3. A claim about a page nobody is reading is not a status."""

    def test_a_dead_bridge_never_renders_green(self) -> None:
        tone, headline, why = live_state(CAUGHT, {"seen_at": NOW - 12}, now=NOW)
        self.assertNotEqual(tone, TONE_GREEN)
        self.assertIn("응답 없음", headline)
        self.assertIn("지금 상태가 아닙니다", why)

    def test_a_live_loop_that_cannot_read_is_not_a_live_bridge(self) -> None:
        """The common shape while a cancelled seat page navigates.

        `seen_at` keeps moving — the loop is fine — but every read throws, so
        `autopilot_status` is never rewritten and age alone calls it healthy.
        """
        failing = {"seen_at": NOW - 0.2, "failures": 4,
                   "last_error": "페이지에서 자동화 스크립트를 찾지 못했습니다"}
        tone, headline, _ = live_state(CAUGHT, failing, now=NOW)
        self.assertNotEqual(tone, TONE_GREEN)
        self.assertIn("읽기 실패", headline)

    def test_before_the_host_has_started_it_is_not_an_alarm(self) -> None:
        tone, headline, _ = live_state({}, {}, now=NOW)
        self.assertEqual(tone, TONE_FAINT)
        self.assertIn("대기 중", headline)


class AnUnansweredPress(unittest.TestCase):
    """`apply_state` fires every command as `window.PureClick && …` and drops it
    either way, so a press against a page with no autopilot on it vanished."""

    def test_a_press_the_page_never_answered_is_reported(self) -> None:
        idle = {"running": False, "locked": False, "message": "", "attempts": 0}
        tone, headline, _ = live_state(
            idle, LIVE, asked=("감시 시작", ACK_SECONDS + 1), now=NOW
        )
        self.assertEqual(tone, TONE_AMBER)
        self.assertIn("반응하지 않았습니다", headline)

    def test_a_press_still_within_the_grace_period_is_not(self) -> None:
        idle = {"running": False, "locked": False, "message": "", "attempts": 0}
        tone, headline, _ = live_state(
            idle, LIVE, asked=("감시 시작", ACK_SECONDS - 1), now=NOW
        )
        self.assertEqual(tone, TONE_FAINT)
        self.assertIn("대기 중", headline)


class EmptyMessages(unittest.TestCase):
    def test_a_running_watch_with_nothing_to_say_still_says_it_is_running(self) -> None:
        """`message` is empty until the first `updateOverlay` of a run."""
        tone, headline, _ = live_state(
            {"running": True, "message": "", "attempts": 0}, LIVE, now=NOW
        )
        self.assertEqual(tone, TONE_ACCENT)
        self.assertIn("감시 중", headline)

    def test_a_halt_with_no_attempts_does_not_claim_zero_tries(self) -> None:
        _, headline, _ = live_state({"haltedByUser": True}, LIVE, now=NOW)
        self.assertEqual(headline, "멈춤")


class Vitals(unittest.TestCase):
    def test_it_reads_the_counters_the_page_already_publishes(self) -> None:
        line = watch_vitals(
            {"watchedBlocks": 4, "freeSeats": 2, "sweepTicks": 118, "takenConflicts": 3}
        )
        self.assertEqual(line, "구역 4 · 빈자리 2 · 스윕 118회 · 경합 3회")

    def test_zero_free_seats_is_a_reading_not_an_absence(self) -> None:
        # The whole point of a watch is a venue with nothing in it, and "0" is
        # the number that says the watch is looking.
        self.assertIn("빈자리 0", watch_vitals({"watchedBlocks": 4, "freeSeats": 0}))

    def test_it_costs_nothing_before_the_watch_has_run(self) -> None:
        self.assertEqual(watch_vitals({}), "")
        self.assertEqual(watch_vitals(None), "")


if __name__ == "__main__":
    unittest.main()
