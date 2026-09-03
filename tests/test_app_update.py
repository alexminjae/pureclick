"""The updater, and specifically what it refuses.

This downloads JavaScript that runs inside a page holding a live Naver login, so
the only interesting cases are the ones where it must NOT use what it fetched.
Every failure has to reach the panel: silent fallback is this app's recurring
failure mode, and an unverified script must never look like a verified one.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app_update  # noqa: E402

SOURCE = "// autopilot v2\n"
GOOD_SHA = app_update.sha256_of(SOURCE)
MANIFEST_URL = "https://example.invalid/manifest.json"
JS_URL = "https://example.invalid/nolsniper_autopilot.js"


def manifest(**over) -> dict:
    base = {
        "app_version": "1.0.0",
        "releases_url": "https://example.invalid/releases",
        "autopilot_sha256": GOOD_SHA,
        "autopilot_url": JS_URL,
    }
    base.update(over)
    return base


def opener_for(man: dict, body: str = SOURCE, *, fail: str = ""):
    def opener(url: str, _timeout: float) -> bytes:
        if fail == "manifest" and url == MANIFEST_URL:
            raise OSError("offline")
        if fail == "download" and url == JS_URL:
            raise OSError("connection reset")
        if url == MANIFEST_URL:
            return json.dumps(man).encode("utf-8")
        return body.encode("utf-8")
    return opener


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "NOLSniper" / "nolsniper_autopilot.js"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _check(self, man, body=SOURCE, *, fail="", current="1.0.0"):
        return app_update.check(MANIFEST_URL, current_version=current,
                                cache_path=self.cache, opener=opener_for(man, body, fail=fail))

    def test_a_matching_checksum_is_accepted_and_cached(self) -> None:
        status = self._check(manifest())
        self.assertEqual(status.autopilot, "updated")
        self.assertEqual(self.cache.read_text(encoding="utf-8"), SOURCE)

    def test_a_mismatched_checksum_is_refused_and_nothing_is_written(self) -> None:
        """The whole point. A tampered script must never reach the page."""
        status = self._check(manifest(), body="// something else entirely\n")
        self.assertEqual(status.autopilot, "refused")
        self.assertFalse(self.cache.exists(), "a refused script must not be cached")
        self.assertIn("검증 실패", status.note)

    def test_being_offline_falls_back_and_says_so(self) -> None:
        status = self._check(manifest(), fail="manifest")
        self.assertEqual(status.autopilot, "bundled")
        self.assertTrue(status.note.strip(), "a silent fallback is the bug, not the feature")

    def test_a_failed_download_falls_back_and_says_so(self) -> None:
        status = self._check(manifest(), fail="download")
        self.assertEqual(status.autopilot, "failed")
        self.assertFalse(self.cache.exists())
        self.assertTrue(status.note.strip())

    def test_a_newer_app_version_is_reported_but_never_installed(self) -> None:
        status = self._check(manifest(app_version="1.4.0"), current="1.0.0")
        self.assertEqual(status.app_update, "1.4.0")
        self.assertIn("새 버전", status.note)
        self.assertEqual(status.releases_url, "https://example.invalid/releases")

    def test_the_same_version_is_not_reported_as_an_update(self) -> None:
        self.assertEqual(self._check(manifest(app_version="1.0.0")).app_update, "")

    def test_versions_compare_numerically(self) -> None:
        # "1.10.0" < "1.9.0" as strings, which would hide every release past .9
        self.assertTrue(app_update.is_newer("1.10.0", "1.9.0"))
        self.assertFalse(app_update.is_newer("1.9.0", "1.10.0"))
        self.assertTrue(app_update.is_newer("v2.0.0", "1.99.99"))

    def test_an_already_current_cache_is_not_downloaded_again(self) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(SOURCE, encoding="utf-8")

        calls: list[str] = []

        def counting(url: str, timeout: float) -> bytes:
            calls.append(url)
            return opener_for(manifest())(url, timeout)

        status = app_update.check(MANIFEST_URL, current_version="1.0.0",
                                  cache_path=self.cache, opener=counting)
        self.assertEqual(status.autopilot, "updated")
        self.assertNotIn(JS_URL, calls, "no reason to re-download a script we already have")

    def test_a_manifest_without_a_script_still_reports_the_app_version(self) -> None:
        status = self._check(manifest(app_version="2.0.0", autopilot_sha256="", autopilot_url=""))
        self.assertEqual(status.autopilot, "bundled")
        self.assertEqual(status.app_update, "2.0.0")

    def test_a_refused_update_leaves_an_existing_good_cache_alone(self) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text("// known good\n", encoding="utf-8")
        self._check(manifest(), body="// tampered\n")
        self.assertEqual(self.cache.read_text(encoding="utf-8"), "// known good\n")


class VersionTagTests(unittest.TestCase):
    """The panel's window title, and the whole point of showing it at all.

    Six point releases went out in one troubleshooting session as
    identically-named zip files, sent back and forth by hand. "Still broken"
    and "still running the version from before the fix" render identically
    from outside the app — this is what removes that ambiguity, so it has to
    actually reach the screen, not just exist as a string somewhere.
    """

    def setUp(self) -> None:
        self._env_before = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_before)

    def test_an_unstamped_checkout_reads_as_dev_not_as_a_confusing_zero(self) -> None:
        os.environ.pop("NOLSNIPER_VERSION", None)
        self.assertEqual(app_update.version_tag(), "(dev)")

    def test_a_tagged_release_shows_its_number(self) -> None:
        os.environ["NOLSNIPER_VERSION"] = "0.1.6"
        self.assertEqual(app_update.version_tag(), "(v0.1.6)")

    def test_a_branch_build_still_shows_something_specific(self) -> None:
        os.environ["NOLSNIPER_VERSION"] = "0.0.0+abc1234"
        self.assertEqual(app_update.version_tag(), "(v0.0.0+abc1234)")

    def test_the_panel_actually_puts_this_in_its_title(self) -> None:
        """A helper nobody calls is exactly as useful as not having one."""
        source = (ROOT / "mac" / "nolsniper.py").read_text(encoding="utf-8")
        self.assertIn("app_update.version_tag()", source,
                      "version_tag must reach the window title, not just exist")
