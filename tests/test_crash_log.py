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
