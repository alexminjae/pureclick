"""mode → guidance: the banner and the instruction can never disagree."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.mode import BEFORE_OPEN, OPEN, UNKNOWN, derive_mode, guidance, sale_phase, MODES  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=KST)


class SalePhase(unittest.TestCase):
    def test_before_open_and_open_and_unknown(self) -> None:
        self.assertEqual(sale_phase(NOW + timedelta(hours=1), NOW), BEFORE_OPEN)
        self.assertEqual(sale_phase(NOW - timedelta(seconds=1), NOW), OPEN)
        self.assertEqual(sale_phase(None, NOW), UNKNOWN)


class DeriveMode(unittest.TestCase):
    def test_precedence(self) -> None:
        held = {"locked": True, "pageSelected": 1, "running": True}
        self.assertEqual(derive_mode(page="seat", seat=held, arm={"running": True}), "held")
        self.assertEqual(derive_mode(page="seat", seat={"locked": True, "pageSelected": 0}, arm={}), "on_seat")
        self.assertEqual(derive_mode(page="seat", seat={"running": True, "runMode": "catch"}, arm={}), "watching")
        self.assertEqual(derive_mode(page="seat", seat={"running": True, "runMode": "grab"}, arm={}), "grabbing")
        self.assertEqual(derive_mode(page="goods", seat={}, arm={"running": True, "fired": False}), "armed")
        self.assertEqual(derive_mode(page="goods", seat={}, arm={"running": False, "fired": True}), "entering")
        self.assertEqual(derive_mode(page="waiting", seat={}, arm={}), "entering")
        self.assertEqual(derive_mode(page="other", url="https://t/onestop/schedule", seat={}, arm={}), "on_schedule")
        self.assertEqual(derive_mode(page="seat", seat={}, arm={}), "on_seat")
        self.assertEqual(derive_mode(page="nol", seat={"haltedByUser": True}, arm={}), "halted")
        self.assertEqual(derive_mode(page="nol", seat={}, arm={"lastError": "x"}), "error")
        self.assertEqual(derive_mode(page="nol", seat={}, arm={}), "ready")
        self.assertEqual(derive_mode(page="other", url="https://nol.yanolja.com/ticket", seat={}, arm={}), "no_show")
        self.assertEqual(derive_mode(page="nol", seat={}, arm={}, bridge_live=False), "offline")

    def test_every_mode_has_guidance(self) -> None:
        for mode in MODES:
            for phase in (BEFORE_OPEN, OPEN, UNKNOWN):
                g = guidance(mode, phase, round_picked=True, auto_seats=True)
                self.assertTrue(g.banner and g.instruction, (mode, phase))


class GuidanceRules(unittest.TestCase):
    def test_before_open_arms_and_says_so(self) -> None:
        g = guidance("ready", BEFORE_OPEN, round_picked=True, auto_seats=True, open_text="09-05 14:00")
        self.assertEqual(g.primary, "arm")
        self.assertIn("오픈에 자동 진입", g.instruction)
        self.assertIn("오픈 예정", g.banner)
        self.assertNotIn("판매 중", g.banner + g.instruction)
        self.assertEqual(g.arm_reason, "")
        self.assertTrue(g.enter_reason)

    def test_open_enters_now_and_explains_arm(self) -> None:
        g = guidance("ready", OPEN, round_picked=True, auto_seats=True)
        self.assertEqual(g.primary, "enter")
        self.assertIn("지금 진입", g.instruction)
        self.assertIn("판매 중", g.banner)
        self.assertTrue(g.arm_reason)
        self.assertEqual(g.enter_reason, "")

    def test_round_first(self) -> None:
        g = guidance("ready", OPEN, round_picked=False, auto_seats=True)
        self.assertIn("회차", g.instruction)

    def test_working_modes_never_offer_entry(self) -> None:
        for mode in ("held", "grabbing", "watching", "armed", "entering", "on_schedule"):
            g = guidance(mode, OPEN, round_picked=True, auto_seats=True)
            self.assertNotIn(g.primary, ("arm", "enter"), mode)
            self.assertTrue(g.arm_reason and g.enter_reason, mode)

    def test_held_reads_as_held_not_waiting(self) -> None:
        g = guidance("held", OPEN, round_picked=True, auto_seats=True)
        self.assertIn("좌석 잡음", g.banner)
        self.assertNotIn("대기 중", g.instruction)


if __name__ == "__main__":
    unittest.main()


class CatchModeStability(unittest.TestCase):
    """On the seat map a running watch is "watching" and nothing else."""

    def test_leftover_arm_flags_never_read_as_armed_on_the_seat_map(self) -> None:
        seat = {"running": True, "runMode": "catch"}
        for arm in ({"running": True, "fired": False}, {"running": True, "fired": True}, {"fired": True}, {}):
            self.assertEqual(derive_mode(page="seat", seat=seat, arm=arm), "watching", arm)
        # Even with the watch idle, an arm on the seat map is a leftover, not a state.
        self.assertEqual(derive_mode(page="seat", seat={}, arm={"running": True, "fired": False}), "on_seat")

    def test_the_gap_between_two_catch_attempts_stays_watching(self) -> None:
        between = {"running": False, "runMode": "catch"}
        self.assertEqual(derive_mode(page="seat", seat=between, arm={}), "watching")
        self.assertEqual(derive_mode(page="seat", seat={**between, "haltedByUser": True}, arm={}), "halted")
        self.assertEqual(derive_mode(page="seat", seat={**between, "lastError": "x"}, arm={}), "error")

    def test_no_ghost_transitions_over_a_flapping_poll(self) -> None:
        seat_on = {"running": True, "runMode": "catch"}
        seat_gap = {"running": False, "runMode": "catch"}
        polls = [({"running": True}, seat_on), ({}, seat_gap), ({"running": True}, seat_on),
                 ({"fired": True}, seat_gap), ({}, seat_on)]
        modes = {derive_mode(page="seat", seat=s, arm=a) for a, s in polls}
        self.assertEqual(modes, {"watching"})

    def test_watching_guidance_is_frozen_text(self) -> None:
        g = guidance("watching", OPEN, round_picked=True, auto_seats=True)
        self.assertEqual(g.banner, "취켓팅 중 · 실시간 좌석 감시 중")
