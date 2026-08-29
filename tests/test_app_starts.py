"""Every name the panel imports at startup actually exists.

Written after deleting the Windows edition removed `ensure_mac_ready` along
with the dead code above it — `mac/pureclick.py` imports that on line 24, so
the panel would have failed to start with an ImportError and no test would have
noticed. The suite covered the logic modules thoroughly and the wiring not at
all.

This is deliberately dumb: it imports what the app imports and checks the names
resolve. It cannot catch a runtime bug, but it catches a deletion.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAC = ROOT / "mac"
for path in (ROOT, MAC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _declared_imports(source: Path) -> list[tuple[str, str]]:
    """Every `from <module> import <name>` in a file, as (module, name)."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                found.append((node.module, alias.name))
    return found


# The two files that actually run: the panel and the browser host it launches.
ENTRY_POINTS = [MAC / "pureclick.py", MAC / "browser_host.py"]
# Third-party and stdlib are the environment's problem, not ours.
OURS = {
    "browser_bridge", "browser_host", "browser_session", "pureclick_arm_core",
    "pureclick_catalog", "pureclick_core", "pureclick_mac_core",
    "pureclick_seat_core", "pureclick_showinfo", "pureclick_zone_map",
}


class AppStartsTest(unittest.TestCase):
    def test_every_imported_name_resolves(self) -> None:
        checked = 0
        for entry in ENTRY_POINTS:
            self.assertTrue(entry.exists(), f"{entry} is gone")
            for module_name, name in _declared_imports(entry):
                if module_name not in OURS:
                    continue
                module = __import__(module_name)
                self.assertTrue(
                    hasattr(module, name),
                    f"{entry.name} imports {name} from {module_name}, which no longer has it",
                )
                checked += 1
        self.assertGreater(checked, 10, "the scan found almost nothing; it is not working")

    def test_the_autopilot_script_the_host_injects_exists(self) -> None:
        # browser_host reads this off disk at document-start injection time, so
        # a rename would not surface until a page loaded.
        script = ROOT / "browser" / "pureclick_autopilot.js"
        self.assertTrue(script.exists(), "the injected autopilot script is missing")
        self.assertIn("AUTOPILOT_BUILD", script.read_text(encoding="utf-8")[:2000])


if __name__ == "__main__":
    unittest.main()
