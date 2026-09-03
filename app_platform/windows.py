"""Windows: WebView2, CoreWebView2.CookieManager, msvcrt locking.

Imports cleanly on macOS. Every native call is behind a small function so the
logic above it can be tested on a Mac with those calls faked — which is the only
way any of this is exercised before it reaches a Windows machine.

Three things here differ from macOS in ways that bite if missed:

  * WebView2 has no `removeAllUserScripts()`. `AddScriptToExecuteOnDocumentCreated`
    hands back an id, and the previous script has to be removed by that id or
    every `reload_autopilot` stacks another copy of a 325 KB script that runs on
    every document creation.
  * Calls must be marshalled to the thread that created the control. The
    `Invoke(Func[Object](...)) + ContinueWith(..., syncContextTaskScheduler)`
    pattern below is lifted from pywebview's own `evaluate_js`, because this is
    the one place a proven pattern in this exact stack beats inventing one —
    but only for a caller on a *different* thread from the control's. Called
    from the control's own thread it deadlocks: `Invoke` runs the delegate
    inline there, so the wait that follows blocks the one thread the
    `syncContextTaskScheduler` continuation needs in order to ever run, and
    nothing pumps windows messages while that wait sits there — which is
    `pywebview`'s `Shown`/`loaded` events firing synchronously, so every path
    that reacts to them and calls in here was reported live as the whole app
    going 응답 없음 (Not Responding). `_await_on_ui` and `_browser` below check
    `_message_loop_running()` and take a pumping path instead of blocking when
    they are already on that thread.
"""

from __future__ import annotations

import contextlib
import os
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
    from System.Threading.Tasks import Task, TaskScheduler  # type: ignore[import-not-found]

    return Action, Func, Object, String, Task, TaskScheduler


def _message_loop_running() -> bool:
    """True when called from the thread pumping WinForms' own message loop.

    `Application.MessageLoop` needs no Control reference, so it works even
    before one exists — which matters, because `_browser`'s wait below can run
    before `winforms.BrowserView.instances` has anything in it yet. This is the
    one question both `_browser` and `_await_on_ui` need answered: may this
    thread block, or does blocking it starve the very message loop the thing
    it's waiting for is delivered through. Isolated so tests can fake it.
    """
    import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

    return bool(WinForms.Application.MessageLoop)


def _pump_messages() -> None:
    """Process one round of the Windows message queue without blocking.

    Only ever called when `_message_loop_running()` is True — DoEvents from any
    other thread is unsafe. Isolated so tests can fake it.
    """
    import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

    WinForms.Application.DoEvents()


def _browser(window: Any) -> Any:
    """pywebview's EdgeChrome wrapper for this window, once it exists.

    CoreWebView2 is null until EnsureCoreWebView2Async completes, and that
    completion is delivered through the UI thread's message loop. A plain
    time.sleep() loop called from that same thread — which pywebview's own
    Shown/loaded events fire on synchronously — blocks the one thing the wait
    is waiting for: measured live as the panel staying fine while the whole
    예매창 went 응답 없음 for the full length of this wait. Pump instead of
    sleeping outright when this is that thread; a background thread caller
    still just sleeps, since the UI thread's loop runs independently of it.
    """
    from webview.platforms import winforms  # type: ignore[import-not-found]

    pumping = _message_loop_running()
    for _ in range(_WEBVIEW_WAIT_TRIES):
        form = winforms.BrowserView.instances.get(window.uid)
        browser = getattr(form, "browser", None) if form is not None else None
        control = getattr(browser, "webview", None) if browser is not None else None
        # CoreWebView2 is null until initialisation completes — the same race the
        # macOS side waits out for `.webview`.
        if control is not None and getattr(control, "CoreWebView2", None) is not None:
            return browser
        if pumping:
            _pump_messages()
        time.sleep(_WEBVIEW_WAIT_SECONDS)
    return None


def _await_on_ui(browser: Any, start_task: Any, timeout: float = _CALL_TIMEOUT_SECONDS) -> Any:
    """Run `start_task()`'s async WebView2 call and wait for it to finish.

    Two paths, chosen by whether this is already the thread running WinForms'
    message loop:

    - A background thread (the common case — apply_state runs off the
      watch_state poller): marshal in with Invoke and block this thread while
      waiting; the UI thread's own loop, undisturbed, delivers the
      syncContext continuation normally. Lifted from pywebview's own
      evaluate_js, which this same shape works for precisely because it is
      always called that way.
    - The UI thread itself (install_document_start_script from Shown/loaded,
      which pywebview fires synchronously on that thread, no marshaling
      involved before we are ever called): Invoke would just run the delegate
      inline here, and then blocking this thread on `done.acquire()` would be
      blocking the one thread the syncContext continuation needs in order to
      ever run — a real, bounded (this function's own timeout) but entirely
      real hang, reported live as the whole app going 응답 없음 for its
      length. So the continuation runs off-thread instead
      (TaskScheduler.Default) and this thread pumps messages while it waits
      rather than blocking them, covering the case where the antecedent
      task's own completion also needs that pump to be delivered.
    """
    Action, Func, Object, _String, Task, TaskScheduler = _clr()
    done = Semaphore(0)
    box: dict[str, Any] = {}

    def _record(task: Any) -> None:
        box["result"] = task.Result
        done.release()

    if _message_loop_running():
        start_task().ContinueWith(Action[Task[Object]](_record), TaskScheduler.Default)
        deadline = time.monotonic() + timeout
        while not done.acquire(timeout=0.01):
            _pump_messages()
            if time.monotonic() > deadline:
                raise TimeoutError("WebView2 call did not complete")
        return box.get("result")

    def _on_ui() -> Any:
        return start_task().ContinueWith(Action[Task[Object]](_record), browser.syncContextTaskScheduler)

    browser.webview.Invoke(Func[Object](_on_ui))
    if not done.acquire(timeout=timeout):
        raise TimeoutError("WebView2 call did not complete")
    return box.get("result")


def _run_on_ui_thread(browser: Any, fn: Any) -> None:
    """Run a synchronous, void WebView2 call on the thread that owns the control.

    `_await_on_ui` is for calls that hand back a Task to await. CreateCookie and
    AddOrUpdateCookie return void and just have to happen on the UI thread —
    called from a background thread (which is where the startup sequence now
    runs, so it cannot wedge the UI thread) they otherwise throw or hang. From
    the UI thread itself this is a direct call; from anywhere else it is a
    blocking Control.Invoke, which the UI thread's own message loop services.
    """
    if _message_loop_running():
        fn()
        return
    _Action, Func, Object, _String, _Task, _TaskScheduler = _clr()

    def _wrapped() -> Any:
        fn()
        return None

    # Func[Object], not Action — matches _await_on_ui's idiom and what pywebview
    # itself hands Invoke, so the same fakes exercise it.
    browser.webview.Invoke(Func[Object](_wrapped))


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


# Public name for the diagnostic dump in browser_host; the leading-underscore
# one stays as the in-module spelling ensure_ready() already uses.
def webview2_runtime_version() -> str:
    return _webview2_runtime_version()


def disable_gpu_rendering() -> None:
    """Force WebView2 to render in software, before any window exists.

    Reported live: the panel healthy, the process not crashing, nothing
    hanging (the timeouts added for that would have caught it and written
    crash.log — they did not), and the 예매 창 rendering as pure blank anyway.
    That is the textbook signature of a Chromium GPU compositing failure
    underneath a UI that otherwise works correctly — well documented on VMs
    and remote sessions, and just as real on physical hardware with an
    outdated or buggy Intel graphics driver, which is exactly what a laptop's
    integrated GPU is.

    pywebview exposes no setting for this — CoreWebView2CreationProperties is
    built entirely inside its own edgechromium.py, with no hook for a caller to
    add browser arguments. WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS is Microsoft's
    own documented way around exactly that: the WebView2 loader reads it
    directly from the process environment when it creates its environment, so
    it works regardless of what the hosting library does or does not expose.
    It has to be set before that environment is created — before
    webview.create_window() — setting it after has no effect.
    """
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu")


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
        manager = self._manager
        _run_on_ui_thread(
            self._browser,
            lambda: manager.AddOrUpdateCookie(cookie_from_row(manager, row)),
        )


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
