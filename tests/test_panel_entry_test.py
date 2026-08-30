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

from datetime import datetime  # noqa: E402


def _load(name: str):
    source = (ROOT / "mac" / "pureclick.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PureClickMacApp")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    # The renderer calls into core/seat.py; the extracted function needs those
    # names in its globals or it raises NameError the moment a log is present.
    from datetime import datetime, timedelta

    from core.clock import KST, PureClickError
    from core.seat import waiting_log_lines

    # A function lifted out of its module has none of that module's globals.
    ns: dict = {
        "waiting_log_lines": waiting_log_lines,
        "datetime": datetime, "timedelta": timedelta,
        "KST": KST, "PureClickError": PureClickError,
    }
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns[name]


render = _load("_render_entry_result")


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
        source = (ROOT / "mac" / "pureclick.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PureClickMacApp")
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
        source = (ROOT / "mac" / "pureclick.py").read_text(encoding="utf-8")
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
        from core.clock import PureClickError

        build = _load("_test_time_text")
        panel = FakePanel()
        panel.test_date, panel.test_hour = FakeVar(), FakeVar()
        panel.test_minute, panel.test_second = FakeVar(), FakeVar()
        panel.test_date.set("2027-01-27")
        panel.test_hour.set("99")
        panel.test_minute.set("00")
        panel.test_second.set("00")
        with self.assertRaises(PureClickError):
            build(panel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
