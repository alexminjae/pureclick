"""Which browser the Chrome host picks, and in what order.

Reported from a real Windows machine: Edge opened while Chrome was installed.
The search looped install location first and browser second, so a per-user
Chrome under %LOCALAPPDATA% — which is what an install without admin rights
produces — lost to an Edge in Program Files.

Edge works (same engine, same protocol), so this is a preference bug rather
than a breakage, but it is the kind that quietly picks the wrong browser
forever on the machines that hit it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mac"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cdp  # noqa: E402

ENV = {
    "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
    "PROGRAMFILES": r"C:\Program Files",
    "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
}

# Built the way find_chrome() builds them. Running on macOS, Path is PosixPath
# and joins these Windows fragments with a forward slash, so hand-written
# backslash constants would never match what the code produces.
def _p(root_key: str, rel: str) -> str:
    return str(Path(ENV[root_key]) / rel)


CHROME_USER = _p("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe")
CHROME_MACHINE = _p("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe")
EDGE = _p("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe")


def _find_with(present: set[str], env: dict[str, str] | None = None) -> str | None:
    """Run find_chrome() as if `present` were the only files on a Windows box."""
    def exists(self: Path) -> bool:
        return str(self) in present

    with mock.patch.object(cdp.platform, "system", return_value="Windows"), \
         mock.patch.dict(cdp.os.environ, {**ENV, **(env or {})}, clear=True), \
         mock.patch.object(Path, "exists", exists), \
         mock.patch.object(cdp.shutil, "which", return_value=None):
        return cdp.find_chrome()


class BrowserPreferenceTests(unittest.TestCase):
    def test_per_user_chrome_beats_program_files_edge(self) -> None:
        """The reported bug, as a test."""
        self.assertEqual(_find_with({CHROME_USER, EDGE}), CHROME_USER)

    def test_machine_wide_chrome_also_beats_edge(self) -> None:
        self.assertEqual(_find_with({CHROME_MACHINE, EDGE}), CHROME_MACHINE)

    def test_edge_is_used_when_chrome_is_genuinely_absent(self) -> None:
        """Edge is a working fallback, not a failure — every Windows box has it."""
        self.assertEqual(_find_with({EDGE}), EDGE)

    def test_nothing_installed_is_none_not_a_guess(self) -> None:
        self.assertIsNone(_find_with(set()))

    def test_an_explicit_override_wins(self) -> None:
        self.assertEqual(
            _find_with({CHROME_USER, EDGE}, {"NOLSNIPER_BROWSER": EDGE}), EDGE
        )

    def test_an_override_pointing_at_nothing_does_not_silently_fall_back(self) -> None:
        """Falling back would hide the typo and look like the override worked."""
        missing = r"C:\nope\chrome.exe"
        self.assertIsNone(_find_with({CHROME_USER}, {"NOLSNIPER_BROWSER": missing}))


if __name__ == "__main__":
    unittest.main()
