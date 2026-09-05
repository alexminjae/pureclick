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

    def _clear_zone_canvas(self) -> None:
        self.canvas_cleared = True

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


rounds_signature = _load("_rounds_signature")


class RoundsSignatureTests(unittest.TestCase):
    """A rounds-only catalog must register as a change, or the picker stays blank."""

    def test_a_round_list_arriving_is_a_change(self) -> None:
        before = {"goods_code": "26009314"}
        after = {"goods_code": "26009314", "rounds": [{"play_seq": "041"}, {"play_seq": "106"}]}
        self.assertNotEqual(rounds_signature(None, before), rounds_signature(None, after))

    def test_the_same_list_is_not_a_change(self) -> None:
        cat = {"rounds": [{"play_seq": "041"}, {"play_seq": "106"}]}
        self.assertEqual(rounds_signature(None, cat), rounds_signature(None, dict(cat)))
        self.assertEqual(rounds_signature(None, None), (0, "", ""))


import re as _re
from core.arm import ArmPayload as _ArmPayload
from core.clock import NolSniperError as _NolSniperError

_on_round_pick = _load("_on_round_pick")
_refresh_show_where = _load("_refresh_show_where")
_refresh_round_line = _load("_refresh_round_line")
_pretty_play_date = _load("_pretty_play_date")
_clock_text = _load("_clock_text")
_row_date = _load("_row_date")
_row_time = _load("_row_time")
_round_label = _load("_round_label")
_arm_payload = _load("_arm_payload")
# The loaded functions share one namespace; give it what the panel module has,
# including a stand-in NolSniperApp for the staticmethod cross-calls.
_NS_APP = type("NolSniperApp", (), {"_row_date": staticmethod(
    _row_date.__func__ if hasattr(_row_date, "__func__") else _row_date),
    "_row_time": staticmethod(_row_time.__func__ if hasattr(_row_time, "__func__") else _row_time)})
for _fn in (_on_round_pick, _refresh_show_where, _refresh_round_line, _pretty_play_date,
            _clock_text, _row_date, _row_time, _round_label, _arm_payload):
    (_fn.__func__ if hasattr(_fn, "__func__") else _fn).__globals__.update(
        {"re": _re, "ArmPayload": _ArmPayload, "NolSniperError": _NolSniperError,
         "NolSniperApp": _NS_APP})
_row_date = _row_date.__func__ if hasattr(_row_date, "__func__") else _row_date
_row_time = _row_time.__func__ if hasattr(_row_time, "__func__") else _row_time
_clock_text = _clock_text.__func__ if hasattr(_clock_text, "__func__") else _clock_text
_round_label = _round_label.__func__ if hasattr(_round_label, "__func__") else _round_label


class _RoundBox:
    def __init__(self, index: int) -> None:
        self._index = index
    def current(self) -> int:
        return self._index


class _Bridge:
    def __init__(self) -> None:
        self.published = []
    def publish_show(self, goods, place, biz_code="61776", **round_) -> None:
        self.published.append((goods, place, round_))


class _PickPanel:
    """Just enough panel for a round pick to flow to the card and the page."""
    ROUNDS = [
        {"play_seq": "026", "play_date": "20260905", "play_time": "1400"},
        {"play_seq": "029", "play_date": "20260908", "play_time": "1930"},
    ]
    def __init__(self, index: int) -> None:
        self.rounds = list(self.ROUNDS)
        self.round_box = _RoundBox(index)
        self.play_seq = FakeVar("026"); self.play_date = FakeVar("20260905"); self.play_time = FakeVar("1400")
        self.goods_code = FakeVar("26009314"); self.place_code = FakeVar("26000011")
        self.round_note = FakeVar(); self.show_where = FakeVar(); self.show_round = FakeVar()
        self._show_info_data = {"place_name": "블루스퀘어"}
        self._round_user_picked = False
        self.browser = _Bridge()
        self.entry_offset_ms = FakeVar("0"); self.auto_start_on = FakeVar("1")
    # bound helpers
    def _row_date(self, row): return _row_date(row)
    def _row_time(self, row): return _row_time(row)
    def _round_label(self, row): return _round_label(row)
    def _clock_text(self, v): return _clock_text(v)
    def _pretty_play_date(self, raw=None): return _pretty_play_date(self, raw)
    def _refresh_show_where(self, **kw): _refresh_show_where(self, **kw)
    def _refresh_round_line(self): _refresh_round_line(self)
    def _publish_round_hint(self):
        self.browser.publish_show(self.goods_code.get(), self.place_code.get(),
                                  play_seq=self.play_seq.get(), play_date=self.play_date.get(),
                                  play_time=self.play_time.get())
    def _resolved_goods(self): return self.goods_code.get()
    def _entry_offset_ms(self): return 0
    def get(self): return "1"


class RoundPickSyncTests(unittest.TestCase):
    """Picking 029 must move the card subtitle and the page hint to 029."""

    def test_the_subtitle_follows_the_pick(self) -> None:
        panel = _PickPanel(1)
        _on_round_pick(panel, event=object())
        self.assertEqual(panel.play_seq.get(), "029")
        self.assertEqual(panel.play_date.get(), "20260908")
        self.assertEqual(panel.play_time.get(), "1930")
        self.assertIn("2026.09.08", panel.show_where.get())
        self.assertIn("19:30", panel.show_where.get())
        self.assertIn("회차 029", panel.show_where.get())
        self.assertNotIn("회차 026", panel.show_where.get())
        self.assertTrue(panel._round_user_picked)

    def test_the_page_is_told_the_picked_round(self) -> None:
        panel = _PickPanel(1)
        _on_round_pick(panel)
        goods, place, round_ = panel.browser.published[-1]
        self.assertEqual((goods, place), ("26009314", "26000011"))
        self.assertEqual(round_["play_seq"], "029")
        self.assertEqual(round_["play_date"], "20260908")


class ArmRoundSyncTests(unittest.TestCase):
    """The arm carries the picked round, never a stale date left in the field."""

    def test_a_stale_play_date_cannot_reach_the_arm(self) -> None:
        panel = _PickPanel(1)
        panel.play_seq.set("029"); panel.play_date.set("20260804")  # stale premiere date
        panel.auto_start_on = FakeVar("1")
        payload = _arm_payload(panel, target_unix=1.0, offset_seconds=0.0, dry_run=False)
        self.assertEqual(payload.play_seq, "029")
        self.assertEqual(payload.play_date, "20260908")
        self.assertEqual(payload.play_time, "1930")

    def test_an_unknown_seq_falls_back_to_the_picker_selection(self) -> None:
        panel = _PickPanel(1)
        panel.play_seq.set("001"); panel.play_date.set("20260804")
        payload = _arm_payload(panel, target_unix=1.0, offset_seconds=0.0, dry_run=False)
        self.assertEqual((payload.play_seq, payload.play_date), ("029", "20260908"))


_park_for_entry = _load("_park_for_entry")
from core.entry import needs_parking as _needs_parking, park_url as _park_url
_park_for_entry.__globals__.update({"needs_parking": _needs_parking, "park_url": _park_url})


class _NavBridge:
    def __init__(self, url: str) -> None:
        self.url = url; self.navigated = []
    def read_page_context(self): return {"url": self.url}
    def navigate(self, url): self.navigated.append(url)


class _ParkPanel:
    """The few panel hooks _park_for_entry touches after navigating."""
    def __init__(self, url: str) -> None:
        self.browser = _NavBridge(url); self.status = FakeVar(); self.notes = []
    def _ui(self, fn, *a, **k): fn(*a, **k)
    def _note(self, text, **k): self.notes.append(text)


class ParkForEntryTests(unittest.TestCase):
    """오픈에 자동 진입 always parks on the interpark goods page."""

    def test_forced_park_moves_even_from_the_entry_origin(self) -> None:
        panel = _ParkPanel("https://tickets.interpark.com/goods/26012694")
        self.assertTrue(_park_for_entry(panel, "26012694", force=True))
        self.assertEqual(panel.browser.navigated, ["https://tickets.interpark.com/goods/26012694"])

    def test_unforced_park_is_skipped_on_the_entry_origin(self) -> None:
        panel = _ParkPanel("https://tickets.interpark.com/goods/26012694")
        self.assertFalse(_park_for_entry(panel, "26012694"))
        self.assertEqual(panel.browser.navigated, [])

    def test_nol_page_is_always_parked(self) -> None:
        panel = _ParkPanel("https://nol.yanolja.com/ticket/products/26012694")
        self.assertTrue(_park_for_entry(panel, "26012694"))
