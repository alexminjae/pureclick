"""The one-request "did anything free?" trigger.

Detection today costs one seatStatus request per two blocks — measured 58ms
each, hard-capped at two blockKeys (n>=3 is HTTP 400) — so a 34-block venue
spends 17 requests and about 4.4 seconds to answer a question the whole-venue
remaining count answers in one request and 132ms.

The two sources were measured to agree: round 097 read 202 and 202; round 096
sat two apart and stayed two apart across eleven samples. A standing offset is
harmless for a trigger, which cares only that the number moved.

These tests are mostly about the ways it must NOT fire, because every one of
them was seen while establishing that.
"""

from __future__ import annotations

import unittest

from pureclick_watch_trigger import TriggerState, next_trigger_state


class TriggerTest(unittest.TestCase):
    def test_a_first_reading_is_a_baseline_not_an_event(self) -> None:
        state = next_trigger_state(None, 207, now=100.0)
        self.assertTrue(state.usable)
        self.assertEqual(state.changed_at, 0.0, "nothing has changed yet; we had not looked")

    def test_an_unchanged_count_does_not_move_the_clock(self) -> None:
        first = next_trigger_state(None, 207, now=100.0)
        second = next_trigger_state(first, 207, now=101.0)
        self.assertEqual(second.changed_at, first.changed_at)
        self.assertEqual(second.seen_at, 101.0, "but we did look")

    def test_a_seat_freeing_moves_the_clock(self) -> None:
        first = next_trigger_state(None, 207, now=100.0)
        second = next_trigger_state(first, 208, now=102.0)
        self.assertEqual(second.changed_at, 102.0)
        self.assertTrue(second.usable)

    def test_a_seat_selling_also_counts_as_a_change(self) -> None:
        # The count moving either way means the venue moved, and the sweep is
        # what decides whether anything is catchable. Firing on a sale costs one
        # extra sweep; missing a cancellation costs the ticket.
        first = next_trigger_state(None, 207, now=100.0)
        second = next_trigger_state(first, 206, now=102.0)
        self.assertEqual(second.changed_at, 102.0)

    def test_zero_is_not_sold_out(self) -> None:
        # Measured: rounds 094 and 098 report remainCnt 0 while their maps hold
        # 662 and 604 seats — they are simply not on sale. Reading that as
        # "nothing free" would make the trigger go quiet on a live venue.
        first = next_trigger_state(None, 207, now=100.0)
        state = next_trigger_state(first, 0, now=101.0)
        self.assertFalse(state.usable, "the watch must fall back to sweeping")
        self.assertIn("판매 중이 아닌", state.note)

    def test_a_failed_reading_falls_back_rather_than_going_quiet(self) -> None:
        first = next_trigger_state(None, 207, now=100.0)
        state = next_trigger_state(first, None, now=101.0)
        self.assertFalse(state.usable)
        self.assertEqual(state.total, 207, "the last known count is kept")

    def test_a_show_that_hides_remains_never_uses_the_trigger(self) -> None:
        state = next_trigger_state(None, 0, hide_remain=True, now=100.0)
        self.assertFalse(state.usable)
        self.assertIn("공개하지 않습니다", state.note)

    def test_recovering_from_a_failure_rebaselines_before_firing(self) -> None:
        # Coming back after a gap, the count may differ for reasons we never
        # saw. Treating that first reading as a change would fire a sweep on
        # every recovery.
        good = next_trigger_state(None, 207, now=100.0)
        broken = next_trigger_state(good, None, now=101.0)
        back = next_trigger_state(broken, 190, now=102.0)
        self.assertTrue(back.usable)
        self.assertEqual(back.changed_at, good.changed_at, "a rebaseline is not an event")
        after = next_trigger_state(back, 191, now=103.0)
        self.assertEqual(after.changed_at, 103.0, "and the next real change does fire")

    def test_it_survives_the_bridge(self) -> None:
        state = next_trigger_state(None, 207, now=100.0)
        mapping = state.to_mapping()
        self.assertEqual(
            set(mapping), {"total", "changed_at", "seen_at", "usable", "note"}
        )
        self.assertIsInstance(mapping["total"], int)


if __name__ == "__main__":
    unittest.main()


class TriggerBridgeTest(unittest.TestCase):
    """The trigger has to survive the trip from the panel to the page.

    It crosses a JSON state file and then a JS string, and it is the only part
    of the watch that does — the page cannot fetch the feed itself because
    api-ticketfront.interpark.com sends no Access-Control-Allow-Origin.
    """

    def test_it_round_trips_through_the_state_file(self) -> None:
        import json
        import sys
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mac"))
        from browser_bridge import BrowserBridge, load_state

        with tempfile.TemporaryDirectory() as tmp:
            bridge = BrowserBridge(Path(tmp))
            state = next_trigger_state(None, 207, now=100.0)
            bridge.push_trigger(state.to_mapping())
            written = load_state(bridge.state_path).get("watch_trigger")
            self.assertEqual(written["total"], 207)
            self.assertTrue(written["usable"])

            # Written only when it changes: a quiet venue must not rewrite the
            # file twice a second while a seat race is in progress.
            same = next_trigger_state(state, 207, now=100.0)
            self.assertFalse(
                bridge.push_trigger(same.to_mapping()) or
                load_state(bridge.state_path)["watch_trigger"] != written,
                "an unchanged reading should not rewrite the file",
            )

            # And it must survive being embedded in the script the host runs.
            self.assertIn('"total": 207', json.dumps(written, indent=None) or "")

    def test_the_host_embeds_it_as_data_not_as_a_command(self) -> None:
        # A command reloads the autopilot or restarts a run. The trigger fires
        # several times a minute; if it were a command it would restart the
        # watch it is supposed to be helping.
        from pathlib import Path

        host = (Path(__file__).resolve().parent.parent / "mac" / "browser_host.py").read_text()
        self.assertIn("setWatchTrigger", host)
        at_trigger = host.index("watch_trigger")
        at_command = host.index('command = state.get("command")')
        self.assertLess(at_trigger, at_command, "the trigger is applied outside the command path")
