"""Run the mechanical detectors, so these bug classes are not found by hand again.

Each detector exists because the repo shipped an instance of it:

  D1  `isVisible` was declared twice in one scope. Function declarations hoist,
      so the later one won for the whole file — including call sites written
      above it — and every visibility test silently changed behaviour.
  D3  `seatState.awaitingPayment` was read twice and never assigned true, so the
      branch that refuses a second run while holding seats could not run.
  D5  `runArmScheduler()` was called without await. It set `armState.running`
      before two awaits that sat outside the try that resets it, so one throw
      left the flag stuck and 대기 시작 silently refused every later press.

The tool needs an ES parser that is not a dependency of this repo. When none is
available the audit is skipped with a reason rather than passing quietly.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "audit_js.mjs"
TARGET = ROOT / "browser" / "pureclick_autopilot.js"

NO_PARSER_EXIT = 2


class StaticAuditTests(unittest.TestCase):
    def _run(self, target: Path) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is not available")
        return subprocess.run(
            ["node", str(TOOL), str(target)],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )

    def test_the_autopilot_has_no_gating_findings(self) -> None:
        result = self._run(TARGET)
        if result.returncode == NO_PARSER_EXIT:
            self.skipTest(result.stderr.strip() or "no ES parser available")
        self.assertEqual(
            result.returncode, 0,
            "the static audit found bugs of a class this repo has already shipped:\n"
            + result.stdout,
        )

    def test_the_detectors_actually_detect(self) -> None:
        """A tool that reports nothing on a broken file is worse than no tool."""
        broken = ROOT / "tests" / "_audit_fixture.generated.mjs"
        broken.write_text(
            """
            (function () {
              const seatState = { stuck: false };
              function shadowed() { return 1; }
              function shadowed() { return 2; }
              async function slow() { return 1; }
              function run() {
                seatState.stuck = false;
                if (seatState.stuck) return "unreachable";
                slow();
                return shadowed();
              }
              window.PureClick = { run };
            })();
            """,
            encoding="utf-8",
        )
        try:
            result = self._run(broken)
            if result.returncode == NO_PARSER_EXIT:
                self.skipTest(result.stderr.strip() or "no ES parser available")
            self.assertEqual(result.returncode, 1, "the fixture is broken three ways")
            for detector in ("D1", "D3", "D5"):
                self.assertIn(detector, result.stdout, f"{detector} missed its own bug")
        finally:
            broken.unlink(missing_ok=True)
