"""The panel and the in-page autopilot must agree on the seat config.

They are two hand-maintained lists in two languages, and they drifted: `mode`
was written by the panel and ignored by the JS, `refresh_ms` and `instant` were
declared on both sides and read by neither, and three keys the panel sent were
missing from the JS defaults — those worked only because every consumer happened
to be written defensively against `undefined`.

Nothing else links the two layers, so this test is the link.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from core.seat import SeatPreferences

AUTOPILOT = Path(__file__).resolve().parent.parent / "browser" / "nolsniper_autopilot.js"

# `enabled` is synthesised by to_mapping() rather than being a dataclass field.
PANEL_ONLY: frozenset[str] = frozenset({"enabled"})


def default_seat_config_keys() -> set[str]:
    """The keys declared in the JS DEFAULT_SEAT_CONFIG literal."""
    source = AUTOPILOT.read_text(encoding="utf-8")
    match = re.search(r"const DEFAULT_SEAT_CONFIG = \{(.*?)\n  \};", source, re.S)
    if not match:  # pragma: no cover - only if the literal is restructured
        raise AssertionError("DEFAULT_SEAT_CONFIG literal not found in the autopilot")
    # Top-level keys only: two-space indent inside the literal, skipping comments.
    return set(re.findall(r"^    ([a-z_][a-z0-9_]*):", match.group(1), re.M))


class ConfigContractTests(unittest.TestCase):
    def test_every_key_the_panel_sends_is_declared_in_the_autopilot(self) -> None:
        sent = set(SeatPreferences().to_mapping()) - PANEL_ONLY
        declared = default_seat_config_keys()
        missing = sorted(sent - declared)
        self.assertEqual(
            missing,
            [],
            "the panel sends keys the autopilot never declares, so they are "
            f"undefined whenever localStorage is empty: {missing}",
        )

    def test_the_autopilot_declares_nothing_the_panel_never_sends(self) -> None:
        sent = set(SeatPreferences().to_mapping()) - PANEL_ONLY
        declared = default_seat_config_keys() - {"enabled"}
        orphaned = sorted(declared - sent)
        self.assertEqual(
            orphaned,
            [],
            f"the autopilot declares settings nothing can ever set: {orphaned}",
        )

    def test_seat_strategy_values_agree_across_both_layers(self) -> None:
        from core.seat import VALID_STRATEGIES

        source = AUTOPILOT.read_text(encoding="utf-8")
        match = re.search(r"const SEAT_STRATEGIES = \[(.*?)\];", source, re.S)
        self.assertIsNotNone(match, "SEAT_STRATEGIES not found in the autopilot")
        js_values = set(re.findall(r'"([a-z]+)"', match.group(1)))
        self.assertEqual(
            js_values,
            set(VALID_STRATEGIES),
            "a strategy the panel accepts but the autopilot does not know falls "
            "back to 'stage', silently ignoring the user's choice",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# The functions that render the seat *status*. Others in core/seat.py also take
# a parameter called `seat` but are handed one seat object, not the payload.
STATUS_CONSUMERS: frozenset[str] = frozenset(
    {"running_hint", "seat_order_lines", "map_move_lines"}
)

SEAT_MODULE = Path(__file__).resolve().parent.parent / "core" / "seat.py"


def published_status_keys() -> set[str]:
    """Top-level keys of the `seat:` object in seatStatusSummary()."""
    source = AUTOPILOT.read_text(encoding="utf-8")
    start = source.index("function seatStatusSummary()")
    literal = source[source.index("seat: {", start) : source.index(
        "      arm: { ...armState },", start
    )]
    return set(re.findall(r"^\s{8}([A-Za-z_][A-Za-z0-9_]*):", literal, re.M))


def panel_status_reads() -> dict[str, str]:
    """`seat.get("X")` in each status-rendering function, mapped to its function."""
    tree = ast.parse(SEAT_MODULE.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in STATUS_CONSUMERS:
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "seat"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.setdefault(node.args[0].value, fn.name)
    return found


class StatusContractTests(unittest.TestCase):
    """The panel must not render a field the page never sends.

    This is the app's most repeated bug: a value computed in one layer and
    consumed in the other with nothing linking them, so the line simply never
    appears and nobody finds out. `nudges` sat in running_hint being read from a
    payload that has never carried it; `unreachableSkips` was incremented for a
    whole fix before anyone noticed it was not published. Both are the same
    mistake, and this is the link that catches it.
    """

    def test_every_status_field_the_panel_renders_is_published(self) -> None:
        published = published_status_keys()
        self.assertGreater(len(published), 20, "the status literal failed to parse")
        missing = sorted(
            f"{key} (read in {where})"
            for key, where in panel_status_reads().items()
            if key not in published
        )
        self.assertEqual(
            missing,
            [],
            "the panel reads status fields the page never sends, so those lines "
            "can never appear:\n  " + "\n  ".join(missing),
        )
