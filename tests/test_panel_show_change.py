"""What must be dropped when the 예매 창 moves to a different show.

Seat coordinates have no common scale between venues — posTop spans 52..111 on
one and 1168..1183 on another — so anything derived from one show's seat
positions is meaningless in the next. Keeping it produces a picker that draws
the previous venue while claiming to be the current one, which is impossible to
tell from a bug.

`_apply_catalog` only touches attributes and helpers, so it can be driven with
stand-ins rather than a display.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mac"))


def _load(name: str):
    source = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NolSniperApp")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"parse_box": lambda _b: None}
    exec(compile(module, "<panel>", "exec"), ns)  # noqa: S102 - our own source
    return ns[name]


apply_catalog = _load("_apply_catalog")

OLD_SKETCH = [{"k": "old", "x": 10.0, "y": 60.0}]


class FakeVar:
    """Stand-in for tk.StringVar."""

    def __init__(self, value: str = "") -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = str(value)


class FakePanel:
    """Only what `_apply_catalog` reaches for."""

    def __init__(self, code: str) -> None:
        self._catalog = {"goods_code": code}
        self._zone_sketch = list(OLD_SKETCH)
        self._watch_rect = {"left": 1, "top": 1, "right": 5, "bottom": 5}
        self._block_rows = [{"key": "old", "name": "old"}]
        self.notes: list[str] = []
        self.redrew = False
        # Editable fields the method mirrors the 예매판 into.
        for name in ("goods_code", "place_code", "play_date", "play_seq", "play_time"):
            setattr(self, name, FakeVar())

    # Collaborators, stubbed.
    def _note(self, text: str, *, error: bool = False) -> None:
        self.notes.append(text)

    def _update_zone_summary(self) -> None: ...
    def _refresh_round_line(self) -> None: ...

    # The 일정 picker fills from the same catalog this exercises. It draws
    # widgets, which this double has none of, so it is a no-op here — the
    # picker's own behaviour is covered in tests/test_panel_rounds.py.
    def _apply_rounds(self, catalog: dict) -> None: ...
    def _schedule_zone_map(self) -> None:
        self.redrew = True

    def _refresh_zone_picker(self) -> None: ...
    def _cache_sketch(self, goods, sketch, blocks) -> None:
        self.cached = (goods, list(sketch), list(blocks))

    def _load_cached_sketch(self, goods) -> bool:
        return False

    def _apply_blocks(self, blocks: list) -> None:
        self._block_rows = list(blocks)

    def _refresh_show_where(self, **_kw) -> None: ...
    def _merge_live_grades(self, _grades: list) -> bool:
        return False

    def _set_open_time(self, _raw: str) -> None: ...


class ShowChangeTests(unittest.TestCase):
    def test_new_show_with_blocks_but_no_sketch_yet_drops_the_old_map(self) -> None:
        """The case that used to leak.

        Blocks for the new show arrive before its sketch has been built. The
        reset lived in a branch only reached when *no* blocks arrived, so the
        previous venue stayed on screen.
        """
        panel = FakePanel("AAA111")
        apply_catalog(panel, {"goods_code": "BBB222", "blocks": [{"block_key": "new"}]})
        self.assertEqual(panel._zone_sketch, [], "the old show's seats must go")
        self.assertIsNone(panel._watch_rect, "the old show's rect must go")

    def test_new_show_with_a_sketch_uses_it(self) -> None:
        panel = FakePanel("AAA111")
        fresh = [{"k": "new", "x": 500.0, "y": 1200.0}]
        apply_catalog(
            panel, {"goods_code": "BBB222", "blocks": [{"block_key": "new"}], "sketch": fresh}
        )
        self.assertEqual(panel._zone_sketch, fresh)

    def test_changing_the_round_drops_the_old_one(self) -> None:
        """일정 변경 within the same show is a different target.

        Block keys embed the 회차 — the same venue is 017:001 on one round and
        022:001 on the next — so a rect or sketch kept across a round change
        points at seats that no longer exist, and both functions quietly stop
        finding anything. Identity was goods_code alone, which never changes
        with the round, so none of this reset.
        """
        panel = FakePanel("AAA111")
        panel._catalog = {"goods_code": "AAA111", "play_seq": "017"}
        apply_catalog(
            panel,
            {"goods_code": "AAA111", "play_seq": "022", "blocks": [{"block_key": "022:001"}]},
        )
        self.assertEqual(panel._zone_sketch, [], "the old round's seats must go")
        self.assertIsNone(panel._watch_rect, "the old round's rect must go")
        self.assertTrue(
            any("회차" in note for note in panel.notes),
            f"the user must be told why, got {panel.notes}",
        )

    def test_same_show_and_round_keeps_the_drawn_area(self) -> None:
        """Re-polling an unchanged target must not wipe a rect the user drew."""
        panel = FakePanel("AAA111")
        panel._catalog = {"goods_code": "AAA111", "play_seq": "017"}
        apply_catalog(
            panel,
            {"goods_code": "AAA111", "play_seq": "017", "blocks": [{"block_key": "017:001"}]},
        )
        self.assertEqual(panel._zone_sketch, OLD_SKETCH)
        self.assertIsNotNone(panel._watch_rect)

    def test_same_show_keeps_what_it_has(self) -> None:
        """Re-polling the same show must not wipe a rect the user drew."""
        panel = FakePanel("AAA111")
        apply_catalog(panel, {"goods_code": "AAA111", "blocks": [{"block_key": "old"}]})
        self.assertEqual(panel._zone_sketch, OLD_SKETCH)
        self.assertIsNotNone(panel._watch_rect, "same show, the drawn area still applies")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
