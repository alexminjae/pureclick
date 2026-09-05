"""pywebview's own evaluate_js has no timeout, and that cost a whole poller.

Reported live: the 예매 창 rendered once, then the panel's bridge line never
moved past "예매 창 연결 대기 중…" again. No crash.log, even after that file
was added specifically to catch exactly this kind of failure — because nothing
raised. pywebview's edgechromium.evaluate_js does a bare `semaphore.acquire()`
with no timeout: if WebView2's own message loop never processes the queued
call, for any reason on any machine, the calling thread blocks forever. Silent,
by construction — there is nothing to catch.

poll_context is the only source of the bridge health report the panel reads.
A single stuck evaluate_js call inside it does not just cost one tick; it costs
every tick for the rest of the session, because the thread never returns to the
top of its own loop.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "mac") not in sys.path:
    sys.path.insert(0, str(ROOT / "mac"))

import browser_host  # noqa: E402


class CallWithTimeoutTests(unittest.TestCase):
    def test_a_fast_call_returns_its_result(self) -> None:
        self.assertEqual(browser_host._call_with_timeout(lambda: 42, timeout=1.0, label="t"), 42)

    def test_a_call_that_never_returns_times_out_promptly(self) -> None:
        """The bug itself: pywebview's evaluate_js can block forever. This
        must give up close to the requested timeout, not eventually."""
        stuck = threading.Event()  # never set — simulates the hang exactly
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            browser_host._call_with_timeout(lambda: stuck.wait(), timeout=0.1, label="hang")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, f"must give up promptly, took {elapsed:.2f}s")

    def test_a_real_exception_still_propagates_as_itself(self) -> None:
        def boom():
            raise ValueError("real failure")

        with self.assertRaises(ValueError):
            browser_host._call_with_timeout(boom, timeout=1.0, label="err")

    def test_the_timeout_value_is_readable_in_the_message(self) -> None:
        """0.2s rendered as "0s" would misname the very thing being measured."""
        try:
            browser_host._call_with_timeout(lambda: threading.Event().wait(), timeout=0.05, label="t")
        except TimeoutError as exc:
            self.assertIn("0.05", str(exc))
        else:
            self.fail("must raise")


class PollContextSurvivesAHungCallTests(unittest.TestCase):
    """The actual reported bug, reproduced: run poll_context for real, against
    a window whose evaluate_js hangs forever, and prove the loop keeps
    reporting instead of the thread just disappearing.
    """

    def setUp(self) -> None:
        self._original_timeout = browser_host._POLL_CONTEXT_TIMEOUT
        browser_host._POLL_CONTEXT_TIMEOUT = 0.05  # keep the test fast
        self._original_write = browser_host.write_bridge_health
        self.reports: list[dict] = []
        browser_host.write_bridge_health = lambda health: self.reports.append(health)

    def tearDown(self) -> None:
        browser_host._POLL_CONTEXT_TIMEOUT = self._original_timeout
        browser_host.write_bridge_health = self._original_write

    def test_poll_context_keeps_reporting_through_a_permanent_hang(self) -> None:
        class HangingWindow:
            def evaluate_js(self, _script):
                threading.Event().wait()  # never returns — the bug itself

            def get_current_url(self):
                return "https://nol.yanolja.com/ticket"

        stop_event = threading.Event()
        t = threading.Thread(
            target=browser_host.poll_context, args=(HangingWindow(), stop_event), daemon=True
        )
        t.start()
        try:
            # Several ticks' worth of time. If the fix regresses to no timeout,
            # exactly one report ever lands (or none) and this fails.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and len(self.reports) < 3:
                time.sleep(0.05)
        finally:
            stop_event.set()
            t.join(timeout=2.0)

        self.assertFalse(t.is_alive(), "the poller thread must actually exit when asked to stop")
        self.assertGreaterEqual(
            len(self.reports), 3,
            "must keep reporting on every tick despite the permanent hang, not just once",
        )
        last = self.reports[-1]
        self.assertGreater(last["failures"], 0, "a hang is a failure, and must be counted as one")
        self.assertIn("poll_context", last["last_error"],
                      "must name what actually failed, not a generic message")
        # seen_at is written every tick regardless of success — that is what
        # lets the panel tell "alive but failing" apart from "dead".
        self.assertGreater(last["seen_at"], 0)


class HungHostRestartRule(unittest.TestCase):
    """A page whose script never idles gets a fresh 예매 창 — never over a held
    seat, never for a blocked main thread, never before 45s."""

    def test_restarts_a_hung_page_once_it_has_lasted_and_nothing_is_held(self) -> None:
        self.assertTrue(browser_host.should_restart_hung_host({"running": True, "pageSelected": 0}, 46.0, True))
        self.assertTrue(browser_host.should_restart_hung_host({}, 46.0, None), "no snapshot means no known hold")

    def test_too_early_is_not_a_hang_worth_a_relaunch(self) -> None:
        self.assertFalse(browser_host.should_restart_hung_host({}, 8.0, True))

    def test_never_over_a_held_seat(self) -> None:
        for seat in ({"locked": True}, {"confirmStarted": True}, {"awaitingPayment": True}, {"pageSelected": 1}):
            self.assertFalse(browser_host.should_restart_hung_host(seat, 120.0, True), seat)

    def test_a_blocked_main_thread_is_a_dialog_not_a_hang(self) -> None:
        # Restarting would not clear it and would throw the session away.
        self.assertFalse(browser_host.should_restart_hung_host({}, 120.0, False))


class HeartbeatKeepsLivenessTruthful(unittest.TestCase):
    """seen_at means 'the host is alive', not 'the last read finished' — a slow
    read must not read as a closed window (예매 창 응답 없음)."""

    def setUp(self) -> None:
        self._orig = browser_host.write_bridge_health
        self.written: list[dict] = []
        browser_host.write_bridge_health = lambda h: self.written.append(dict(h))
        self._orig_live = dict(browser_host._LIVE_HEALTH)

    def tearDown(self) -> None:
        browser_host.write_bridge_health = self._orig
        browser_host._LIVE_HEALTH.clear()
        browser_host._LIVE_HEALTH.update(self._orig_live)

    def test_heartbeat_bumps_seen_at_while_the_read_facts_stay_put(self) -> None:
        # A read succeeded 10s ago and nothing has completed since (a slow tick).
        stale_ok = time.time() - 10
        browser_host._LIVE_HEALTH.update({"last_ok": stale_ok, "failures": 0, "last_error": ""})
        stop = threading.Event()
        t = threading.Thread(target=browser_host.heartbeat, args=(stop, 0.02), daemon=True)
        t.start()
        time.sleep(0.1)
        stop.set(); t.join(timeout=1.0)
        self.assertGreaterEqual(len(self.written), 3, "the heartbeat keeps writing on its own cadence")
        last = self.written[-1]
        # seen_at is fresh (the host is alive) even though the last read is old.
        self.assertLess(time.time() - last["seen_at"], 1.0, "seen_at is current — the window is not 'lost'")
        self.assertEqual(last["last_ok"], stale_ok, "the real read-success time is preserved for the panel")

    def test_a_dead_heartbeat_lets_seen_at_age_into_lost(self) -> None:
        from core.seat import BRIDGE_STALE_SECONDS, bridge_status
        # The heartbeat stopped (process gone): seen_at frozen in the past.
        frozen = {"seen_at": time.time() - (BRIDGE_STALE_SECONDS + 2), "failures": 0}
        state, _ = bridge_status(frozen)
        self.assertEqual(state, "lost")


class HealthWriteIsAtomic(unittest.TestCase):
    def test_write_uses_replace_not_a_bare_write(self) -> None:
        import inspect
        src = inspect.getsource(browser_host.write_bridge_health)
        self.assertIn("os.replace", src, "a torn read of the health file reads as a dead bridge")
