"""Recovery journeys through the panel — Journey 4 in particular.

    취켓팅 (catch mode) → 전부 정지 (stopped by the user) → 대기 시작 again
    (re-armed) → the next entry starts clean.

Every hop is a place the panel used to keep something it should have dropped:
the countdown to an arm that was just cancelled, a "watching" that hid the
clock after the watch had stopped, the leftover arm on the seat map that flapped
the banner — or, going the other way, an arm the user had *just* made being
mistaken for a leftover and thrown away. The tests below walk the journey and
check what each step leaves for the next.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from core.clock import KST, parse_target_time  # noqa: E402
from core.mode import MODE_LABELS, derive_mode, guidance  # noqa: E402
from core.mode import sale_phase  # noqa: E402
from test_panel_arm_gate import FUTURE, PAST, _Panel  # noqa: E402

PANEL_SOURCE = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")


def _load(*names: str, extra: dict | None = None) -> dict:
    """Lift panel methods out of the class without importing tkinter."""
    tree = ast.parse(PANEL_SOURCE)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
    body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in body} == set(names), set(names) - {n.name for n in body}
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"getattr": getattr, "parse_target_time": parse_target_time, "KST": KST,
                "FAINT": "faint", "AMBER": "amber"}
    ns.update(extra or {})
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns


class _Var:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value) -> None:
        self.value = str(value)

    def get(self) -> str:
        return self.value


class _Bridge:
    """Records what the panel pushed at the page."""

    def __init__(self) -> None:
        self.pushes: list[dict] = []
        self.commands: list[tuple[str, dict]] = []

    def push(self, **kwargs) -> None:
        self.pushes.append(kwargs)

    def send_command(self, name: str, **kwargs) -> None:
        self.commands.append((name, kwargs))

    def clears(self) -> int:
        return sum(1 for p in self.pushes if p.get("clear_arm"))


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def server_time_unix(self) -> float:
        return self.now


class _JourneyPanel:
    """The slice of the panel the journey touches, wired to lifted methods."""

    NS = _load("_tidy_arm_on_seat", "stop_all", "_tick_countdown", "_countdown_text",
               "_show_countdown")

    def __init__(self) -> None:
        self.browser = _Bridge()
        self._last_arm_cfg: dict = {}
        self._last_arm_status: dict = {}
        self._armed_target_unix: float | None = None
        self._armed_is_test = False
        self._mode = "no_show"
        self.flashes: list[tuple] = []
        self.presses: list[str] = []
        self._trigger_on = True
        self.countdown = _Var()
        self.countdown_label = object()
        self.target_date = _Var("2099-01-01")
        self.target_time = _Var("20:00:00")
        self.clock = _Clock(datetime(2098, 12, 31, 20, 0, tzinfo=KST).timestamp())
        for name in ("_tidy_arm_on_seat", "stop_all", "_tick_countdown", "_show_countdown"):
            setattr(self, name, self.NS[name].__get__(self))
        text = self.NS["_countdown_text"]  # a @staticmethod in the class body
        self._countdown_text = getattr(text, "__func__", text)

    # collaborators stop_all leans on
    def _stop_trigger_worker(self) -> None:
        self._trigger_on = False

    def _remember_press(self, label: str) -> None:
        self.presses.append(label)

    def _flash(self, *args, **kwargs) -> None:
        self.flashes.append(args)

    def _entry_offset_ms(self) -> int:
        return 0

    # what the poll hands the tidy-up each tick
    def poll(self, page: str, *, arm_cfg: dict | None = None, arm_running: bool = False) -> bool:
        self._last_arm_cfg = dict(arm_cfg or {})
        self._last_arm_status = {"running": arm_running}
        return self._tidy_arm_on_seat({"page": page})


ARM = {"goods_code": "26099999", "target_server_unix": 4_102_444_800.0, "enabled": True}


class LeftoverArmIsClearedOncePerVisit(unittest.TestCase):
    """The seat-map tidy-up: one clear per landing, never of a fresh arm."""

    def test_landing_on_the_seat_map_clears_the_arm_that_got_us_there(self) -> None:
        panel = _JourneyPanel()
        panel._armed_target_unix = ARM["target_server_unix"]
        self.assertFalse(panel.poll("goods", arm_cfg=ARM, arm_running=True), "counting down: not a leftover")
        self.assertEqual(panel.browser.clears(), 0)
        self.assertTrue(panel.poll("seat", arm_cfg=ARM, arm_running=True), "the entry is over")
        self.assertEqual(panel.browser.clears(), 1)
        self.assertIsNone(panel._armed_target_unix, "and the countdown stops claiming it is next")

    def test_the_same_leftover_is_not_cleared_again_every_poll(self) -> None:
        panel = _JourneyPanel()
        panel.poll("seat", arm_cfg=ARM)
        for _ in range(5):  # the state file still carries the arm for a few polls
            self.assertFalse(panel.poll("seat", arm_cfg=ARM, arm_running=True))
        self.assertEqual(panel.browser.clears(), 1)

    def test_a_page_still_reporting_running_is_a_leftover_too(self) -> None:
        panel = _JourneyPanel()
        self.assertTrue(panel.poll("seat", arm_cfg=None, arm_running=True))

    def test_nothing_to_clear_is_not_a_clear(self) -> None:
        panel = _JourneyPanel()
        self.assertFalse(panel.poll("seat"))
        self.assertEqual(panel.browser.clears(), 0)
        self.assertFalse(getattr(panel, "_arm_cleared_on_seat", False),
                         "an empty seat map must not spend the once-per-visit latch")


class Journey4(unittest.TestCase):
    """취켓팅 → 전부 정지 → 대기 시작 → clean start, as the panel sees it."""

    def test_the_stop_drops_the_countdown_and_the_trigger(self) -> None:
        panel = _JourneyPanel()
        panel._armed_target_unix = ARM["target_server_unix"]
        panel._armed_is_test = True
        panel.stop_all()
        self.assertEqual(panel.browser.commands, [("stop_all", {"clear_arm": True})])
        self.assertIsNone(panel._armed_target_unix, "nothing will fire, so nothing is counted to")
        self.assertFalse(panel._armed_is_test)
        self.assertFalse(panel._trigger_on, "the whole-venue remain watcher stops with the watch")
        self.assertEqual(panel.presses, ["전부 정지"])
        self.assertEqual(panel.flashes[0][1], "정지 요청됨")

    def test_a_re_arm_after_the_stop_is_carried_through_to_the_next_landing(self) -> None:
        panel = _JourneyPanel()
        # 1. The first entry lands on the seat map; its arm is tidied away.
        panel.poll("goods", arm_cfg=ARM, arm_running=True)
        panel.poll("seat", arm_cfg=ARM, arm_running=True)
        self.assertEqual(panel.browser.clears(), 1)
        # 2. 취켓팅 runs, then 전부 정지 — still on the seat map.
        panel.stop_all()
        for _ in range(3):
            panel.poll("seat")
        # 3. 대기 시작: the panel parks the 예매 창 on the goods page and publishes
        #    a new arm. Seen from the goods page it is a live arm, not a leftover.
        panel._armed_target_unix = ARM["target_server_unix"]
        self.assertFalse(panel.poll("goods", arm_cfg=ARM, arm_running=True))
        self.assertFalse(panel.poll("goods", arm_cfg=ARM, arm_running=True))
        self.assertEqual(panel.browser.clears(), 1, "the fresh arm is never thrown away")
        self.assertEqual(panel._armed_target_unix, ARM["target_server_unix"], "the countdown counts to it")
        # 4. It fires and lands on the seat map again: cleared once, like the first.
        self.assertTrue(panel.poll("seat", arm_cfg=ARM, arm_running=True))
        self.assertFalse(panel.poll("seat", arm_cfg=ARM, arm_running=True))
        self.assertEqual(panel.browser.clears(), 2)
        self.assertIsNone(panel._armed_target_unix)

    def test_a_stale_seat_context_right_after_the_re_arm_cannot_eat_it(self) -> None:
        """The poll may still say "seat" for a tick after 대기 시작 is pressed."""
        panel = _JourneyPanel()
        panel.poll("seat", arm_cfg=ARM, arm_running=True)   # first landing, cleared
        panel.stop_all()
        panel.poll("seat")                                    # stopped, still there
        panel._armed_target_unix = ARM["target_server_unix"]  # re-armed
        self.assertFalse(panel.poll("seat", arm_cfg=ARM, arm_running=True),
                         "the once-per-visit latch is still spent: the new arm survives the stale tick")
        self.assertEqual(panel.browser.clears(), 1)
        self.assertEqual(panel._armed_target_unix, ARM["target_server_unix"])

    def test_the_countdown_hides_under_the_watch_and_comes_back_after_the_stop(self) -> None:
        panel = _JourneyPanel()
        panel._mode = "watching"
        panel._tick_countdown()
        self.assertEqual(panel.countdown.get(), "", "nothing to count to while the watch runs")
        panel._mode = "halted"                    # 전부 정지
        panel._tick_countdown()
        self.assertEqual(panel.countdown.get(), "1일 00:00:00", "back to 티켓 오픈 once stopped")
        panel._mode = "armed"                     # 대기 시작, a rehearsal 90s out
        panel._armed_target_unix = panel.clock.now + 90
        panel._armed_is_test = True
        panel._tick_countdown()
        self.assertEqual(panel.countdown.get(), "테스트 00:01:30", "counting to the new arm, not the old one")

    def test_the_modes_along_the_journey_and_the_buttons_at_each(self) -> None:
        seat_watch = {"running": True, "runMode": "catch"}
        seat_stopped = {"running": False, "runMode": "catch", "haltedByUser": True}
        arm_live = {"running": True, "fired": False}
        arm_fired = {"running": False, "fired": True}
        steps = [
            ("seat", seat_watch, {}, "watching"),
            ("seat", seat_stopped, {}, "halted"),
            ("seat", seat_stopped, arm_live, "halted"),      # a stale arm flag on the map is noise
            ("goods", {}, arm_live, "armed"),                # parked and re-armed
            ("goods", {}, arm_fired, "entering"),
            ("waiting", {}, arm_fired, "entering"),
            ("seat", {}, arm_fired, "on_seat"),              # clean landing: no halt, no error
            ("seat", seat_watch, arm_fired, "watching"),
        ]
        for page, seat, arm, want in steps:
            self.assertEqual(derive_mode(page=page, seat=seat, arm=arm), want, (page, seat, arm))
            self.assertIn(want, MODE_LABELS)

        # And the buttons: stopped on the seat map, both ways forward are open.
        panel = _Panel({"ticket_open_kst": FUTURE}, page="seat", seat=seat_stopped)
        self.assertEqual(panel.mode_text.get(), MODE_LABELS["halted"])
        self.assertTrue(panel.btn_catch.enabled, "감시 시작 again")
        self.assertTrue(panel.btn_arm.enabled, "or 대기 시작 for the next round")
        # Re-armed: the stop is the action.
        told = guidance("armed", sale_phase(datetime.now(KST) + timedelta(days=1), datetime.now(KST)),
                        round_picked=True, auto_seats=True, open_text="01-01 20:00:00")
        self.assertEqual(told.primary, "stop")
        # The clean landing is a seat map with the watch on offer, no error text.
        panel = _Panel({"ticket_open_kst": PAST}, page="seat", seat={})
        self.assertEqual(panel.mode_text.get(), MODE_LABELS["on_seat"])
        self.assertTrue(panel.btn_catch.enabled)
        self.assertNotIn("오류", panel.guidance.get())


JOURNEY5 = ROOT / "tests" / "journey_hold_lifecycle.mjs"


@unittest.skipUnless(shutil.which("node"), "node is needed to drive the autopilot")
class Journey5CatchHoldLetGo(unittest.TestCase):
    """취켓팅 catches → holds → the user lets go → PAUSED until 감시 시작.

    The page's side is played by tests/journey_hold_lifecycle.mjs against the
    real focus poller and press sequence; this asserts what it reports and
    what the panel makes of the state it leaves. Also covered there: a real
    pointer press on a seat yields the watch, and step=price ends every poller.
    """

    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run(
            ["node", str(JOURNEY5), "--json"], cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        lines = [line for line in done.stdout.strip().splitlines() if line.startswith("{")]
        # A failed check still prints its report (exit 1); only a harness that
        # crashed before reporting is an error here. The failed check itself is
        # named by test_every_check_in_the_journey_holds.
        if not lines:
            raise AssertionError(f"journey harness crashed:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
        cls.report = json.loads(lines[-1])
        cls.by_name = {c["name"]: c for c in cls.report["checks"]}

    def _passed(self, *names: str) -> None:
        for name in names:
            with self.subTest(check=name):
                self.assertIn(name, self.by_name, "the harness no longer makes this check")
                got = self.by_name[name]
                self.assertTrue(got["ok"], f"{name}: {got.get('detail', '')}")

    def test_every_check_in_the_journey_holds(self) -> None:
        failed = [c for c in self.report["checks"] if not c["ok"]]
        self.assertEqual(failed, [], "\n".join(f"{c['name']}: {c.get('detail', '')}" for c in failed))

    def test_the_catch_pauses_the_poller_and_says_so(self) -> None:
        self._passed("no seatStatus leaves while a seat is held", "the overlay says 좌석 선점 완료",
                     "the hold guard is watching")

    def test_letting_go_leaves_a_clean_pause_and_nothing_is_snatched(self) -> None:
        self._passed("paused: not running", "paused: the stale lock is dropped", "paused: sticky until 감시 시작",
                     "paused: the poller is off", "no press while paused", "no request while paused")
        self.assertLess(self.report["pause"]["noticedMs"], 700, "the deselect is noticed within two guard ticks")

    def test_the_users_hand_and_the_price_step_end_the_sweep(self) -> None:
        self._passed("an untrusted (our own) press does not pause", "yielded: no request follows",
                     "yielded: the user's selection is untouched (no clear, no release)",
                     "step=price: no request after the URL changed")

    def test_the_stream_is_gapless_under_the_cap(self) -> None:
        stream = self.report["stream"]
        self.assertLessEqual(stream["requestsPerSec"], stream["cap"] + 2)
        self.assertGreaterEqual(stream["requestsPerSec"], 45)
        self.assertLess(stream["gapMaxMs"], 2 * (1000 / stream["cap"]) + 10)

    def test_the_panel_reads_the_pause_as_halted_with_the_watch_on_offer(self) -> None:
        paused = {"running": False, "runMode": "catch", "locked": False, "haltedByUser": True,
                  "pauseReason": "userDeselect", "pageSelected": 0}
        self.assertEqual(derive_mode(page="seat", seat=paused, arm={}), "halted")
        panel = _Panel({"ticket_open_kst": FUTURE}, page="seat", seat=paused)
        self.assertEqual(panel.mode_text.get(), MODE_LABELS["halted"])
        self.assertTrue(panel.btn_catch.enabled, "감시 시작 is the way back")
        # Touched by hand while a seat is now selected by the user: held, not watching.
        touched = {"running": False, "runMode": "catch", "locked": False, "haltedByUser": True,
                   "pauseReason": "humanTouch", "pageSelected": 1}
        self.assertEqual(derive_mode(page="seat", seat=touched, arm={}), "halted")


if __name__ == "__main__":
    unittest.main()
