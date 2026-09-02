"""Windows: WebView2, CoreWebView2.CookieManager, msvcrt locking.

Imports cleanly on macOS. Every native call is behind a small function so the
logic above it can be tested on a Mac with those calls faked — which is the only
way any of this is exercised before it reaches a Windows machine.

Two things here differ from macOS in ways that bite if missed:

  * WebView2 has no `removeAllUserScripts()`. `AddScriptToExecuteOnDocumentCreated`
    hands back an id, and the previous script has to be removed by that id or
    every `reload_autopilot` stacks another copy of a 325 KB script that runs on
    every document creation.
  * Calls must be marshalled to the thread that created the control. The
    `Invoke(Func[Object](...)) + ContinueWith(..., syncContextTaskScheduler)`
    pattern below is lifted from pywebview's own `evaluate_js`, because this is
    the one place a proven pattern in this exact stack beats inventing one.
"""

from __future__ import annotations

import contextlib
import platform
import time
from threading import Semaphore
from typing import Any, Iterator

NAME = "windows"

_WEBVIEW_WAIT_TRIES = 60
_WEBVIEW_WAIT_SECONDS = 0.05
_CALL_TIMEOUT_SECONDS = 10.0

WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"

# The document-start script currently registered, per window. WebView2 gives no
# way to enumerate them, so the id has to be remembered on this side.
_script_ids: dict[Any, str] = {}


def _clr() -> Any:
    """The CLR types the marshalling needs. Isolated so tests can fake it."""
    from System import Action, Func, Object, String  # type: ignore[import-not-found]
    from System.Threading.Tasks import Task  # type: ignore[import-not-found]

    return Action, Func, Object, String, Task


def _browser(window: Any) -> Any:
    """pywebview's EdgeChrome wrapper for this window, once it exists."""
    from webview.platforms import winforms  # type: ignore[import-not-found]

    for _ in range(_WEBVIEW_WAIT_TRIES):
        form = winforms.BrowserView.instances.get(window.uid)
        browser = getattr(form, "browser", None) if form is not None else None
        control = getattr(browser, "webview", None) if browser is not None else None
        # CoreWebView2 is null until initialisation completes — the same race the
        # macOS side waits out for `.webview`.
        if control is not None and getattr(control, "CoreWebView2", None) is not None:
            return browser
        time.sleep(_WEBVIEW_WAIT_SECONDS)
    return None


def _await_on_ui(browser: Any, start_task: Any, timeout: float = _CALL_TIMEOUT_SECONDS) -> Any:
    """Run `start_task()` on the UI thread and wait for its Task to finish."""
    Action, Func, Object, _String, Task = _clr()
    done = Semaphore(0)
    box: dict[str, Any] = {}

    def _on_ui() -> Any:
        return start_task().ContinueWith(
            Action[Task[Object]](lambda task: (box.__setitem__("result", task.Result), done.release())),
            browser.syncContextTaskScheduler,
        )

    browser.webview.Invoke(Func[Object](_on_ui))
    if not done.acquire(timeout=timeout):
        raise TimeoutError("WebView2 call did not complete")
    return box.get("result")


def prepare_display() -> None:
    """Tell Windows this process scales itself, before any window exists.

    Without it Tk is handed a virtualised resolution on a scaled display: point
    sizes grow with the DPI while every fixed pixel padding in the panel does
    not, so the fonts outgrow the boxes they sit in. It has to run before the Tk
    root is created — after that the process DPI mode is already fixed.
    """
    try:
        import ctypes

        # PROCESS_PER_MONITOR_DPI_AWARE where it exists (Win8.1+), falling back
        # to the older system-wide call.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a mis-scaled panel beats no panel
        return None
    return None


# The WebView2 Runtime's own installer-detection GUID, the same one Microsoft's
# own sample code checks — not something specific to this app.
_WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def _webview2_runtime_version() -> str:
    """The installed WebView2 Runtime's version, or "" if none is found.

    Checked directly against the registry rather than trusting that the Python
    bindings loading means the runtime is present — they do not depend on each
    other. edgechromium.py's interop DLLs are bundled with this app and load
    fine regardless of whether msedgewebview2.exe exists on the machine; the
    runtime itself is only touched later, inside EnsureCoreWebView2Async — and
    pywebview's own handler for that call failing is a bare `logger.error(...)`
    with no exception raised. Measured live: a panel that reported everything
    fine, next to a 예매창 that never rendered anything, because ensure_ready()
    had only verified the bindings, which is not the same claim as "this will
    work" and was quietly standing in for it.

    Checks the three locations Microsoft's own installation-detection sample
    checks: the per-machine key under both hives a 64-bit Windows install can
    land in, and the per-user key for an install made without admin rights.
    """
    try:
        import winreg
    except ImportError:
        return ""  # not Windows; callers only reach here on Windows

    for hive, subkey in (
        (winreg.HKEY_LOCAL_MACHINE,
         f"SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{_WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE,
         f"SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\{_WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_CURRENT_USER,
         f"SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\{_WEBVIEW2_CLIENT_GUID}"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        # An empty or all-zero version is how the key can exist without the
        # runtime actually being installed — not hypothetical, it is exactly
        # what Microsoft's own detection sample checks for.
        if version and str(version) != "0.0.0.0":
            return str(version)
    return ""


def ensure_ready() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("app_platform.windows loaded on a non-Windows system")
    try:
        import clr  # noqa: F401  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"pythonnet(clr)을 불러오지 못했습니다: {exc}") from exc
    try:
        from webview.platforms import edgechromium  # noqa: F401  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        # This is what a missing WebView2 runtime *can* look like, but it is
        # not the only shape the failure takes — see _webview2_runtime_version.
        raise RuntimeError(
            "WebView2 런타임이 없습니다. 아래 주소에서 'Evergreen Bootstrapper'를 "
            f"설치한 뒤 다시 실행하세요:\n{WEBVIEW2_DOWNLOAD}\n({exc})"
        ) from exc
    if not _webview2_runtime_version():
        # The bindings loaded, but the native runtime itself is not there — the
        # failure a bundled interop DLL cannot see. Left unchecked, this is the
        # one that produces a healthy-looking panel next to a blank 예매창.
        raise RuntimeError(
            "WebView2 런타임을 찾지 못했습니다. 아래 주소에서 'Evergreen Bootstrapper'를 "
            f"설치한 뒤 다시 실행하세요:\n{WEBVIEW2_DOWNLOAD}"
        )


def install_document_start_script(window: Any, source: str) -> bool:
    browser = _browser(window)
    if browser is None:
        raise RuntimeError(f"window {window.uid!r} has no WebView2 yet")
    core = browser.webview.CoreWebView2

    previous = _script_ids.pop(window.uid, None)
    if previous is not None:
        # Before adding, never after: a failure between the two must not leave
        # two copies registered.
        core.RemoveScriptToExecuteOnDocumentCreated(previous)

    script_id = _await_on_ui(
        browser, lambda: core.AddScriptToExecuteOnDocumentCreatedAsync(source)
    )
    if script_id is None:
        raise RuntimeError("WebView2 returned no script id")
    _script_ids[window.uid] = str(script_id)
    return True


def cookie_row_from(cookie: Any) -> dict[str, Any]:
    """One CoreWebView2Cookie in the shape browser_session already stores."""
    row: dict[str, Any] = {
        "Name": str(cookie.Name),
        "Value": str(cookie.Value),
        "Domain": str(cookie.Domain),
        "Path": str(cookie.Path),
    }
    expires = float(getattr(cookie, "Expires", -1) or -1)
    # WebView2 reports a session cookie as -1; the jar records it by omission,
    # which is what the macOS side does and what restore() expects.
    if expires > 0:
        row["Expires"] = expires
    if bool(getattr(cookie, "IsSecure", False)):
        row["Secure"] = "TRUE"
    if bool(getattr(cookie, "IsHttpOnly", False)):
        row["HttpOnly"] = True
    return row


def cookie_from_row(manager: Any, row: dict[str, Any]) -> Any:
    cookie = manager.CreateCookie(
        str(row.get("Name") or ""),
        str(row.get("Value") or ""),
        str(row.get("Domain") or ""),
        str(row.get("Path") or "/"),
    )
    expires = row.get("Expires")
    if expires:
        cookie.Expires = float(expires)
    if row.get("Secure"):
        cookie.IsSecure = True
    if row.get("HttpOnly"):
        cookie.IsHttpOnly = True
    return cookie


class _CookieStore:
    """The subset of WKHTTPCookieStore that browser_session actually uses."""

    def __init__(self, browser: Any) -> None:
        self._browser = browser
        self._manager = browser.webview.CoreWebView2.CookieManager

    def all_cookies(self, url: str | None = None) -> list[dict[str, Any]]:
        cookies = _await_on_ui(self._browser, lambda: self._manager.GetCookiesAsync(url))
        return [cookie_row_from(c) for c in (cookies or [])]

    def set_cookie(self, row: dict[str, Any]) -> None:
        self._manager.AddOrUpdateCookie(cookie_from_row(self._manager, row))


def cookie_store(window: Any) -> Any | None:
    browser = _browser(window)
    if browser is None:
        return None
    return _CookieStore(browser)


def dump_cookies(window: Any, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    store = cookie_store(window)
    if store is None:
        raise RuntimeError("WebView2 쿠키 저장소에 접근할 수 없습니다")
    return store.all_cookies(None)


def restore_cookies(window: Any, rows: list[dict[str, Any]]) -> int:
    store = cookie_store(window)
    if store is None:
        raise RuntimeError("WebView2 쿠키 저장소에 접근할 수 없습니다")
    restored = 0
    for row in rows:
        if not row.get("Name") or not row.get("Domain"):
            continue
        store.set_cookie(row)
        restored += 1
    return restored


def _msvcrt() -> Any:
    import msvcrt  # type: ignore[import-not-found]

    return msvcrt


# msvcrt.locking is byte-range, not whole-file like flock, so both sides agree on
# one sentinel byte. The lock file exists only to be locked; nothing reads it.
_LOCK_BYTES = 1


def lock_exclusive(handle: Any) -> None:
    msvcrt = _msvcrt()
    handle.seek(0)
    # LK_LOCK retries for ~10s and then raises, which is the behaviour we want:
    # blocking, but not forever if the other process wedged.
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, _LOCK_BYTES)


def unlock(handle: Any) -> None:
    msvcrt = _msvcrt()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)


@contextlib.contextmanager
def timing_precision() -> Iterator[None]:
    """Ask Windows for a 1 ms timer while a sync is running.

    Recovered from the deleted Windows edition (commit 8f285dd). The clock sync
    polls with `time.sleep(0.005)`; Windows' default timer granularity is
    ~15.6 ms, which widens the very second-boundary bracket the sync exists to
    tighten and can stop it ever meeting its own accuracy gate.
    """
    enabled = False
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)  # type: ignore[attr-defined]
        enabled = True
    except Exception:  # noqa: BLE001 - precision is an optimisation, not a requirement
        enabled = False
    try:
        yield
    finally:
        if enabled:
            with contextlib.suppress(Exception):
                import ctypes

                ctypes.windll.winmm.timeEndPeriod(1)  # type: ignore[attr-defined]
