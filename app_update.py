"""Tell the user about a newer build, and refresh the autopilot safely.

A downloaded exe is a snapshot. But the file that changes most is not the Python
— over the last 60 days `browser/nolsniper_autopilot.js` had 18 commits against
15 for the panel — because it tracks someone else's markup. So the Python shell
ships in the binary and the automation can be refreshed in place.

Two rules this module exists to keep:

  1. **Nothing is used before its SHA-256 matches the manifest.** The download is
     JavaScript that runs inside a page holding a live Naver login. The checksum
     refuses a tampered, truncated or corrupted file. It does *not* protect
     against whoever can publish to the repo — that is the trust root, and
     anyone running the exe is trusting it.
  2. **Every failure is reported, never silent.** Offline, bad hash, unreachable
     manifest — all fall back to the bundled autopilot and say so. Silent
     fallback is this app's recurring failure mode, and an unverified update must
     never look like a verified one.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Where the running app looks for the release manifest. The workflow publishes it
# beside NOLSniper.exe. Empty means "no update source configured", and the check
# is then skipped in silence rather than reporting a failure that is not one —
# which is the state of a source checkout with no remote.
def _stamped(name: str, default: str = "") -> str:
    """Read a value the build wrote next to the app.

    A frozen build unpacks to sys._MEIPASS; a source checkout reads from the
    repo. An environment variable overrides both, which is how this gets tested
    without a release.
    """
    override = os.environ.get(f"NOLSNIPER_{name}")
    if override:
        return override.strip()
    for base in (getattr(sys, "_MEIPASS", None), Path(__file__).resolve().parent):
        if not base:
            continue
        candidate = Path(base) / name
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip() or default
        except OSError:
            pass
    return default


def manifest_url() -> str:
    """Where to look for the release manifest.

    Empty means no update source is configured, and the check is then skipped in
    silence rather than reporting a failure that is not one — which is exactly
    the state of a source checkout with no remote.
    """
    return _stamped("UPDATE_URL")


def app_version() -> str:
    """The build's version.

    Written into VERSION by the Windows workflow. A source checkout has no such
    file, and 0.0.0 there is right: every published build is newer, which is
    true and harmless because nothing auto-installs.
    """
    return _stamped("VERSION", "0.0.0")


def version_tag() -> str:
    """`(v1.2.3)`, ready to put on screen.

    A build with no VERSION file — a source checkout — is `app_version() ==
    "0.0.0"` exactly; anything else, tagged release or a bare-branch build
    stamped `0.0.0+<sha>`, is shown as-is. The point is not the format so much
    as that this is visible at all: "still broken" and "still on the version
    from before the fix" are indistinguishable from the outside without it.
    """
    version = app_version()
    return "(dev)" if version == "0.0.0" else f"(v{version})"


MANIFEST_TIMEOUT = 6.0
DOWNLOAD_TIMEOUT = 20.0
MAX_AUTOPILOT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class UpdateStatus:
    """What the check found. `note` is what the panel shows; it is never empty."""

    app_update: str = ""        # a newer exe version, or ""
    releases_url: str = ""
    autopilot: str = "bundled"  # "bundled" | "updated" | "refused" | "failed"
    note: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "app_update": self.app_update,
            "releases_url": self.releases_url,
            "autopilot": self.autopilot,
            "note": self.note,
        }


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _default_opener(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read(MAX_AUTOPILOT_BYTES + 1)


def version_tuple(value: str) -> tuple[int, ...]:
    """`1.10.0` sorts after `1.9.0`. String comparison gets that backwards."""
    parts: list[int] = []
    for chunk in str(value or "").strip().lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def check(
    manifest_url: str,
    *,
    current_version: str,
    cache_path: Path,
    opener: Callable[[str, float], bytes] | None = None,
) -> UpdateStatus:
    """Look once. Never raises — the caller is a worker thread on startup."""
    fetch = opener or _default_opener

    try:
        raw = fetch(manifest_url, MANIFEST_TIMEOUT)
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - offline is normal, not exceptional
        return UpdateStatus(
            autopilot="bundled",
            note=f"업데이트 확인 실패 (내장 버전으로 실행): {str(exc)[:70]}",
        )

    releases = str(manifest.get("releases_url") or "")
    newest = str(manifest.get("app_version") or "")
    app_update = newest if newest and is_newer(newest, current_version) else ""

    wanted = str(manifest.get("autopilot_sha256") or "").lower()
    url = str(manifest.get("autopilot_url") or "")
    if not wanted or not url:
        return UpdateStatus(app_update, releases, "bundled",
                            _note(app_update, newest, "자동화 스크립트 정보가 없어 내장 버전을 씁니다"))

    # Already have this exact one.
    try:
        if cache_path.is_file() and sha256_of(cache_path.read_text(encoding="utf-8")) == wanted:
            return UpdateStatus(app_update, releases, "updated",
                                _note(app_update, newest, "자동화 스크립트 최신"))
    except OSError:
        pass  # unreadable cache is the same as no cache

    try:
        payload = fetch(url, DOWNLOAD_TIMEOUT)
        if len(payload) > MAX_AUTOPILOT_BYTES:
            raise ValueError("autopilot payload is implausibly large")
        source = payload.decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return UpdateStatus(app_update, releases, "failed",
                            _note(app_update, newest,
                                  f"자동화 스크립트를 받지 못했습니다 (내장 버전 사용): {str(exc)[:60]}"))

    got = sha256_of(source)
    if got != wanted:
        # The one case that must never be papered over.
        return UpdateStatus(app_update, releases, "refused",
                            _note(app_update, newest,
                                  "자동화 스크립트 검증 실패 — 내장 버전으로 실행합니다"))

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_name(cache_path.name + ".tmp")
        temp.write_text(source, encoding="utf-8")
        temp.replace(cache_path)
    except OSError as exc:
        return UpdateStatus(app_update, releases, "failed",
                            _note(app_update, newest,
                                  f"자동화 스크립트를 저장하지 못했습니다: {str(exc)[:60]}"))

    return UpdateStatus(app_update, releases, "updated",
                        _note(app_update, newest, "자동화 스크립트를 새로 받았습니다"))


def _note(app_update: str, newest: str, tail: str) -> str:
    if app_update:
        return f"새 버전 {newest} 이(가) 있습니다 · {tail}"
    return tail
