"""When 대기 시작 is offered, and when it is not.

오픈 대기 only means anything before a show goes on sale, so the button is gated
on that — deliberately. But the gate used to fail closed: `_sale_open()` returned
True both when the sale had started *and* when the open time could not be read
at all, and an unknown time therefore read as "no waiting needed". Standing in
front of a show that had not opened, with the panel unable to name its open
time, you were told to press 예매하기 instead — and the button you needed was
greyed out.

The panel accepts a typed open time, so there was never a reason to lock it.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.clock import KST  # noqa: E402

_SRC = (ROOT / "mac" / "pureclick.py").read_text(encoding="utf-8")
_CLS = next(
    n for n in ast.parse(_SRC).body
    if isinstance(n, ast.ClassDef) and n.name == "PureClickMacApp"
)


def _methods(*names):
    body = [n for n in _CLS.body if getattr(n, "name", None) in names]
    assert len(body) == len(names), f"missing: {set(names) - {getattr(n, 'name', None) for n in body}}"
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"datetime": datetime, "timedelta": timedelta, "KST": KST}
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns


NS = _methods("_update_guidance", "_sale_open", "_open_time", "_set_enabled")
FUTURE = (datetime.now(KST) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
PAST = (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")


class _Var:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value) -> None:
        self.value = str(value)

    def get(self) -> str:
        return self.value


class _Button:
    def __init__(self) -> None:
        self.enabled: bool | None = None

    def state(self, flags) -> None:
        self.enabled = "!disabled" in flags


class _Panel:
    """Enough of the panel to run _update_guidance without a display."""

    def __init__(self, info: dict | None, page: str = "nol") -> None:
        self._show_info_data = info
        self.guidance = _Var()
        self.open_note = _Var()
        self.btn_arm = _Button()
        self.btn_catch = _Button()
        self.btn_test = _Button()
        self._open_time = lambda: NS["_open_time"](self)
        self._sale_open = lambda: NS["_sale_open"](self)
        self._browser_goods_code = lambda ctx: (ctx or {}).get("goods_code")
        self._set_enabled = staticmethod(NS["_set_enabled"])
        NS["_update_guidance"](self, {"page": page, "goods_code": "26099999"})


class ArmGateTest(unittest.TestCase):
    def test_a_show_that_has_not_opened_offers_the_button(self) -> None:
        panel = _Panel({"ticket_open_kst": FUTURE})
        self.assertTrue(panel.btn_arm.enabled)
        self.assertIn("판매 전", panel.guidance.get())

    def test_an_unknown_open_time_is_not_read_as_already_open(self) -> None:
        """The bug. Unknown is not open, and the panel takes a typed time."""
        for unknown in ("", None, "곧 오픈", "미정"):
            panel = _Panel({"ticket_open_kst": unknown})
            self.assertTrue(
                panel.btn_arm.enabled, f"locked out by ticket_open_kst={unknown!r}"
            )
            self.assertIn("직접 입력", panel.guidance.get())

    def test_a_show_already_on_sale_does_not_offer_it(self) -> None:
        # Deliberate: there is no queue to be first into once the sale is open.
        panel = _Panel({"ticket_open_kst": PAST})
        self.assertFalse(panel.btn_arm.enabled)
        self.assertIn("예매하기", panel.guidance.get())

    def test_the_seat_map_offers_the_watch_instead(self) -> None:
        panel = _Panel({"ticket_open_kst": FUTURE}, page="seat")
        self.assertFalse(panel.btn_arm.enabled)
        self.assertTrue(panel.btn_catch.enabled)

    def test_the_rehearsal_follows_the_same_pages_as_a_real_entry(self) -> None:
        # It arms the real scheduler, so it can only work where one can run.
        self.assertTrue(_Panel({"ticket_open_kst": FUTURE}, page="nol").btn_test.enabled)
        self.assertFalse(_Panel({"ticket_open_kst": FUTURE}, page="seat").btn_test.enabled)


class LookupRetryTest(unittest.TestCase):
    """A failed lookup used to be permanent.

    _auto_loaded_code was claimed before the fetch ran, so one failure left the
    panel at "공연 정보를 가져오는 중…" with every button disabled and nothing
    ever trying again.
    """

    @staticmethod
    def _body(name: str) -> str:
        fn = next(n for n in _CLS.body if getattr(n, "name", None) == name)
        return ast.get_source_segment(_SRC, fn) or ""

    def test_the_code_is_claimed_only_once_the_lookup_succeeds(self) -> None:
        follow = self._body("_follow_browser_show")
        self.assertNotIn("self._auto_loaded_code = code", follow,
                         "claiming it here blocks every retry")
        self.assertIn("self._auto_loaded_code = code", self._body("_apply_show_info"),
                      "it is claimed on success instead")

    def test_a_retry_cannot_storm_the_api(self) -> None:
        # The page is read every 500ms, so a retry needs an in-flight guard.
        follow = self._body("_follow_browser_show")
        self.assertIn("_fetching_code", follow)
        self.assertIn("_fetching_code = None", self._body("_fetch_show_worker"),
                      "and the claim must be released however the worker ends")


if __name__ == "__main__":
    unittest.main()
