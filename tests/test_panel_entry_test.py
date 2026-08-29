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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    source = (ROOT / "mac" / "pureclick.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PureClickMacApp")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {}
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
    def __init__(self, started: float = 1.0) -> None:
        self._entry_test_started = started
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

    def test_silent_until_a_test_is_run(self) -> None:
        """A live run must not paint its result into the rehearsal box."""
        panel = FakePanel(started=0.0)
        render(panel, {"fired": True, "latenessMs": 1.0, "goodsCode": "X", "playSeq": "1"})
        self.assertEqual(panel.test_result.get(), "")

    def test_nothing_to_report_before_it_fires(self) -> None:
        panel = FakePanel()
        render(panel, {"fired": False})
        self.assertEqual(panel.test_result.get(), "")

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

    def test_the_moment_is_recomputed_when_the_button_is_pressed(self) -> None:
        """An offset chosen ten minutes ago points into the past.

        The time is picked as an offset from now, so a panel left open would
        otherwise arm against a moment that has already gone and fail with
        이미 지난 시각입니다 instead of testing anything.
        """
        self.assertIn("_on_test_offset()", self._body("run_entry_test"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
