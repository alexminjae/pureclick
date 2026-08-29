from __future__ import annotations

import json
import sys
import time
import threading
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import webview
except ImportError as exc:
    raise SystemExit("pywebview is required. Run: pip install pywebview") from exc

import browser_session  # noqa: E402
from browser_bridge import load_state, locked_state, merge_if_changed, patch_state, save_state  # noqa: E402

STATE_PATH = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(__file__).with_name(".pureclick_browser_state.json")
)
SCRIPT_PATH = ROOT_DIR / "browser" / "pureclick_autopilot.js"
COOKIE_PATH = Path(__file__).with_name(".pureclick_cookies.json")
START_URL = "https://nol.yanolja.com/ticket"


def _window_geometry() -> tuple[int | None, int | None, int, int]:
    """(x, y, width, height), as the panel passes it in argv[2:6]."""
    try:
        x, y, width, height = (int(value) for value in sys.argv[2:6])
        return x, y, max(760, width), max(640, height)
    except ValueError:
        return None, None, 1280, 900


WINDOW_GEOMETRY = _window_geometry()
# Every 25th 0.4s tick, so roughly every ten seconds.
COOKIE_SAVE_EVERY = 25

_COMMAND_JS = {
    "run_entry": "window.PureClick && PureClick.runEntry()",
    "run_seats": "window.PureClick && PureClick.runSeats()",
    "run_catch": "window.PureClick && PureClick.runCatch()",
    "probe_seats": "window.PureClick && PureClick.probeSeats()",
    "sync_grades": "window.PureClick && PureClick.syncGrades()",
    "fetch_show": "window.PureClick && PureClick.fetchShowCatalog()",
    "stop_all": "window.PureClick && PureClick.stopAll()",
    # One instrumented click, then stop. Reports into the persistent trace.
    "diagnose": "window.PureClick && PureClick.diagnose()",
    "clear_trace": "window.PureClick && PureClick.clearTrace()",
}


def load_script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def install_document_start_script(window: webview.Window) -> bool:
    """Register the autopilot as a WKUserScript that runs at document start.

    Injecting on the `loaded` event is too late for the parts that matter: the
    popup shim has to be in place before NOL's own bundle wires up 예매하기, and
    the seat page is a race where the first few hundred milliseconds decide
    whether a seat is still there. A user script is evaluated before any page
    script on every navigation and in every frame, so nothing has to be timed.

    Reaching into pywebview's Cocoa backend for the WKWebView is deliberate —
    pywebview exposes no API for document-start injection. Failure is not fatal;
    `inject_autopilot` on `loaded` remains as the fallback.

    IMPORTANT: the script source is snapshotted when this runs. Call it again
    whenever autopilot.js changes (reload_autopilot), or navigations keep
    re-injecting a stale copy that still does background PreselectSeat.
    """
    try:
        import WebKit
        from webview.platforms import cocoa

        # On the `shown` event the window is on screen but pywebview has not
        # always finished registering it, and the KeyError that follows was
        # being reported as "injection unavailable" on every launch — which
        # meant the very first page load ran without the popup shim. Wait the
        # few milliseconds out rather than falling back for the whole session.
        # Registration and the WKWebView itself land separately, and `shown`
        # can beat both: first it was a KeyError on the uid, then a
        # BrowserView with no `.webview` yet. Either was reported as
        # "injection unavailable", which meant the very first page load ran
        # without the popup shim. Wait for a usable webview instead.
        webview_obj = None
        for _ in range(60):
            instance = cocoa.BrowserView.instances.get(window.uid)
            webview_obj = getattr(instance, "webview", None) if instance else None
            if webview_obj is not None:
                break
            time.sleep(0.05)
        if webview_obj is None:
            raise RuntimeError(f"window {window.uid!r} has no webview yet")
        controller = webview_obj.configuration().userContentController()
        controller.removeAllUserScripts()
        script = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            load_script(),
            0,  # WKUserScriptInjectionTimeAtDocumentStart
            False,  # all frames, not just the main one
        )
        controller.addUserScript_(script)
        return True
    except Exception as exc:  # noqa: BLE001 - fall back to on-load injection
        print(f"[pureclick] document-start injection unavailable: {exc}", file=sys.stderr)
        return False


def reinstall_autopilot_scripts(window: webview.Window) -> None:
    """Refresh both the document-start snapshot and the live page copy."""
    install_document_start_script(window)
    if not (window.get_current_url() or "").startswith(("about:", "data:")):
        window.evaluate_js(load_script())


def read_state() -> dict[str, Any]:
    with locked_state(STATE_PATH):
        return load_state(STATE_PATH)


def write_state(state: dict[str, Any]) -> None:
    with locked_state(STATE_PATH):
        save_state(STATE_PATH, state)


def _storage_js(body: str) -> str:
    """Wrap a localStorage write so an opaque origin cannot raise.

    The window starts blank so the saved session can be restored before the
    first request, and a blank document has no storage to write to.
    """
    return f"try {{ {body} }} catch (e) {{ }}"


def _storage_set_js(key: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False)
    return _storage_js(f"localStorage.setItem({json.dumps(key)}, {json.dumps(payload)});")


def apply_state(window: webview.Window) -> None:
    with locked_state(STATE_PATH):
        state = load_state(STATE_PATH)
        navigate_url = state.pop("navigate_url", None)
        if navigate_url:
            save_state(STATE_PATH, state)

    if navigate_url:
        window.load_url(navigate_url)
        return

    if not state:
        return

    scripts: list[str] = []
    if "arm" not in state:
        scripts.append(_storage_js("localStorage.removeItem('pureclick_arm_v1');"))
    elif state.get("arm") is not None:
        scripts.append(_storage_set_js("pureclick_arm_v1", state["arm"]))
    if state.get("seat") is not None:
        scripts.append(_storage_set_js("pureclick_seat_v1", state["seat"]))
    if state.get("reload_autopilot"):
        # Refresh the WKUserScript snapshot first — otherwise the next seat-map
        # navigation re-injects the launch-time (often pre-fix) autopilot and
        # the invisible-hand path runs again under a new-looking overlay.
        install_document_start_script(window)
        scripts.append(load_script())

    command = state.get("command")
    if command and command in _COMMAND_JS:
        scripts.append(_COMMAND_JS[command])

    if not scripts:
        return

    window.evaluate_js("\n".join(scripts))

    drop = []
    if command:
        drop.append("command")
    if state.get("reload_autopilot"):
        drop.append("reload_autopilot")
    if drop:
        patch_state(STATE_PATH, drop=drop)


def inject_autopilot(window: webview.Window) -> None:
    # The window starts blank so the saved session can be restored before the
    # first request; there is nothing to automate on an opaque origin.
    if (window.get_current_url() or "").startswith(("about:", "data:")):
        return
    # Keep document-start in sync with disk on every load — otherwise a long-
    # lived app keeps re-injecting the script from process start.
    install_document_start_script(window)
    window.evaluate_js(load_script())
    apply_state(window)


def watch_state(window: webview.Window, stop_event: threading.Event) -> None:
    last_mtime = 0.0
    while not stop_event.is_set():
        try:
            mtime = STATE_PATH.stat().st_mtime if STATE_PATH.exists() else 0.0
            if mtime != last_mtime:
                last_mtime = mtime
                apply_state(window)
        except OSError:
            pass
        stop_event.wait(0.12)


# One round trip instead of four. Every evaluate_js is dispatched onto the UI
# thread and blocks until it answers, so while a page is loading these queue up
# behind it — four per tick was enough to starve `watch_state`, which is what
# actually delivers the panel's commands.
_SNAPSHOT_JS = """
(function () {
  if (!window.PureClick) return null;
  return {
    context: PureClick.readShowContext(),
    catalog: PureClick.readShowCatalog(),
    status: PureClick.status(),
  };
})()
"""


def poll_context(window: webview.Window, stop_event: threading.Event) -> None:
    tick = 0
    saved_jar: list[dict[str, Any]] = []
    while not stop_event.is_set():
        tick += 1
        try:
            # Carry the login forward. Logging in is the one manual step in the
            # whole flow, so it is worth a round trip every ten seconds to make
            # sure a crash or a quit never costs it.
            if tick % COOKIE_SAVE_EVERY == 0:
                jar = browser_session.dump(window)
                if jar and jar != saved_jar:
                    browser_session.save_jar(COOKIE_PATH, jar)
                    saved_jar = jar

            snapshot = window.evaluate_js(_SNAPSHOT_JS)
            if isinstance(snapshot, dict):
                for key, field in (
                    ("page_context", "context"),
                    ("show_catalog", "catalog"),
                    ("autopilot_status", "status"),
                ):
                    value = snapshot.get(field)
                    if isinstance(value, dict):
                        merge_if_changed(STATE_PATH, key, value)
        except Exception:
            pass
        stop_event.wait(0.4)


def main() -> None:
    stop_event = threading.Event()

    # A target=_blank click would otherwise hand the booking flow to Safari,
    # where none of this exists. Everything stays in this one window.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

    # Started blank so the saved session can be put back *before* the first
    # request goes out; otherwise NOL is asked for the page as a logged-out
    # visitor and the restored cookies arrive one navigation too late.
    # Tiled beside the control panel when the panel says where; the two windows
    # are one app and a centred 예매 창 used to sit on top of the panel.
    x, y, width, height = WINDOW_GEOMETRY
    window = webview.create_window(
        "NOL 예매 — 이 창에서 공연을 여세요",
        "about:blank",
        x=x,
        y=y,
        width=width,
        height=height,
        min_size=(760, 640),
    )

    def on_loaded() -> None:
        inject_autopilot(window)

    window.events.loaded += on_loaded
    window.events.closed += lambda: stop_event.set()

    started = threading.Event()

    def on_shown() -> None:
        if started.is_set():
            return
        started.set()
        install_document_start_script(window)
        jar = browser_session.load_jar(COOKIE_PATH)
        if jar:
            restored = browser_session.restore(window, jar)
            print(f"[pureclick] restored {restored} cookies", file=sys.stderr)
        window.load_url(START_URL)

    window.events.shown += on_shown

    threading.Thread(target=watch_state, args=(window, stop_event), daemon=True).start()
    threading.Thread(target=poll_context, args=(window, stop_event), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
