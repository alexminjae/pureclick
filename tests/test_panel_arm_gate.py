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

from core.clock import KST, PureClickError  # noqa: E402

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
    ns: dict = {"datetime": datetime, "timedelta": timedelta, "KST": KST,
                "PureClickError": PureClickError}
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns


NS = _methods("_update_guidance", "_show_guidance", "_sale_open", "_open_time",
              "_set_enabled")
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


class _Box:
    """Stands in for the tip box, which is packed and unpacked."""

    def __init__(self) -> None:
        self.shown = True

    def winfo_exists(self) -> bool:
        return True

    def winfo_manager(self) -> str:
        return "pack" if self.shown else ""

    def pack(self, **_kwargs) -> None:
        self.shown = True

    def pack_forget(self) -> None:
        self.shown = False


class _Panel:
    """Enough of the panel to run _update_guidance without a display."""

    def __init__(self, info: dict | None, page: str = "nol",
                 seat: dict | None = None) -> None:
        self._show_info_data = info
        self.guidance = _Var()
        self.open_note = _Var()
        self.btn_arm = _Button()
        self.btn_catch = _Button()
        self.btn_test = _Button()
        self.tip_edge = _Box()
        self.aim_card = None
        self._open_time = lambda: NS["_open_time"](self)
        self._sale_open = lambda: NS["_sale_open"](self)
        self._browser_goods_code = lambda ctx: (ctx or {}).get("goods_code")
        self._set_enabled = staticmethod(NS["_set_enabled"])
        self._show_guidance = lambda visible: NS["_show_guidance"](self, visible)
        NS["_update_guidance"](self, {"page": page, "goods_code": "26099999"}, seat)


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


class GuidanceStandsDownTest(unittest.TestCase):
    """지금 할 일 is the only thing on screen addressed to the user, and the live
    band reports the macro. That division held until the macro started working:
    `_update_guidance` read `page` and nothing else, so arriving at a seat map
    pinned it to "[감시 시작]을 누르면…" and it said that while the watch ran and
    while a seat was held. Two boxes described the same moment, and one of them
    named a button you had already pressed."""

    ON_MAP = {"ticket_open_kst": FUTURE}

    def test_it_is_there_while_there_is_something_to_press(self) -> None:
        panel = _Panel(self.ON_MAP, page="seat")
        self.assertTrue(panel.tip_edge.shown)
        self.assertIn("감시 시작", panel.guidance.get())

    def test_a_running_watch_takes_it_away(self) -> None:
        panel = _Panel(self.ON_MAP, page="seat", seat={"running": True})
        self.assertFalse(panel.tip_edge.shown)

    def test_so_does_a_held_seat(self) -> None:
        panel = _Panel(self.ON_MAP, page="seat", seat={"locked": True})
        self.assertFalse(panel.tip_edge.shown)

    def test_and_it_comes_back_when_the_macro_stops(self) -> None:
        panel = _Panel(self.ON_MAP, page="seat", seat={"running": True})
        self.assertFalse(panel.tip_edge.shown)
        NS["_update_guidance"](panel, {"page": "seat", "goods_code": "26099999"},
                               {"running": False, "haltedByUser": True})
        self.assertTrue(panel.tip_edge.shown)


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


class ArmPressTest(unittest.TestCase):
    """Pressing 대기 시작 must always do something visible.

    Three ways it could look inert, all of them real:

    - `_start_worker` returned silently while another worker was alive, and one
      always is for the first ~2s after launch because the startup clock sync
      shares that slot. No thread, no message, no error.
    - The clock was synced *before* the target time was read, so an empty field
      reported ~2 seconds after the press.
    - The report was a raw Python ValueError in English:
      "time data '' does not match format '%Y-%m-%d'".
    """

    @staticmethod
    def _body(name: str) -> str:
        fn = next(n for n in _CLS.body if getattr(n, "name", None) == name)
        return ast.get_source_segment(_SRC, fn) or ""

    def test_a_busy_panel_says_so_instead_of_swallowing_the_press(self) -> None:
        body = self._body("_start_worker")
        self.assertIn("_note", body, "a refused press must be reported")
        self.assertIn("return False", body, "and the caller must be able to tell")

    def test_the_press_is_acknowledged_before_the_worker_starts(self) -> None:
        self.assertIn("self.status.set", self._body("arm"))

    def test_the_target_is_read_before_the_clock_is_synced(self) -> None:
        body = self._body("_arm_worker")
        wanted = body.index("_target_time_text()")
        synced = body.index("self._sync_now()")
        self.assertLess(wanted, synced,
                        "validate the time before spending two seconds on the clock")

    def test_every_time_error_is_readable(self) -> None:
        build = _methods("_target_time_text")["_target_time_text"]

        class _Field:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        class _P:
            pass

        cases = {
            ("", ""): "입력하세요",
            ("2026-09-05", ""): "입력하세요",
            ("5 Sept", "20:00"): "날짜 형식",
            ("2026-09-05", "저녁"): "시각 형식",
        }
        for (date, clock), expected in cases.items():
            panel = _P()
            panel.target_date, panel.target_time = _Field(date), _Field(clock)
            with self.assertRaises(Exception) as caught:
                build(panel)
            message = str(caught.exception)
            self.assertIn(expected, message, f"{date!r} {clock!r} -> {message}")
            self.assertNotIn("does not match format", message, "no raw ValueError text")

    def test_a_valid_time_still_parses(self) -> None:
        build = _methods("_target_time_text")["_target_time_text"]

        class _Field:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        class _P:
            pass

        panel = _P()
        panel.target_date, panel.target_time = _Field("2026-09-05"), _Field("20:00")
        self.assertEqual(build(panel), "2026-09-05 20:00:00.000")


if __name__ == "__main__":
    unittest.main()
