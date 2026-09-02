from __future__ import annotations

import json
import os
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

import app_platform  # noqa: E402
import browser_session  # noqa: E402
from browser_bridge import load_state, locked_state, merge_if_changed, patch_state, save_state  # noqa: E402

# What the platform hooks actually did, so a first run on an unfamiliar machine
# can say which one failed instead of looking healthy. Published with the bridge
# health, which the panel already renders.
PLATFORM_STATE: dict[str, Any] = {
    "platform": app_platform.NAME,
    "document_start": "unknown",
    "document_start_error": "",
    "autopilot_source": "bundled",
}

# A frozen build's __file__ resolves inside sys._MEIPASS, wiped on every
# launch. The panel always passes STATE_PATH explicitly (argv[1]) — see
# BrowserBridge, which already relocates it when frozen — so this fallback
# only matters for a standalone/manual invocation, but it should still not
# quietly point somewhere that gets erased.
_DATA_DIR = app_platform.user_data_dir() if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

STATE_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else _DATA_DIR / ".pureclick_browser_state.json"


def _bundled_script_path() -> Path:
    """Where the autopilot lives, from a checkout or inside a frozen bundle.

    PyInstaller one-file builds unpack to a temp directory and expose it as
    `sys._MEIPASS`; a path built from `__file__` finds nothing there.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "browser" / "pureclick_autopilot.js"
    return ROOT_DIR / "browser" / "pureclick_autopilot.js"


SCRIPT_PATH = _bundled_script_path()
# Not argv-threaded like STATE_PATH: both processes are the same exe re-invoked
# with a flag, sys.frozen and sys.platform agree between them, so computing
# this independently on each side lands on the identical path.
COOKIE_PATH = _DATA_DIR / ".pureclick_cookies.json"
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


def autopilot_cache_path() -> Path:
    """Where a verified autopilot update is kept, outside the bundle.

    A frozen build unpacks read-only to a temp directory, so an update cannot be
    written next to the bundled copy. Used unconditionally, source checkout
    included — unlike STATE_PATH/COOKIE_PATH this was never repo-relative, so
    there is no dev-mode behaviour to preserve by keeping it local.
    """
    return app_platform.user_data_dir() / "pureclick_autopilot.js"


def load_script() -> str:
    """The autopilot source: a verified update if there is one, else the bundle.

    The update is only ever written after its SHA-256 matched the release
    manifest (see app_update.py), so reaching for it here does not re-open that
    decision. Anything unreadable falls back to the bundled copy and says so,
    because an unverified or missing update must never look like a good one.
    """
    cached = autopilot_cache_path()
    try:
        if cached.is_file():
            source = cached.read_text(encoding="utf-8")
            if source.strip():
                PLATFORM_STATE["autopilot_source"] = "updated"
                return source
    except OSError as exc:
        PLATFORM_STATE["autopilot_source"] = f"bundled (업데이트 읽기 실패: {exc})"
    else:
        PLATFORM_STATE["autopilot_source"] = "bundled"
    return SCRIPT_PATH.read_text(encoding="utf-8")


def install_document_start_script(window: webview.Window) -> bool:
    """Register the autopilot to run at document start, before any page script.

    Injecting on the `loaded` event is too late for the parts that matter: the
    popup shim has to be in place before NOL's own bundle wires up 예매하기, and
    the seat page is a race where the first few hundred milliseconds decide
    whether a seat is still there. A user script is evaluated before any page
    script on every navigation and in every frame, so nothing has to be timed.

    pywebview exposes no API for this on either platform, so app_platform reaches
    into the backend: a WKUserScript on macOS, AddScriptToExecuteOnDocumentCreated
    on Windows. Failure is not fatal — `inject_autopilot` on `loaded` remains as
    the fallback — but it is no longer only a line on stderr. On Windows this is
    the most likely thing to fail, and the fallback still *looks* like a working
    app while losing the one race the whole macro exists to win, so the outcome
    is recorded and shown on the panel.

    IMPORTANT: the script source is snapshotted when this runs. Call it again
    whenever autopilot.js changes (reload_autopilot), or navigations keep
    re-injecting a stale copy that still does background PreselectSeat.
    """
    try:
        app_platform.install_document_start_script(window, load_script())
        PLATFORM_STATE["document_start"] = "ok"
        PLATFORM_STATE["document_start_error"] = ""
        return True
    except Exception as exc:  # noqa: BLE001 - fall back to on-load injection
        PLATFORM_STATE["document_start"] = "failed"
        PLATFORM_STATE["document_start_error"] = str(exc)[:160]
        print(f"[pureclick] document-start injection unavailable: {exc}", file=sys.stderr)
        return False


# pywebview's own evaluate_js — third-party, not ours to edit — does a bare
# `semaphore.acquire()` with no timeout. If WebView2's own message loop never
# processes the queued call, for any reason on any machine, the calling thread
# blocks forever: no exception, no log, nothing. Measured live: the 예매 창
# rendered once and then poll_context never wrote another bridge health
# report — and no crash.log either, because nothing ever raised. A thread stuck
# inside a third-party library's own untimed wait is invisible to every check
# this app already has.
_EVALUATE_JS_TIMEOUT = 8.0
# poll_context runs every 400ms and is the only source of the bridge health
# report the panel reads — a stuck call there has to be noticed and reported
# within a few ticks, not eventually.
_POLL_CONTEXT_TIMEOUT = 3.0


def _call_with_timeout(fn: Any, *, timeout: float, label: str) -> Any:
    """Run `fn()` and give up after `timeout`s instead of waiting forever.

    Runs `fn` in a disposable thread and gives this caller its own thread back
    if it does not return in time. The stuck call itself is not cancelled —
    there is no way to interrupt a blocked WebView2 Invoke from here — but the
    poller that called this can report the timeout and try again next tick,
    instead of silently ceasing to exist.
    """
    box: dict[str, Any] = {}
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            errors.append(exc)

    worker = threading.Thread(target=runner, name=f"evaluate_js:{label}", daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        raise TimeoutError(f"{label}: evaluate_js did not return within {timeout:g}s")
    if errors:
        raise errors[0]
    return box.get("result")


def reinstall_autopilot_scripts(window: webview.Window) -> None:
    """Refresh both the document-start snapshot and the live page copy."""
    install_document_start_script(window)
    if not (window.get_current_url() or "").startswith(("about:", "data:")):
        _call_with_timeout(
            lambda: window.evaluate_js(load_script()),
            timeout=_EVALUATE_JS_TIMEOUT, label="reinstall_autopilot_scripts",
        )


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

    # The whole-venue trigger the panel watches on the page's behalf. Pushed as
    # data, not as a command: it must not reload the autopilot or restart a run.
    trigger = state.get("watch_trigger")
    if isinstance(trigger, dict):
        scripts.append(f"window.PureClick?.setWatchTrigger?.({json.dumps(trigger)});")

    # Config that lands after the script has booted is invisible until something
    # re-decides the route. This asks it to, and the page ignores the request
    # when it is mid-run or already holding a seat.
    if state.get("arm") is not None or state.get("seat") is not None:
        scripts.append("window.PureClick?.configApplied?.();")

    command = state.get("command")
    if command and command in _COMMAND_JS:
        scripts.append(_COMMAND_JS[command])

    if not scripts:
        return

    _call_with_timeout(
        lambda: window.evaluate_js("\n".join(scripts)),
        timeout=_EVALUATE_JS_TIMEOUT, label="apply_state",
    )

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
    # Storage first, script second.
    #
    # localStorage is per-origin and the booking flow crosses two of them
    # (nol.yanolja.com for the product page, tickets.interpark.com for the seat
    # map). This ran the other way round, so on every fresh origin the script
    # booted, bootRoute() read an empty config and decided there was nothing to
    # do, and only then was the config written. bootRoute() is not called again
    # — `if (!alreadyLoaded)` guards it, and the 400ms watcher only re-runs it
    # when the URL *changes*, which it just had. So the arm and the seat config
    # arrived one moment too late to be read, which is how you can land on the
    # seat map after the queue and have nothing start.
    apply_state(window)
    _call_with_timeout(
        lambda: window.evaluate_js(load_script()),
        timeout=_EVALUATE_JS_TIMEOUT, label="inject_autopilot",
    )
    # And once more, for anything the panel wrote while the script was loading.
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


HEALTH_PATH = STATE_PATH.with_name(".pureclick_bridge_health.json")


def write_bridge_health(health: dict[str, Any]) -> None:
    """Say whether the page is still being read, and how long since it was.

    Deliberately not in the shared state file: this changes every tick, and the
    point of it is that its timestamp keeps moving while the loop lives and
    stops the moment it does not.
    """
    try:
        HEALTH_PATH.write_text(json.dumps(health), encoding="utf-8")
    except OSError:
        pass  # a health report that cannot be written is not worth crashing for


def poll_context(window: webview.Window, stop_event: threading.Event) -> None:
    """Read the page every 400ms — and say so when it cannot.

    This is the only source of page_context (which decides which buttons the
    panel enables), show_catalog (the seat table) and autopilot_status (every
    status line). It used to end in a bare `except Exception: pass`, so when
    evaluate_js began failing — a navigation, an uninjected page, a closed
    window — the panel went on rendering the last state it ever received, with
    nothing to distinguish that from a working app. Keeping the loop alive is
    right; being silent about it is not.
    """
    tick = 0
    saved_jar: list[dict[str, Any]] = []
    failures = 0
    last_ok = 0.0
    last_error = ""
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

            # Shorter than the other call sites: this runs every 400ms and is
            # the only source of the bridge health the panel reads, so a stuck
            # call here has to be noticed and reported quickly, not eventually.
            snapshot = _call_with_timeout(
                lambda: window.evaluate_js(_SNAPSHOT_JS),
                timeout=_POLL_CONTEXT_TIMEOUT, label="poll_context",
            )
            if isinstance(snapshot, dict):
                for key, field in (
                    ("page_context", "context"),
                    ("show_catalog", "catalog"),
                    ("autopilot_status", "status"),
                ):
                    value = snapshot.get(field)
                    if isinstance(value, dict):
                        merge_if_changed(STATE_PATH, key, value)
                failures = 0
                last_ok = time.time()
                last_error = ""
            else:
                # A page with no autopilot on it answers null. Not an error, but
                # not a reading either — the panel must not treat it as fresh.
                failures += 1
                last_error = "페이지에서 자동화 스크립트를 찾지 못했습니다"
        except Exception as error:  # noqa: BLE001 - the loop must outlive any one read
            failures += 1
            last_error = f"{type(error).__name__}: {error}"[:160]

        # Its own small file, not the shared state. This is written every tick
        # so its timestamp ages when the loop dies, and the shared state is
        # ~180KB — merging into that 2.5 times a second would mean rewriting it
        # constantly and holding its lock away from the panel for no reason.
        write_bridge_health({
            "seen_at": time.time(),
            "last_ok": last_ok,
            "failures": failures,
            "last_error": last_error,
            # Which platform hooks took. On a machine neither of us has tested,
            # this is what turns "it doesn't work" into a specific answer.
            **PLATFORM_STATE,
        })
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
