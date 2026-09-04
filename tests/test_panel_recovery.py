"""Recovering from the two things that used to need a full relaunch.

Both were reported from the same Windows sitting, and both had the same shape:
something ordinary happened, nothing noticed, and the panel went on rendering a
world that no longer existed.

1. Shifting the device clock forward — which is what people do to make a
   not-yet-open show's 예매하기 button appear — froze the panel's server clock
   *permanently*. `_render_server_time` set `_resyncing = True` and then asked
   `_start_worker`, which returns False whenever any other work is running. The
   thread never started, so its `finally` never cleared the latch, so no
   re-sync was ever attempted again for the rest of the session.

2. Closing the 예매 창 ended the host process and left the panel talking to a
   corpse — 예매 창 응답 없음, forever, with no button, no menu and no automatic
   recovery anywhere in the app.
"""

from __future__ import annotations

import ast
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

PANEL_SOURCE = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")


def _load(name: str, extra: dict | None = None):
    """Lift one method out of the panel, without importing tkinter."""
    tree = ast.parse(PANEL_SOURCE)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"threading": threading, "time": time, "getattr": getattr}
    ns.update(extra or {})
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns[name]


class ResyncNeverJams(unittest.TestCase):
    """The clock has to be able to re-measure whatever else is going on."""

    def test_the_resync_does_not_share_the_worker_slot(self):
        # The bug, stated as a rule. `_start_worker` refuses when anything else
        # is running and the caller had already raised the latch, so one
        # refusal was permanent — and a clock change is precisely the moment
        # the panel is most likely to be busy.
        tree = ast.parse(PANEL_SOURCE)
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
        for name in ("_render_server_time", "_resync_now"):
            fn = next(n for n in cls.body
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = [
                node.func.attr
                for node in ast.walk(fn)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ]
            self.assertNotIn(
                "_start_worker", calls,
                f"{name} must not queue the re-sync behind other work",
            )

    def test_a_failed_resync_releases_the_latch_for_the_next_tick(self):
        class Panel:
            _resyncing = False
            _resync_thread = None
            notes: list = []

            def _sync_now(self):
                raise RuntimeError("no network")

            def _ui(self, fn, /, *args, **kwargs):
                fn(*args, **kwargs)

            def _note(self, text, *, error=False):
                Panel.notes.append(text)

        panel = Panel()
        _load("_resync_worker").__get__(panel, Panel)()
        self.assertFalse(panel._resyncing, "a failure must not wedge the latch")
        self.assertTrue(any("다시 맞추지 못했습니다" in n for n in Panel.notes),
                        "and must say so rather than failing silently")

    def test_the_resync_runs_even_while_another_worker_is_alive(self):
        started = threading.Event()

        class Panel:
            _resyncing = False
            _resync_thread = None
            # The shared slot, deliberately occupied — this is the exact state
            # in which the old code gave up forever.
            worker = threading.Thread(target=lambda: time.sleep(30), daemon=True)

            def _sync_now(self):
                started.set()
                return type("R", (), {"offset_seconds": 0.012})()

            def _ui(self, fn, /, *args, **kwargs):
                fn(*args, **kwargs)

            def _note(self, text, *, error=False):
                pass

        Panel._resync_worker = _load("_resync_worker")
        Panel.worker.start()
        panel = Panel()
        _load("_resync_now").__get__(panel, Panel)()
        self.assertTrue(started.wait(5), "a busy panel must still be able to re-sync")
        panel._resync_thread.join(5)
        self.assertFalse(panel._resyncing)


class BrowserComesBack(unittest.TestCase):
    """Closing the 예매 창 must not cost you the whole program."""

    def _panel(self, running: bool):
        class Bridge:
            def __init__(self):
                self.running = running
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Panel:
            REOPEN_COOLDOWN_S = 3.0
            REOPEN_MAX_TRIES = 5

            def __init__(self):
                self.browser = Bridge()
                self.starts = 0
                self.notes: list = []
                self._reopen_tries = 0
                self._reopen_at = 0.0

            def _start_browser(self):
                self.starts += 1

            def _note(self, text, *, error=False):
                self.notes.append(text)

        return Panel()

    def test_a_live_browser_is_left_alone(self):
        panel = self._panel(running=True)
        _load("_keep_browser_alive").__get__(panel, type(panel))()
        self.assertEqual(panel.starts, 0, "nothing is wrong; do not restart it")

    def test_a_dead_browser_is_reopened_and_the_user_is_told(self):
        panel = self._panel(running=False)
        keep = _load("_keep_browser_alive").__get__(panel, type(panel))
        keep()
        self.assertEqual(panel.starts, 1)
        self.assertTrue(any("다시 여는 중" in n for n in panel.notes))

    def test_a_failing_relaunch_backs_off_instead_of_spinning(self):
        panel = self._panel(running=False)
        keep = _load("_keep_browser_alive").__get__(panel, type(panel))
        keep()
        keep()
        keep()
        self.assertEqual(panel.starts, 1,
                         "relaunching a browser that will not start, twice a second, "
                         "is worse than one clear message")

    def test_it_gives_up_and_points_at_the_button(self):
        panel = self._panel(running=False)
        keep = _load("_keep_browser_alive").__get__(panel, type(panel))
        for _ in range(panel.REOPEN_MAX_TRIES + 2):
            panel._reopen_at = 0.0  # ignore the cooldown; this is about the cap
            keep()
        self.assertEqual(panel.starts, panel.REOPEN_MAX_TRIES,
                         "it must stop trying, not roll past the cap")
        self.assertTrue(any("다시 열기" in n for n in panel.notes),
                        "and hand the user the manual way out")

    def test_the_reopen_button_exists_and_is_wired_to_the_bridge(self):
        # The whole point is that it is reachable when nothing else works, so
        # it lives outside the scrolling body beside 전부 정지.
        self.assertIn("예매 창 다시 열기", PANEL_SOURCE)
        self.assertIn("command=self.reopen_browser", PANEL_SOURCE)


class BridgeRemembersWhereItWas(unittest.TestCase):
    def test_a_respawn_still_tiles_beside_the_panel(self):
        import browser_bridge

        bridge = browser_bridge.BrowserBridge(ROOT / "mac")
        launched: list = []

        class FakeProc:
            def __init__(self):
                self._alive = True

            def poll(self):
                return None if self._alive else 0

        def fake_popen(argv, **kwargs):
            launched.append(argv)
            return FakeProc()

        original = browser_bridge.subprocess.Popen
        browser_bridge.subprocess.Popen = fake_popen
        try:
            bridge.start(geometry=(10, 20, 300, 400))
            self.assertEqual(launched[0][-4:], ["10", "20", "300", "400"])
            # The browser goes away, and something other than _start_browser
            # brings it back — push() and navigate() both do, with no geometry.
            bridge.process._alive = False
            bridge.start()
            self.assertEqual(
                launched[1][-4:], ["10", "20", "300", "400"],
                "a 예매 창 that came back untiled reads as a different bug entirely",
            )
        finally:
            browser_bridge.subprocess.Popen = original


if __name__ == "__main__":
    unittest.main()
