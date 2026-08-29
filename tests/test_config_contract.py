"""The panel and the in-page autopilot must agree on the seat config.

They are two hand-maintained lists in two languages, and they drifted: `mode`
was written by the panel and ignored by the JS, `refresh_ms` and `instant` were
declared on both sides and read by neither, and three keys the panel sent were
missing from the JS defaults — those worked only because every consumer happened
to be written defensively against `undefined`.

Nothing else links the two layers, so this test is the link.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from pureclick_seat_core import SeatPreferences

AUTOPILOT = Path(__file__).resolve().parent.parent / "browser" / "pureclick_autopilot.js"

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
        from pureclick_seat_core import VALID_STRATEGIES

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
