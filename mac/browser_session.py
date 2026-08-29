"""Keep the 예매 창 logged in across restarts.

WKWebView's default data store reports `isPersistent() == True`, but a plain
Python process does not actually get its jar back on relaunch — measured: a
cookie written in one run, closed gracefully with 20s to settle, is gone in the
next run. Without this module every launch starts logged out, and NOL's Naver
button is a full OAuth redirect carrying `auth_type=reauthenticate`, so "logged
out" means a real Naver login, by hand, every single time.

So the jar is carried across launches explicitly, through WKHTTPCookieStore.
That also preserves `cf_clearance`, without which Cloudflare re-challenges.

The file is a live session — anyone holding it is logged in as you. It is
written 0600 and is in .gitignore. Delete it to sign out.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Semaphore
from typing import Any

# Cookie fields worth carrying. `Expires` is an NSDate and needs converting;
# the rest round-trip as strings.
_STRING_KEYS = ("Name", "Value", "Domain", "Path", "Secure", "Version", "Comment")


def _cookie_store(window: Any) -> Any | None:
    """The live WKHTTPCookieStore behind a pywebview window, or None."""
    try:
        from webview.platforms import cocoa

        instance = cocoa.BrowserView.instances[window.uid]
        return instance.webview.configuration().websiteDataStore().httpCookieStore()
    except Exception:
        return None


def dump(window: Any, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Every cookie the browser currently holds, including HttpOnly ones."""
    store = _cookie_store(window)
    if store is None:
        return []

    from PyObjCTools import AppHelper

    done = Semaphore(0)
    rows: list[dict[str, Any]] = []

    def handler(cookies) -> None:
        try:
            for cookie in cookies:
                properties = cookie.properties()
                row: dict[str, Any] = {}
                for key in _STRING_KEYS:
                    value = properties.objectForKey_(key)
                    if value is not None:
                        row[key] = str(value)
                expires = properties.objectForKey_("Expires")
                if expires is not None:
                    row["Expires"] = float(expires.timeIntervalSince1970())
                if properties.objectForKey_("HttpOnly") is not None:
                    row["HttpOnly"] = True
                if row.get("Name"):
                    rows.append(row)
        finally:
            done.release()

    AppHelper.callAfter(lambda: store.getAllCookies_(handler))
    if not done.acquire(timeout=timeout):
        return []
    return rows


def restore(window: Any, rows: list[dict[str, Any]]) -> int:
    """Put a saved jar back. Must run before the first navigation."""
    store = _cookie_store(window)
    if store is None:
        return 0

    import Foundation
    from PyObjCTools import AppHelper

    restored = 0
    for row in rows:
        properties = Foundation.NSMutableDictionary.dictionary()
        for key in _STRING_KEYS:
            if row.get(key) is not None:
                properties.setObject_forKey_(row[key], key)
        if row.get("Expires"):
            properties.setObject_forKey_(
                Foundation.NSDate.dateWithTimeIntervalSince1970_(row["Expires"]), "Expires"
            )
        if row.get("HttpOnly"):
            properties.setObject_forKey_("TRUE", "HttpOnly")
        cookie = Foundation.NSHTTPCookie.cookieWithProperties_(properties)
        if cookie is None:
            continue
        AppHelper.callAfter(lambda c=cookie: store.setCookie_completionHandler_(c, None))
        restored += 1
    return restored


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
