"""macOS: WKWebView, WKHTTPCookieStore, flock.

Imports cleanly on any OS — the Cocoa imports are inside the functions that need
them, so a Windows machine can import this module (and the tests can reason
about it) without pyobjc present.
"""

from __future__ import annotations

import contextlib
import platform
from typing import Any, Iterator

NAME = "darwin"

# Milliseconds a WKUserScript takes to register is not the problem; the webview
# not existing yet is. Both platforms race window creation the same way.
_WEBVIEW_WAIT_TRIES = 60
_WEBVIEW_WAIT_SECONDS = 0.05


def prepare_display() -> None:
    """Nothing to do: Tk on macOS is already told the truth about the display."""
    return None


def disable_gpu_rendering() -> None:
    """Nothing to do: this fixes a WebView2-specific GPU compositing failure.

    WKWebView on macOS has no equivalent bug this stands in for.
    """
    return None


def ensure_ready() -> None:
    """Raise with something actionable if this machine cannot run the app."""
    if platform.system() != "Darwin":
        raise RuntimeError("app_platform.darwin loaded on a non-macOS system")
    try:
        from webview.platforms import cocoa  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - the message is the point
        raise RuntimeError(
            "macOS 브라우저 엔진(pywebview/pyobjc)을 불러오지 못했습니다: "
            f"{exc}"
        ) from exc


def _instance(window: Any) -> Any:
    import time

    from webview.platforms import cocoa

    # The window and its webview land separately, and `shown` can beat both.
    for _ in range(_WEBVIEW_WAIT_TRIES):
        instance = cocoa.BrowserView.instances.get(window.uid)
        if instance is not None and getattr(instance, "webview", None) is not None:
            return instance
        time.sleep(_WEBVIEW_WAIT_SECONDS)
    return None


def install_document_start_script(window: Any, source: str) -> bool:
    """Register `source` to run before any page script, on every navigation."""
    import WebKit

    instance = _instance(window)
    if instance is None:
        raise RuntimeError(f"window {window.uid!r} has no webview yet")
    controller = instance.webview.configuration().userContentController()
    # WKWebView can drop every previous script in one call; WebView2 cannot,
    # which is why the Windows side has to track ids. See app_platform.windows.
    controller.removeAllUserScripts()
    script = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
        source,
        0,  # WKUserScriptInjectionTimeAtDocumentStart
        False,  # all frames, not just the main one
    )
    controller.addUserScript_(script)
    return True


def cookie_store(window: Any) -> Any | None:
    instance = _instance(window)
    if instance is None:
        return None
    return instance.webview.configuration().websiteDataStore().httpCookieStore()


_STRING_KEYS = ("Name", "Value", "Domain", "Path", "Secure", "Version", "Comment")


def dump_cookies(window: Any, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Every cookie the browser holds, HttpOnly included."""
    from threading import Semaphore

    from PyObjCTools import AppHelper

    store = cookie_store(window)
    if store is None:
        raise RuntimeError("쿠키 저장소에 접근할 수 없습니다")

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
        raise TimeoutError("쿠키를 읽는 데 시간이 초과되었습니다")
    return rows


def restore_cookies(window: Any, rows: list[dict[str, Any]]) -> int:
    """Put a saved jar back. Must run before the first navigation."""
    import Foundation
    from PyObjCTools import AppHelper

    store = cookie_store(window)
    if store is None:
        raise RuntimeError("쿠키 저장소에 접근할 수 없습니다")

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


def lock_exclusive(handle: Any) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def unlock(handle: Any) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def timing_precision() -> Iterator[None]:
    """No-op. macOS already sleeps at roughly the resolution asked for."""
    yield
