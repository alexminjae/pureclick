"""Neither process may fail silently.

The frozen build runs with console=False, so an uncaught exception that kills
either role — the panel or the 예매 창 subprocess — before it can report
anything is otherwise invisible: no console, no dialog, nothing for the user or
for whoever is trying to diagnose it after the fact to look at. pureclick_main
wraps both roles and writes any escaping exception to a log file in the
persistent data directory before letting it continue to terminate the process.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pureclick_main  # noqa: E402


class CrashLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "crash.log"
        self._original = pureclick_main._crash_log_path
        pureclick_main._crash_log_path = lambda: self.log_path

    def tearDown(self) -> None:
        pureclick_main._crash_log_path = self._original
        self._tmp.cleanup()

    def test_an_exception_is_written_with_its_traceback(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError as exc:
            pureclick_main._log_crash("browser_host (예매 창)", exc)

        self.assertTrue(self.log_path.is_file())
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("browser_host (예매 창)", text)
        self.assertIn("ValueError", text)
        self.assertIn("boom", text)
        self.assertIn("test_an_exception_is_written_with_its_traceback", text,
                      "a traceback with no frames is useless for diagnosing anything")

    def test_a_second_crash_appends_rather_than_overwrites(self) -> None:
        """The whole point is to catch a crash on first launch — losing it to
        the next one because a single log rotates would defeat that."""
        for message in ("first", "second"):
            try:
                raise RuntimeError(message)
            except RuntimeError as exc:
                pureclick_main._log_crash("test", exc)

        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_system_exit_is_logged_too(self) -> None:
        """browser_host.py raises SystemExit, not Exception, when pywebview
        itself is missing — the class of failure this exists for."""
        try:
            raise SystemExit("pywebview is required")
        except SystemExit as exc:
            pureclick_main._log_crash("browser_host (예매 창)", exc)

        self.assertIn("pywebview is required", self.log_path.read_text(encoding="utf-8"))

    def test_a_logging_failure_does_not_raise(self) -> None:
        """A broken logger must never mask the original crash with a second,
        unrelated one — this is diagnostics, not the main event."""
        pureclick_main._crash_log_path = lambda: Path("/nonexistent/directory/crash.log")
        try:
            raise ValueError("boom")
        except ValueError as exc:
            pureclick_main._log_crash("test", exc)  # must not raise

    def test_the_default_path_lives_in_the_persistent_data_directory(self) -> None:
        """Not next to a frozen build's script — that resolves inside
        sys._MEIPASS and is gone by the time anyone goes looking for the log."""
        import app_platform

        expected = app_platform.user_data_dir() / "crash.log"
        pureclick_main._crash_log_path = self._original  # the real function, not setUp's fake
        self.assertEqual(pureclick_main._crash_log_path(), expected)


class BackgroundThreadCrashTests(unittest.TestCase):
    """The gap the main-thread try/except cannot cover by construction.

    watch_state and poll_context — the browser process's two pollers, and the
    only thing that ever writes the bridge health report the panel reads — run
    as background threads. An exception there does not propagate up through
    main()'s own call stack to be caught by its try/except; Python routes it to
    threading.excepthook instead, and the thread just ends. This is precisely
    the shape the msvcrt intra-process lock bug had, and reported live as the
    예매 창 rendering once and then going blank again — a poller dying
    mid-session, after the first successful render, not before it.
    """

    def setUp(self) -> None:
        self._original_hook = threading.excepthook
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "crash.log"
        self._original_path_fn = pureclick_main._crash_log_path
        pureclick_main._crash_log_path = lambda: self.log_path

    def tearDown(self) -> None:
        threading.excepthook = self._original_hook
        pureclick_main._crash_log_path = self._original_path_fn
        self._tmp.cleanup()

    def test_pureclick_main_installs_the_hook_at_import_time(self) -> None:
        """Not a helper nobody calls — this is what makes it actually run."""
        self.assertIs(threading.excepthook, pureclick_main._thread_crashed)

    def test_an_exception_in_a_background_thread_is_logged(self) -> None:
        def poller() -> None:
            raise ValueError("simulated poll_context crash")

        t = threading.Thread(target=poller, name="poll_context")
        t.start()
        t.join(timeout=5)

        self.assertTrue(self.log_path.is_file(),
                        "the try/except in main() cannot see this — only the hook can")
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("poll_context", text, "must name which thread died, not just that one did")
        self.assertIn("simulated poll_context crash", text)

    def test_a_clean_thread_exit_logs_nothing(self) -> None:
        t = threading.Thread(target=lambda: None, name="watch_state")
        t.start()
        t.join(timeout=5)
        self.assertFalse(self.log_path.exists(), "a log on every ordinary exit would bury the real one")
