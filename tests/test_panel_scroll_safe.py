"""Status updates must not move the scrolled column.

Every live label in the panel reserves its height and the scroll root
re-anchors to the top pixel when the column's height changes. This drives the
real `_scrollable_root` in a Tk root, scrolls to the middle, spams the kind of
text changes the 500ms poll makes, and checks the view did not move.
Skipped where Tk has no display.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mac"))

try:
    import tkinter as tk
    from tkinter import ttk
    _root = tk.Tk()
    _root.withdraw()
    HAVE_TK = True
except Exception:  # noqa: BLE001 - no display
    HAVE_TK = False
    _root = None


def _method(name: str):
    source = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(source).body
               if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"tk": tk, "ttk": ttk, "sys": sys, "BG": "#111"}
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns[name]


@unittest.skipUnless(HAVE_TK, "needs a Tk display")
class ScrollSafeStatus(unittest.TestCase):
    def _panel(self):
        top = tk.Toplevel(_root)
        top.geometry("360x400")
        top._scroll_canvas = None
        body = _method("_scrollable_root")(top)
        return top, body

    def _settle(self, top) -> None:
        for _ in range(3):
            top.update_idletasks()
            top.update()

    def test_fixed_height_labels_never_move_the_view(self) -> None:
        top, body = self._panel()
        status = tk.StringVar(value="대기 중")
        tk.Label(body, textvariable=status, height=2, wraplength=300, justify="left",
                 anchor="nw").pack(fill="x")
        for i in range(40):
            tk.Label(body, text=f"row {i}", height=1).pack(fill="x")
        self._settle(top)
        canvas = top._scroll_canvas
        canvas.yview_moveto(0.4)
        self._settle(top)
        before = canvas.yview()[0]
        top_pixel = canvas.canvasy(0)
        for i in range(30):
            status.set(("취켓팅 감시 중 · 100ms 간격 · 빈 좌석 0석 — 취소표가 나오면 즉시 잡습니다 " * (1 + i % 3)).strip())
            self._settle(top)
            self.assertAlmostEqual(canvas.canvasy(0), top_pixel, delta=1.0,
                                   msg=f"view moved on update {i}")
        self.assertAlmostEqual(canvas.yview()[0], before, delta=0.002)
        top.destroy()

    def test_a_growing_label_keeps_the_top_edge(self) -> None:
        """Even a label with no reserved height must not shift what is under
        the pointer: the root re-anchors to the top pixel on every resize."""
        top, body = self._panel()
        for i in range(20):
            tk.Label(body, text=f"row {i}", height=1).pack(fill="x")
        grower = tk.StringVar(value="x")
        tk.Label(body, textvariable=grower, wraplength=300, justify="left").pack(fill="x")
        for i in range(20):
            tk.Label(body, text=f"tail {i}", height=1).pack(fill="x")
        self._settle(top)
        canvas = top._scroll_canvas
        canvas.yview_moveto(0.3)
        self._settle(top)
        top_pixel = canvas.canvasy(0)
        for lines in (1, 4, 1, 6, 2):
            grower.set("\n".join("긴 상태 문장" for _ in range(lines)))
            self._settle(top)
            self.assertAlmostEqual(canvas.canvasy(0), top_pixel, delta=1.0)
        top.destroy()


if __name__ == "__main__":
    unittest.main()
