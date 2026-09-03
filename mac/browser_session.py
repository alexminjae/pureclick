"""Keep the 예매 창 logged in across restarts.

WKWebView's default data store reports `isPersistent() == True`, but a plain
Python process does not actually get its jar back on relaunch — measured: a
cookie written in one run, closed gracefully with 20s to settle, is gone in the
next run. Without this module every launch starts logged out, and NOL's Naver
button is a full OAuth redirect carrying `auth_type=reauthenticate`, so "logged
out" means a real Naver login, by hand, every single time.

So the jar is carried across launches explicitly, through whichever cookie
store the platform has — WKHTTPCookieStore on macOS, CoreWebView2.CookieManager
on Windows, both behind app_platform. That also preserves `cf_clearance`,
without which Cloudflare re-challenges.

The file is a live session — anyone holding it is logged in as you. It is in
.gitignore, and on macOS it is written 0600. **On Windows chmod only toggles
the read-only bit and applies no ACL**, so there the file is readable by any
other account on the machine. Delete it to sign out.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import app_platform

# Cookie fields worth carrying. `Expires` is an NSDate and needs converting;
# the rest round-trip as strings.
# Cookie access is the one part of this that is per-platform: WKHTTPCookieStore
# on macOS, CoreWebView2.CookieManager on Windows. Both live in app_platform, so
# nothing here imports Foundation or pythonnet — see the guard in
# tests/test_platform_seam.py.
#
# What is shared is everything that matters to callers: the jar shape on disk and
# the fact that a failure is reported rather than looking like an empty jar. The
# old `_cookie_store` swallowed every exception and returned None, so on any
# platform it could not reach, `dump()` returned [] and `restore()` returned 0 —
# presented to the user as a working app that happens to be logged out, i.e. a
# full manual Naver login on every single launch with nothing said.
LAST_ERROR = ""


def dump(window: Any, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Every cookie the browser currently holds, including HttpOnly ones."""
    global LAST_ERROR
    try:
        rows = app_platform.dump_cookies(window, timeout=timeout)
        LAST_ERROR = ""
        return rows
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        LAST_ERROR = f"쿠키를 저장하지 못했습니다: {exc}"
        print(f"[nolsniper] {LAST_ERROR}", file=sys.stderr)
        return []


def restore(window: Any, rows: list[dict[str, Any]]) -> int:
    """Put a saved jar back. Must run before the first navigation."""
    global LAST_ERROR
    if not rows:
        return 0
    try:
        restored = app_platform.restore_cookies(window, rows)
        LAST_ERROR = ""
        return restored
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        LAST_ERROR = f"로그인 정보를 복원하지 못했습니다: {exc}"
        print(f"[nolsniper] {LAST_ERROR}", file=sys.stderr)
        return 0


def load_jar(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return rows if isinstance(rows, list) else []


def save_jar(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    path.chmod(0o600)
