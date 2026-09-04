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

from core.clock import KST, NolSniperError  # noqa: E402
from core.mode import BEFORE_OPEN, MODE_LABELS, OPEN, derive_mode, sale_phase  # noqa: E402
from core.mode import guidance as mode_guidance  # noqa: E402
from core.seat import bridge_status  # noqa: E402

_SRC = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
_CLS = next(
    n for n in ast.parse(_SRC).body
    if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp"
)


def _methods(*names):
    body = [n for n in _CLS.body if getattr(n, "name", None) in names]
    assert len(body) == len(names), f"missing: {set(names) - {getattr(n, 'name', None) for n in body}}"
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"datetime": datetime, "timedelta": timedelta, "KST": KST,
                "NolSniperError": NolSniperError, "derive_mode": derive_mode,
                "sale_phase": sale_phase, "mode_guidance": mode_guidance,
                "MODE_LABELS": MODE_LABELS, "OPEN": OPEN, "BEFORE_OPEN": BEFORE_OPEN,
                "bridge_status": bridge_status}
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns


NS = _methods("_update_guidance", "_show_guidance", "_sale_open", "_open_time",
              "_set_enabled", "_style_button")

FUTURE = (datetime.now(KST) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
PAST = (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")


class _Var:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value) -> None:
        self.value = str(value)

    def get(self) -> str:
        return self.value


class _Button:
    def __init__(self) -> None:
        self.enabled: bool | None = None
        self.style = ""

    def state(self, flags) -> None:
        self.enabled = "!disabled" in flags

    def configure(self, **kwargs) -> None:
        self.style = kwargs.get("style", self.style)


class _Bridge:
    def read_bridge_health(self) -> dict:
        return {"seen_at": __import__("time").time(), "failures": 0}


class _Panel:
    """Enough of the panel to run _update_guidance without a display."""

    def __init__(self, info: dict | None, page: str = "nol",
                 seat: dict | None = None, arm: dict | None = None,
                 rounds: bool = True, url: str = "") -> None:
        self._show_info_data = info
        self.guidance = _Var()
        self.open_note = _Var()
        self.mode_banner = _Var()
        self.mode_text = _Var()
        self.action_note = _Var()
        self.play_seq = _Var("043")
        self.auto_start_on = _Var("1")
        self.rounds = [{"play_seq": "043"}] if rounds else []
        self.btn_arm = _Button()
        self.btn_enter_now = _Button()
        self.btn_catch = _Button()
        self.btn_test = _Button()
        self.browser = _Bridge()
        self._last_arm_status = arm or {}
        self._open_time = lambda: NS["_open_time"](self)
        self._sale_open = lambda: NS["_sale_open"](self)
        self._browser_goods_code = lambda ctx: (ctx or {}).get("goods_code")
        self._set_enabled = staticmethod(NS["_set_enabled"])
        self._style_button = staticmethod(NS["_style_button"])
        self._latch_enable = lambda key, want: want  # pass-through in tests
        self._show_guidance = lambda visible: NS["_show_guidance"](self, visible)
        self.update(page, seat, url)

    def update(self, page: str, seat: dict | None = None, url: str = "", **ctx) -> None:
        NS["_update_guidance"](self, {"page": page, "goods_code": "26099999", "url": url, **ctx}, seat)


class ArmGateTest(unittest.TestCase):
    def test_a_show_that_has_not_opened_offers_the_arm_only(self) -> None:
        panel = _Panel({"ticket_open_kst": FUTURE})
        self.assertTrue(panel.btn_arm.enabled)
        self.assertFalse(panel.btn_enter_now.enabled)
        self.assertEqual(panel.btn_arm.style, "Primary.TButton")
        self.assertIn("오픈에 자동 진입", panel.guidance.get())
        self.assertIn("오픈 예정", panel.mode_banner.get())
        self.assertNotIn("판매 중", panel.mode_banner.get() + panel.guidance.get())
        self.assertIn("지금 진입", panel.action_note.get())

    def test_an_unknown_open_time_is_not_read_as_already_open(self) -> None:
        """The bug. Unknown is not open, and the panel takes a typed time."""
        for unknown in ("", None, "곧 오픈", "미정"):
            panel = _Panel({"ticket_open_kst": unknown})
            self.assertTrue(panel.btn_arm.enabled, f"locked out by ticket_open_kst={unknown!r}")
            self.assertTrue(panel.btn_enter_now.enabled)
            self.assertIn("오픈 시각", panel.guidance.get())

    def test_a_show_already_on_sale_enters_now(self) -> None:
        panel = _Panel({"ticket_open_kst": PAST})
        self.assertFalse(panel.btn_arm.enabled)
        self.assertTrue(panel.btn_enter_now.enabled)
        self.assertEqual(panel.btn_enter_now.style, "Primary.TButton")
        self.assertIn("지금 진입", panel.guidance.get())
        self.assertIn("판매 중", panel.mode_banner.get())
        self.assertIn("오픈에 자동 진입", panel.action_note.get())

    def test_no_round_picked_says_so_first(self) -> None:
        panel = _Panel({"ticket_open_kst": PAST}, rounds=False)
        self.assertIn("회차", panel.guidance.get())

    def test_the_seat_map_offers_the_watch_instead(self) -> None:
        panel = _Panel({"ticket_open_kst": FUTURE}, page="seat")
        self.assertFalse(panel.btn_arm.enabled)
        self.assertFalse(panel.btn_enter_now.enabled)
        self.assertTrue(panel.btn_catch.enabled)
        self.assertEqual(panel.mode_text.get(), "좌석맵")


class LoginWatchdogTest(unittest.TestCase):
    def test_no_session_disables_entry_and_says_so(self) -> None:
        panel = _Panel({"ticket_open_kst": PAST})
        panel.update("nol", logged_in=False)
        self.assertIn("로그인 필요", panel.mode_banner.get())
        self.assertFalse(panel.btn_enter_now.enabled)
        self.assertFalse(panel.btn_arm.enabled)

    def test_a_session_keeps_entry_on(self) -> None:
        panel = _Panel({"ticket_open_kst": PAST})
        panel.update("nol", logged_in=True)
        self.assertTrue(panel.btn_enter_now.enabled)


class ModeBannerTest(unittest.TestCase):
    """The banner, the instruction and the buttons come from one mode."""

    ON_MAP = {"ticket_open_kst": PAST}

    def test_a_running_watch_is_watching(self) -> None:
        panel = _Panel(self.ON_MAP, page="seat", seat={"running": True, "runMode": "catch"})
        self.assertEqual(panel.mode_text.get(), "취켓팅 중")
        self.assertFalse(panel.btn_enter_now.enabled)
        self.assertFalse(panel.btn_catch.enabled)

    def test_a_held_seat_is_held_and_never_waiting(self) -> None:
        panel = _Panel(self.ON_MAP, page="seat", seat={"locked": True, "pageSelected": 1})
        self.assertEqual(panel.mode_text.get(), "좌석 잡음")
        self.assertNotIn("대기 중", panel.guidance.get())
        self.assertIn("결제", panel.guidance.get())

    def test_armed_waits_and_offers_nothing_else(self) -> None:
        panel = _Panel({"ticket_open_kst": FUTURE}, page="goods", arm={"running": True, "fired": False})
        self.assertEqual(panel.mode_text.get(), "오픈 대기 중")
        self.assertFalse(panel.btn_arm.enabled)
        self.assertFalse(panel.btn_enter_now.enabled)

    def test_stopping_brings_the_actions_back(self) -> None:
        panel = _Panel(self.ON_MAP, page="seat", seat={"running": True, "runMode": "catch"})
        panel.update("seat", {"running": False, "haltedByUser": True})
        self.assertEqual(panel.mode_text.get(), "중지됨")
        self.assertTrue(panel.btn_catch.enabled)

    def test_home_forgets_the_show(self) -> None:
        panel = _Panel(self.ON_MAP, page="other", url="https://nol.yanolja.com/ticket")
        self.assertEqual(panel.mode_text.get(), "공연 없음")
        self.assertFalse(panel.btn_enter_now.enabled)


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
