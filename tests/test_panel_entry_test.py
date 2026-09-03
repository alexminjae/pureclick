"""The rehearsal readout, and the 회차 it names.

오픈 대기 is one instant that either works or is lost, and until now it could
only be found out in real time. The scheduler was already capable of firing at
any moment; what was missing was a way to ask it to, and a way to see what
happened — `arm` has always been published beside the seat status and never
read, the same computed-but-invisible pattern as the guidance line.

The 상품/회차 line is the part that earns its place: block keys embed the round,
so a run aimed at the wrong one silently finds nothing. A rehearsal that prints
회차 017 while the 예매 창 shows 022 has found that for you.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime, timedelta  # noqa: E402

from core.clock import KST, NolSniperError  # noqa: E402


def _load(name: str):
    source = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    # The renderer calls into core/seat.py; the extracted function needs those
    # names in its globals or it raises NameError the moment a log is present.
    from datetime import datetime, timedelta

    from core.clock import KST, NolSniperError
    from core.seat import waiting_log_lines

    # A function lifted out of its module has none of that module's globals.
    ns: dict = {
        "waiting_log_lines": waiting_log_lines,
        "datetime": datetime, "timedelta": timedelta,
        "KST": KST, "NolSniperError": NolSniperError,
    }
    ns["max"] = max
    # _tick_countdown reaches for these when nothing is armed.
    from core.clock import parse_target_time

    ns["parse_target_time"] = parse_target_time
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns[name]


render = _load("_render_entry_result")
bump = _load("_bump_test_time")
tick = _load("_tick_countdown")
text = _load("_countdown_text")


class FakeVar:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = str(value)

    def get(self) -> str:
        return self.value


class FakePanel:
    def __init__(self) -> None:
        self.test_result = FakeVar()


class TheRehearsalTimeStaysReachable(unittest.TestCase):
    """[+1분] must always produce a moment you can actually rehearse against.

    The default rehearsal time is set once, when the panel is built, to a minute
    out. Nothing refreshes it — so ten minutes into a session the field names a
    moment ten minutes gone, and [테스트 실행] answers 이미 지난 시각입니다. The
    button beside it existed to fix that, and bumped from the *field*: one press
    moved a stale time from ten minutes gone to nine, so it looked like a dead
    button and the readout never changed.
    """

    class Panel:
        TEST_TIME_PAST_TOLERANCE_S = 2.0

        def __init__(self, text: str) -> None:
            self.written = None
            self._text = text

        def _test_time_text(self) -> str:
            if not self._text:
                raise NolSniperError("비어 있음")
            return self._text

        def _set_test_time(self, when) -> None:
            self.written = when

    def test_a_stale_time_is_bumped_from_now_not_from_itself(self) -> None:
        now = datetime.now(KST).replace(tzinfo=None)
        stale = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        panel = self.Panel(stale)
        bump(panel)
        self.assertGreater(
            panel.written, now,
            "one press must reach the future, whatever the field said",
        )
        self.assertLess(panel.written, now + timedelta(minutes=2))

    def test_a_future_time_is_still_bumped_from_the_field(self) -> None:
        # Someone lining a rehearsal up on a real open must be able to nudge it.
        now = datetime.now(KST).replace(tzinfo=None)
        later = now + timedelta(hours=3)
        panel = self.Panel(later.strftime("%Y-%m-%d %H:%M:%S"))
        bump(panel)
        self.assertEqual(panel.written, later.replace(microsecond=0) + timedelta(minutes=1))

    def test_an_unreadable_field_falls_back_to_now(self) -> None:
        panel = self.Panel("")
        bump(panel)
        self.assertGreater(panel.written, datetime.now(KST).replace(tzinfo=None))


class TheCountdownCountsToWhatWillActuallyFire(unittest.TestCase):
    """A clock that stops is worse than no clock.

    The countdown read 티켓 오픈 only. Arming a rehearsal a minute out — the
    single most common thing anyone does here — showed no clock at all, and the
    status line's "테스트 예약 · 60.0초" was written once and never moved. You
    watched a stopped number for the one minute you most wanted a clock for.
    """

    class Panel:
        def __init__(self, now: float, armed=None, is_test=False, open_at=("", "")) -> None:
            self._now = now
            self._armed_target_unix = armed
            self._armed_is_test = is_test
            self.shown = "unset"
            self.target_date = FakeVar()
            self.target_time = FakeVar()
            self.target_date.set(open_at[0])
            self.target_time.set(open_at[1])

            panel = self

            class Clock:
                @staticmethod
                def server_time_unix() -> float:
                    return panel._now

            self.clock = Clock()

        _countdown_text = staticmethod(text)

        def _show_countdown(self, value) -> None:
            self.shown = value

    NOW = 1_800_000_000.0

    def test_an_armed_rehearsal_gets_a_live_clock(self) -> None:
        panel = self.Panel(self.NOW, armed=self.NOW + 47, is_test=True)
        tick(panel)
        self.assertEqual(panel.shown, "테스트 00:00:47")

    def test_a_real_arm_counts_without_the_test_label(self) -> None:
        panel = self.Panel(self.NOW, armed=self.NOW + 3600, is_test=False)
        tick(panel)
        self.assertEqual(panel.shown, "01:00:00")

    def test_the_armed_moment_wins_over_the_show_open(self) -> None:
        # Rehearsing a minute out while the real open is weeks away must show
        # the minute, not the weeks.
        panel = self.Panel(self.NOW, armed=self.NOW + 60, is_test=True,
                           open_at=("2099-01-01", "20:00:00"))
        tick(panel)
        self.assertEqual(panel.shown, "테스트 00:01:00")

    def test_a_fired_arm_stops_claiming_to_be_next(self) -> None:
        panel = self.Panel(self.NOW, armed=self.NOW - 1, is_test=True)
        tick(panel)
        self.assertIsNone(panel.shown, "a moment that has passed counts to nothing")
        self.assertIsNone(panel._armed_target_unix, "and is forgotten, not re-checked forever")

    def test_nothing_armed_and_no_open_time_hides_the_clock(self) -> None:
        panel = self.Panel(self.NOW)
        tick(panel)
        self.assertIsNone(panel.shown)

    def test_days_are_spelled_out_for_a_show_weeks_away(self) -> None:
        # 3033:41:30 is unreadable as a countdown.
        self.assertEqual(text(4 * 86400 + 3661), "4일 01:01:01")
        self.assertEqual(text(3661), "01:01:01")


class EntryTestReadout(unittest.TestCase):
    def test_reports_the_round_it_used(self) -> None:
        panel = FakePanel()
        render(panel, {
            "fired": True, "latenessMs": 3.2, "enteredVia": "waiting",
            "goodsCode": "26011611", "playSeq": "022", "lastError": "",
        })
        text = panel.test_result.get()
        self.assertIn("진입 성공", text)
        self.assertIn("+3ms", text, "lateness is signed: early matters as much as late")
        self.assertIn("회차 022", text, "the round is the whole point of the readout")
        self.assertIn("26011611", text)

    def test_a_failure_says_why(self) -> None:
        panel = FakePanel()
        render(panel, {
            "fired": True, "latenessMs": -12.0, "enteredVia": "",
            "goodsCode": "26011611", "playSeq": "017", "lastError": "대기열 API 실패",
        })
        text = panel.test_result.get()
        self.assertIn("진입 실패", text)
        self.assertIn("대기열 API 실패", text)
        self.assertIn("-12ms", text, "an early fire is still worth seeing")

    def test_a_real_arm_reports_too_not_only_a_rehearsal(self) -> None:
        """The bug this readout was hiding.

        It opened with `if not self._entry_test_started: return`, and that flag
        was set in exactly one place — run_entry_test. So a real 대기 시작
        published lateness, attempt count, clock quality and the round across
        the bridge, and the panel discarded every one of them. "It fires way
        outside the expected time" could not be answered because the answer was
        being computed and thrown away on precisely the runs that mattered.
        """
        panel = FakePanel()
        render(panel, {"fired": True, "latenessMs": 1.0, "enteredVia": "waiting",
                       "goodsCode": "X", "playSeq": "1"})
        self.assertIn("진입 성공", panel.test_result.get())

    def test_nothing_to_report_before_it_fires(self) -> None:
        panel = FakePanel()
        render(panel, {"fired": False})
        self.assertEqual(panel.test_result.get(), "")

    def test_an_arm_refused_before_firing_still_says_why(self) -> None:
        """The case you most need to see, and the one that showed nothing.

        A gateway block refuses the arm *before* it fires, so gating the whole
        readout on `fired` meant "it did not even try" rendered as silence.
        """
        panel = FakePanel()
        render(panel, {
            "fired": False,
            "lastError": "접속 차단 중 — 165초 후에 다시 시도하세요. (/onestop/api/seatStatus)",
        })
        text = panel.test_result.get()
        self.assertIn("접속 차단", text)
        self.assertIn("/onestop/api/seatStatus", text,
                      "the endpoint must survive the truncation")

    @staticmethod
    def _body(name: str) -> str:
        """The whole function, not a fixed slice of characters.

        This used to read the first 600 bytes, so adding a comment moved the
        line it was checking out of range and failed a working change.
        """
        source = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
        return ast.get_source_segment(source, fn) or ""

    def test_the_test_time_is_separate_from_the_real_open(self) -> None:
        """Rehearsing must never disturb the 티켓 오픈 you have set."""
        fn = self._body("run_entry_test")
        self.assertIn("_test_time_text()", fn, "the test fires at its own time")
        self.assertNotIn("target_date", fn, "and never touches the real open fields")
        # It must go through the real scheduler, not a second implementation.
        self.assertIn("_arm_worker", fn)

    def test_the_rehearsal_time_is_an_absolute_moment(self) -> None:
        """You pick a time, not an offset from now.

        It used to be a list of relative offsets (30초/1분/2분/5분/10분 뒤),
        which cannot rehearse against a real open — the one thing a rehearsal is
        for. The offsets are gone; "+1분" survives as a smoke test beside a real
        clock.
        """
        source = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
        self.assertNotIn("TEST_OFFSETS", source, "the offset table is gone")
        fn = self._body("_test_time_text")
        for var in ("test_hour", "test_minute", "test_second", "test_date"):
            self.assertIn(var, fn, f"the picked {var} must reach the target string")

    def test_the_picked_time_is_shaped_for_the_parser(self) -> None:
        """parse_target_time takes 'YYYY-MM-DD HH:MM:SS'; anything else raises."""
        from core.clock import KST, parse_target_time

        build = _load("_test_time_text")
        panel = FakePanel()
        panel.test_date, panel.test_hour = FakeVar(), FakeVar()
        panel.test_minute, panel.test_second = FakeVar(), FakeVar()
        panel.test_date.set("2027-01-27")
        panel.test_hour.set("14")
        panel.test_minute.set("00")
        panel.test_second.set("07")
        text = build(panel)
        self.assertEqual(text, "2027-01-27 14:00:07")
        # And it round-trips through the real parser at the intended instant.
        parsed = parse_target_time(text, target_tz=KST)
        self.assertEqual(datetime.fromtimestamp(parsed, KST).strftime("%H:%M:%S"), "14:00:07")

    def test_an_impossible_time_is_refused_rather_than_armed(self) -> None:
        from core.clock import NolSniperError

        build = _load("_test_time_text")
        panel = FakePanel()
        panel.test_date, panel.test_hour = FakeVar(), FakeVar()
        panel.test_minute, panel.test_second = FakeVar(), FakeVar()
        panel.test_date.set("2027-01-27")
        panel.test_hour.set("99")
        panel.test_minute.set("00")
        panel.test_second.set("00")
        with self.assertRaises(NolSniperError):
            build(panel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
