"""The two 예매 창 hosts must drive the autopilot identically.

browser_host (pywebview: WKWebView on macOS, WebView2 on Windows) and
chrome_host (a real Chrome over CDP) deliberately duplicate the small JS
contract they share — importing one from the other would drag pywebview into a
host whose entire purpose is not to need it. Duplication is only safe if it is
checked, so this reads both files and compares.

The failure this prevents is quiet and expensive: a command added to one host
and not the other means a 조작판 button that works on one platform and silently
does nothing on the other.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAC = ROOT / "mac"


def _module_constants(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
    return out


class HostParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.webview = _module_constants(MAC / "browser_host.py")
        self.chrome = _module_constants(MAC / "chrome_host.py")

    def test_both_hosts_exist(self) -> None:
        self.assertTrue((MAC / "browser_host.py").is_file())
        self.assertTrue((MAC / "chrome_host.py").is_file())

    def test_the_command_table_matches(self) -> None:
        a = ast.literal_eval(self.webview["_COMMAND_JS"])
        b = ast.literal_eval(self.chrome["_COMMAND_JS"])
        self.assertEqual(
            a, b,
            "a 조작판 command that exists in one host and not the other is a button "
            "that silently does nothing on one platform",
        )

    def test_the_status_snapshot_matches(self) -> None:
        a = ast.literal_eval(self.webview["_SNAPSHOT_JS"])
        b = ast.literal_eval(self.chrome["_SNAPSHOT_JS"])
        self.assertEqual(a.strip(), b.strip())

    def test_both_start_at_the_same_url(self) -> None:
        self.assertEqual(
            ast.literal_eval(self.webview["START_URL"]),
            ast.literal_eval(self.chrome["START_URL"]),
        )

    def test_the_localstorage_keys_match_core(self) -> None:
        """The panel writes these; the autopilot reads them. Three copies."""
        chrome_src = (MAC / "chrome_host.py").read_text(encoding="utf-8")
        arm_src = (ROOT / "core" / "arm.py").read_text(encoding="utf-8")
        for key in ("nolsniper_arm_v1", "nolsniper_seat_v1"):
            self.assertIn(key, chrome_src, f"chrome_host must push {key}")
            self.assertIn(key, arm_src, f"core.arm must agree on {key}")


class ChromeHostIndependenceTests(unittest.TestCase):
    """chrome_host exists because the pywebview/pythonnet path can block a
    Python thread inside a .NET call while holding the GIL, freezing the whole
    interpreter. Importing any of that back in would reintroduce the very
    failure mode it was written to escape.
    """

    def test_chrome_host_does_not_import_pywebview_or_pythonnet(self) -> None:
        tree = ast.parse((MAC / "chrome_host.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("webview", "clr", "browser_host", "browser_session"):
            self.assertNotIn(
                banned, imported,
                f"chrome_host must not import {banned} — it exists to avoid that stack",
            )

    def test_cdp_waits_all_have_timeouts(self) -> None:
        """Every wait in cdp.py must be bounded. An unbounded one is the exact
        shape of the bug this module replaces.
        """
        source = (ROOT / "mac" / "cdp.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = ast.unparse(node.func)
            if func.endswith(".wait") or func.endswith(".acquire") or func.endswith(".join"):
                has_timeout = bool(node.args) or any(
                    kw.arg == "timeout" for kw in node.keywords
                )
                self.assertTrue(
                    has_timeout,
                    f"unbounded {func}() in cdp.py — every wait here must have a deadline",
                )


if __name__ == "__main__":
    unittest.main()
