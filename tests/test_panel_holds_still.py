"""The 조작판 must not resize or churn while it reports.

Reported three times across one session, each time as a different symptom:
"the status page starts tweaking", then "it keeps on shifting everything like
it's bugging out the 조작판 up and down", then "it's updating rapidly now so
it's just going crazy".

Each time I fixed the mechanism that happened to be moving rather than the
property that was wanted, so it came back somewhere else. The mechanisms were:

  * a headline carrying the attempt count twice, once from the page's overlay
    text and once appended by the panel;
  * a live band whose height changed as its text wrapped to another line;
  * a ranking box in the scrolling body that set `height=len(lines)` on every
    500ms poll and shoved the whole column down as a run filled it;
  * a number line under the band holding 시도/구역/빈자리/스윕, every one of
    which moves between repaints.

The property, tested here as source structure and in test_band_is_readable.py
as behaviour: the panel renders *state*, and state does not change five times a
second. Anything that reports a moving count belongs in the published status
for debugging, not on screen during a race.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PANEL = (ROOT / "mac" / "pureclick.py").read_text(encoding="utf-8")
_TREE = ast.parse(PANEL)
_CLS = next(
    n for n in _TREE.body if isinstance(n, ast.ClassDef) and n.name == "PureClickMacApp"
)


class TheChurningRenderersStayOut(unittest.TestCase):
    # Each of these built a line or a box out of counters that move while a
    # watch runs. They are still in core/seat.py's history and in the published
    # status; they must not be rendered on the panel.
    RETIRED = ("watch_vitals", "seat_order_lines", "map_move_lines", "running_hint")

    def test_the_panel_does_not_render_a_moving_count(self) -> None:
        for name in self.RETIRED:
            self.assertNotIn(
                name, PANEL,
                f"{name} builds text out of counters; it churned the band",
            )

    def test_no_widget_is_resized_from_a_content_length(self) -> None:
        """`height=len(...)` is the shape of the bug that moved the column."""
        for node in ast.walk(_TREE):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "height":
                    continue
                self.assertNotIsInstance(
                    kw.value, ast.Call,
                    "a height computed from content resizes the widget as the "
                    "content changes, which moves everything below it",
                )

    def test_the_live_band_reserves_its_lines(self) -> None:
        """Fixed line counts are what stop the band growing as text wraps."""
        band = next(
            n for n in _CLS.body
            if isinstance(n, ast.FunctionDef) and n.name == "_build_live_band"
        )
        heights = [
            kw.value.value
            for node in ast.walk(band)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "height" and isinstance(kw.value, ast.Constant)
        ]
        self.assertGreaterEqual(
            len(heights), 2,
            "the headline and the note must both reserve their worst case",
        )
        self.assertTrue(all(isinstance(h, int) and h > 0 for h in heights))

    def test_the_band_is_packed_outside_the_scrolling_body(self) -> None:
        """It is the one thing that must never scroll away — and being outside
        the scroll is also what keeps body reflow from moving it."""
        band = next(
            n for n in _CLS.body
            if isinstance(n, ast.FunctionDef) and n.name == "_build_live_band"
        )
        source = ast.get_source_segment(PANEL, band) or ""
        self.assertIn('side="bottom"', source)
        self.assertIn("tk.Frame(self", source, "packed on the window, not on the body")


if __name__ == "__main__":
    unittest.main()
