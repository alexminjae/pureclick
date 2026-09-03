"""Whether the panel is still hearing from the 예매 창.

The panel could not answer this. `poll_context` — the 400ms loop that is the
only source of page_context (which decides which buttons enable), show_catalog
(the seat table) and autopilot_status (every status line) — ended in a bare
`except Exception: pass`, and nothing timestamped what it published. So a panel
running on twenty-minute-old state looked exactly like a working one, and every
failure reported this session presents the same way from the screen.
"""

from __future__ import annotations

import ast
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.seat import BRIDGE_STALE_SECONDS, EXIT_REASONS, bridge_line  # noqa: E402

NOW = 1_800_000_000.0
SEAT_PAGE = {"page": "seat", "goods_code": "26012217", "play_seq": "001"}


class BridgeLineTest(unittest.TestCase):
    def test_a_live_bridge_says_so_with_its_age(self) -> None:
        line = bridge_line({"seen_at": NOW - 0.3, "failures": 0}, SEAT_PAGE, {}, now=NOW)
        self.assertIn("연결됨", line)
        self.assertIn("0.3초", line)
        self.assertIn("좌석맵", line)
        self.assertIn("26012217", line, "which show, so the panel cannot disagree with the window")

    def test_a_frozen_bridge_is_not_mistaken_for_a_live_one(self) -> None:
        """The whole point. Before this they rendered identically."""
        line = bridge_line({"seen_at": NOW - 12, "failures": 30}, SEAT_PAGE, {}, now=NOW)
        self.assertIn("응답 없음", line)
        self.assertIn("12초", line)
        self.assertNotIn("연결됨", line)

    def test_the_threshold_tolerates_one_missed_tick(self) -> None:
        # The loop runs every 400ms, so a single slow read must not raise alarm.
        fresh = bridge_line({"seen_at": NOW - 1.0, "failures": 0}, SEAT_PAGE, {}, now=NOW)
        self.assertIn("연결됨", fresh)
        self.assertGreater(BRIDGE_STALE_SECONDS, 0.4 * 2)

    def test_a_reading_error_is_named_rather_than_swallowed(self) -> None:
        line = bridge_line(
            {"seen_at": NOW - 0.2, "failures": 3, "last_error": "JavascriptException: boom"},
            SEAT_PAGE, {}, now=NOW,
        )
        self.assertIn("읽기 실패", line)
        self.assertIn("3회", line)
        self.assertIn("JavascriptException", line)

    def test_nothing_heard_yet_is_not_reported_as_failure(self) -> None:
        # At launch the host has not written a health file yet.
        self.assertIn("대기 중", bridge_line({}, SEAT_PAGE, {}, now=NOW))

    def test_it_says_why_a_run_ended(self) -> None:
        # lastExit was published and never drawn: a run that stopped just
        # stopped, with nothing to say whether that was success or failure.
        line = bridge_line({"seen_at": NOW - 0.2, "failures": 0}, SEAT_PAGE,
                           {"lastExit": "reservedUserContinues"}, now=NOW)
        self.assertIn("좌석을 잡았습니다", line)

    def test_every_exit_the_page_can_emit_has_words(self) -> None:
        emitted = set()
        for src in (ROOT / "browser" / "nolsniper_autopilot.js",):
            text = src.read_text(encoding="utf-8")
            import re
            emitted |= set(re.findall(r'lastExit\s*=\s*"(\w+)"', text))
        self.assertTrue(emitted, "the scan found no exits; it is not working")
        missing = emitted - set(EXIT_REASONS)
        self.assertFalse(missing, f"these end a run with no explanation: {sorted(missing)}")

    def test_an_unknown_exit_degrades_quietly(self) -> None:
        line = bridge_line({"seen_at": NOW - 0.2, "failures": 0}, SEAT_PAGE,
                           {"lastExit": "somethingNew"}, now=NOW)
        self.assertIn("연결됨", line)
        self.assertNotIn("somethingNew", line, "a raw code is not an explanation")


class HostHealthTest(unittest.TestCase):
    """The host must keep reporting even when it cannot read the page."""

    SRC = (ROOT / "mac" / "browser_host.py").read_text(encoding="utf-8")

    def _poll(self) -> str:
        tree = ast.parse(self.SRC)
        fn = next(n for n in tree.body if getattr(n, "name", None) == "poll_context")
        return ast.get_source_segment(self.SRC, fn) or ""

    def test_a_failed_read_is_recorded_not_discarded(self) -> None:
        body = self._poll()
        self.assertNotIn("except Exception:\n            pass", body,
                         "the loop must survive a failure without hiding it")
        self.assertIn("failures += 1", body)
        self.assertIn("last_error", body)

    def test_health_is_written_every_tick_even_when_reading_fails(self) -> None:
        body = self._poll()
        write = body.index("write_bridge_health(")
        wait = body.rindex("stop_event.wait(")
        self.assertLess(write, wait, "written before the loop sleeps")
        # Outside the try, or a failing read would stop the reporting too.
        self.assertNotIn("        try:", body[write:wait])

    def test_health_is_not_merged_into_the_shared_state(self) -> None:
        # It changes every tick and the shared file is ~180KB; merging there
        # would rewrite it 2.5 times a second and hold its lock from the panel.
        self.assertIn("HEALTH_PATH", self.SRC)
        body = self._poll()
        self.assertNotIn('merge_if_changed(STATE_PATH, "bridge_health"', body)


class ClockTickTest(unittest.TestCase):
    """One bad reading must not stop the clock for the session."""

    SRC = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")

    def test_the_tick_reschedules_outside_its_try(self) -> None:
        tree = ast.parse(self.SRC)
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
        fn = next(n for n in cls.body if getattr(n, "name", None) == "_tick_server_time")
        body = ast.get_source_segment(self.SRC, fn) or ""
        self.assertIn("try:", body, "it rescheduled itself with no guard at all")
        self.assertIn("finally:", body,
                      "the reschedule must survive the exception that killed it")
        self.assertLess(body.index("except"), body.rindex("self.after("),
                        "reschedule after the handler, not inside the try")


if __name__ == "__main__":
    unittest.main()
