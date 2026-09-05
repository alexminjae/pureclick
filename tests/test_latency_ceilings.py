"""Hard latency ceilings and hot-path invariants, guarded statically.

These are the promises the engine makes about millisecond behaviour. They are
asserted against the source (not the network) so a regression that reintroduces
a timer in a hot path, or loosens a cadence, fails here rather than in a drop.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "browser" / "nolsniper_autopilot.js").read_text(encoding="utf-8")
ARM = (ROOT / "core" / "arm.py").read_text(encoding="utf-8")


def _const(name: str, text: str = JS) -> int:
    m = re.search(rf"const {name} = (\d+);", text) or re.search(rf"{name} = (\d+)\b", text)
    assert m, f"{name} not found"
    return int(m.group(1))


def _slice(start_marker: str, end_marker: str) -> str:
    a = JS.index(start_marker)
    b = JS.index(end_marker, a)
    return JS[a:b]


class CatchCadenceCeilings(unittest.TestCase):
    def test_focus_cadence_is_30ms_or_faster(self) -> None:
        self.assertLessEqual(_const("CATCH_FOCUS_POLL_MS"), 30)
        self.assertLessEqual(_const("CATCH_FAST_POLL_MS"), 30)

    def test_request_rate_is_capped(self) -> None:
        # <=60 req/s keeps us under the gateway abuse block while ~20ms period.
        self.assertLessEqual(_const("CATCH_MAX_REQUESTS_PER_SEC"), 60)
        self.assertGreaterEqual(_const("FOCUS_WORKERS"), 2)

    def test_conflict_snap_tries_several_seats_with_no_sleep(self) -> None:
        seq = _slice("async function pressSequence(", "function startFocusPoller(")
        self.assertGreaterEqual(_const("PRESS_SNAP_MAX"), 2)
        # markSeatTaken then continue to the next seat — never a sleep between.
        self.assertRegex(seq, r"markSeatTaken\(seat\.seatInfoId\);")
        self.assertNotIn("await sleep(", seq)


class HotPathsAreTimerFree(unittest.TestCase):
    def test_focus_poller_send_path_uses_no_timers(self) -> None:
        # The send path (fetch → diff → press) must never sit behind a timer;
        # a background window would clamp it. The one sleep is on the explicit
        # rate-limited idle branch, which is meant to wait.
        send = _slice("if (focusPollerCanSend()) {", "} else {")
        for banned in ("setTimeout", "setInterval", "requestIdleCallback", "await sleep("):
            self.assertNotIn(banned, send, banned)
        # The poller's own bookkeeping helpers never use timers either.
        helpers = _slice("function focusPollerCanSend(", "async function focusWorker(")
        for banned in ("setTimeout", "setInterval", "requestIdleCallback"):
            self.assertNotIn(banned, helpers, banned)

    def test_press_sequence_uses_no_timers(self) -> None:
        seq = _slice("async function pressSequence(", "function startFocusPoller(")
        for banned in ("setTimeout", "setInterval", "requestIdleCallback"):
            self.assertNotIn(banned, seq, banned)

    def test_short_waits_use_a_messagechannel_not_a_timer(self) -> None:
        y = _slice("function yieldFast() {", "// Resolve the moment")
        self.assertIn("postMessage", y)
        self.assertNotIn("setTimeout", y)


class EntryLeadCeilings(unittest.TestCase):
    def test_default_lead_is_150ms(self) -> None:
        self.assertEqual(_const("DEFAULT_ENTRY_LEAD_MS", ARM), 150)

    def test_lead_is_capped(self) -> None:
        self.assertLessEqual(_const("MAX_ENTRY_LEAD_MS", ARM), 600)


class ScheduleStepIsPrompt(unittest.TestCase):
    def test_no_15s_idle_budget_before_the_advance(self) -> None:
        fn = _slice("async function chooseRoundOnSchedule(", "function shownRoundOnSeatPage(")
        # The date/time/next presses are native and verified with short, bounded
        # windows — never SCHEDULE_STEP_TIMEOUT_MS idling on the whole step.
        self.assertIn("nativePress(cell)", fn)
        self.assertIn("nativePress(next)", fn)
        self.assertNotIn("SCHEDULE_STEP_TIMEOUT_MS", fn)

    def test_the_round_is_reconciled_by_seq_not_a_stale_date(self) -> None:
        boot = _slice("if (onSchedulePage()) {", "if ((isNolProductPage() || isGoodsPage())")
        self.assertIn("roundBySeq(", boot)


if __name__ == "__main__":
    unittest.main()


class SejongFloorGateInvariant(unittest.TestCase):
    """floorRank must derive a floor even when the seat carries only a block key
    — otherwise the 1층>2층 gate is a no-op on sketch-shaped data (측정:
    세종문화회관 3022-seat sketch has {k,x,y} and no floor field)."""

    def test_floorRank_has_a_block_key_fallback(self) -> None:
        fn = _slice("function floorRank(seat) {", "function rankCandidates(")
        self.assertIn("blockKey", fn, "floorRank must fall back to the block key")
        self.assertIn(r"/:(\d)\d\d", fn, "and read the floor digit from it")

    def test_the_floor_gate_sits_between_grade_and_distance(self) -> None:
        sort = _slice("const sorted = ranked.sort(", "recordSeatOrder(sorted")
        self.assertLess(sort.index("_rank"), sort.index("_floor"), "grade before floor")
        self.assertLess(sort.index("_floor"), sort.index("_posA"), "floor before distance")


class CatchWorkerYieldsToRenderer(unittest.TestCase):
    """focusWorker must yield a macrotask every iteration or two workers spin on
    microtasks and Chrome shows 페이지 응답 없음 (measured with fast-failing
    fetches). It must also floor the poll period so the yield actually spaces
    the loop, while never adding delay to a fetch that already took ~20ms."""

    def test_every_iteration_yields_a_real_macrotask(self) -> None:
        fn = _slice("async function focusWorker(", "focusPoller.inFlight = Math.max(0, focusPoller.inFlight);")
        # A MessageChannel hop after every send — unclamped, hands the renderer
        # a frame — so a fast-failing fetch cannot spin two workers into a freeze.
        self.assertGreaterEqual(fn.count("await yieldFast();"), 2, "yield on send and idle paths")
        self.assertNotIn("if (!freed.length) continue;", fn, "no bare continue without a yield")
        # No setTimeout floor on the send path (a background window clamps it to
        # ~1s and would collapse the rate); the fetch RTT paces sending instead.
        send = fn[: fn.index("} else {")]
        self.assertNotIn("await sleep(", send, "the send path is timer-free; the fetch paces it")

    def test_the_period_floor_is_about_20ms(self) -> None:
        self.assertLessEqual(_const("FOCUS_YIELD_MS"), 20)
        self.assertGreaterEqual(_const("FOCUS_YIELD_MS"), 10)
