from __future__ import annotations

import datetime
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
import app_update  # noqa: E402
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
    # Filled in as on_shown runs, so the diagnostic dump can say how far the
    # 예매 창's own startup actually got on a machine neither of us can watch.
    "on_shown": "not reached",
    "doc_start_ms": 0,
    "cookie_restore": "not attempted",
    "cookies_in_jar": 0,
    "cookies_restored": 0,
    "last_recovery": "",
}

# A frozen build's __file__ resolves inside sys._MEIPASS, wiped on every
# launch. The panel always passes STATE_PATH explicitly (argv[1]) — see
# BrowserBridge, which already relocates it when frozen — so this fallback
# only matters for a standalone/manual invocation, but it should still not
# quietly point somewhere that gets erased.
_DATA_DIR = app_platform.user_data_dir() if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

STATE_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else _DATA_DIR / ".nolsniper_browser_state.json"


def _bundled_script_path() -> Path:
    """Where the autopilot lives, from a checkout or inside a frozen bundle.

    PyInstaller one-file builds unpack to a temp directory and expose it as
    `sys._MEIPASS`; a path built from `__file__` finds nothing there.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "browser" / "nolsniper_autopilot.js"
    return ROOT_DIR / "browser" / "nolsniper_autopilot.js"


SCRIPT_PATH = _bundled_script_path()
# Not argv-threaded like STATE_PATH: both processes are the same exe re-invoked
# with a flag, sys.frozen and sys.platform agree between them, so computing
# this independently on each side lands on the identical path.
COOKIE_PATH = _DATA_DIR / ".nolsniper_cookies.json"
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
    "run_entry": "window.NOLSniper && NOLSniper.runEntry()",
    "run_seats": "window.NOLSniper && NOLSniper.runSeats()",
    "run_catch": "window.NOLSniper && NOLSniper.runCatch()",
    "probe_seats": "window.NOLSniper && NOLSniper.probeSeats()",
    "sync_grades": "window.NOLSniper && NOLSniper.syncGrades()",
    "fetch_show": "window.NOLSniper && NOLSniper.fetchShowCatalog()",
    "stop_all": "window.NOLSniper && NOLSniper.stopAll()",
    # One instrumented click, then stop. Reports into the persistent trace.
    "diagnose": "window.NOLSniper && NOLSniper.diagnose()",
    "clear_trace": "window.NOLSniper && NOLSniper.clearTrace()",
}


def autopilot_cache_path() -> Path:
    """Where a verified autopilot update is kept, outside the bundle.

    A frozen build unpacks read-only to a temp directory, so an update cannot be
    written next to the bundled copy. Used unconditionally, source checkout
    included — unlike STATE_PATH/COOKIE_PATH this was never repo-relative, so
    there is no dev-mode behaviour to preserve by keeping it local.
    """
    return app_platform.user_data_dir() / "nolsniper_autopilot.js"


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
        print(f"[nolsniper] document-start injection unavailable: {exc}", file=sys.stderr)
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
# report the panel reads. 3s was too tight: nol.yanolja.com/ticket is a ~500 KB
# SPA and a genuine evaluate_js against it, queued behind the page's own script,
# routinely took longer than that — so the panel flipped to "응답 없음" over a
# page that was fine, just slow to read. The watchdog counts ticks, not seconds,
# so a longer per-call ceiling does not slow real-hang detection much.
_POLL_CONTEXT_TIMEOUT = 8.0


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
        scripts.append(_storage_js("localStorage.removeItem('nolsniper_arm_v1');"))
    elif state.get("arm") is not None:
        scripts.append(_storage_set_js("nolsniper_arm_v1", state["arm"]))
    if state.get("seat") is not None:
        scripts.append(_storage_set_js("nolsniper_seat_v1", state["seat"]))
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
        scripts.append(f"window.NOLSniper?.setWatchTrigger?.({json.dumps(trigger)});")

    # Config that lands after the script has booted is invisible until something
    # re-decides the route. This asks it to, and the page ignores the request
    # when it is mid-run or already holding a seat.
    if state.get("arm") is not None or state.get("seat") is not None:
        scripts.append("window.NOLSniper?.configApplied?.();")

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


# Serialises inject_autopilot against itself. It now runs on a fresh thread per
# `loaded` event (see on_loaded), and two overlapping navigations would otherwise
# race the _script_ids bookkeeping inside install_document_start_script.
_inject_lock = threading.Lock()


def inject_autopilot(window: webview.Window) -> None:
    # The window starts blank so the saved session can be restored before the
    # first request; there is nothing to automate on an opaque origin.
    if (window.get_current_url() or "").startswith(("about:", "data:")):
        return
    with _inject_lock:
        # Keep document-start in sync with disk on every load — otherwise a long-
        # lived app keeps re-injecting the script from process start.
        install_document_start_script(window)
        # Storage first, script second.
        #
        # localStorage is per-origin and the booking flow crosses two of them
        # (nol.yanolja.com for the product page, tickets.interpark.com for the
        # seat map). This ran the other way round, so on every fresh origin the
        # script booted, bootRoute() read an empty config and decided there was
        # nothing to do, and only then was the config written. bootRoute() is not
        # called again — `if (!alreadyLoaded)` guards it, and the 400ms watcher
        # only re-runs it when the URL *changes*, which it just had. So the arm
        # and the seat config arrived one moment too late to be read, which is
        # how you can land on the seat map after the queue and have nothing
        # start.
        apply_state(window)
        # The document-start user script already ran the autopilot before the
        # page's own scripts. This re-eval is only the fallback for when that
        # registration failed — when it took, skip it rather than push a 340 KB
        # evaluate_js through the bridge on every single navigation.
        if PLATFORM_STATE.get("document_start") != "ok":
            _call_with_timeout(
                lambda: window.evaluate_js(load_script()),
                timeout=_EVALUATE_JS_TIMEOUT, label="inject_autopilot",
            )
        # And once more, for anything the panel wrote while the script loaded.
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
  if (!window.NOLSniper) return null;
  return {
    context: NOLSniper.readShowContext(),
    catalog: NOLSniper.readShowCatalog(),
    status: NOLSniper.status(),
  };
})()
"""


HEALTH_PATH = STATE_PATH.with_name(".nolsniper_bridge_health.json")


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


def _desktop_dir() -> Path:
    """The user's Desktop — where a non-technical user can actually find a file.

    %LOCALAPPDATA% is invisible in normal use; a file on the Desktop can be
    opened in Notepad and its text pasted into a chat, which is the only
    diagnostic channel available for a machine neither of us can log into.
    """
    if sys.platform == "win32":
        profile = os.environ.get("USERPROFILE")
        if profile and (Path(profile) / "Desktop").is_dir():
            return Path(profile) / "Desktop"
    if (Path.home() / "Desktop").is_dir():
        return Path.home() / "Desktop"
    return Path.home()


DIAG_PATH = _desktop_dir() / "NOLSniper-진단.txt"

_PAGE_PROBE_JS = """
(function () {
  try {
    return {
      readyState: document.readyState,
      href: location.href,
      title: document.title,
      bodyChars: (document.body && document.body.innerHTML.length) || 0,
      hasAutopilot: !!window.NOLSniper,
      hasOverlay: !!document.getElementById('nolsniper-overlay'),
    };
  } catch (e) { return { probeError: String(e) }; }
})()
"""


def _probe_page(window: webview.Window) -> Any:
    """What the page currently is — or the timeout that proves the thread hung."""
    try:
        return _call_with_timeout(
            lambda: window.evaluate_js(_PAGE_PROBE_JS), timeout=2.0, label="diag_probe",
        )
    except Exception as exc:  # noqa: BLE001 - the failure IS the datum
        return f"{type(exc).__name__}: {exc}"


def _crash_log_tail(limit: int = 4000) -> str:
    try:
        path = app_platform.user_data_dir() / "crash.log"
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip() or "(비어 있음)"
    except Exception:  # noqa: BLE001
        pass
    return "(없음)"


def write_desktop_diagnostic(health: dict[str, Any], probe: Any, elapsed: float = 0.0) -> None:
    """One human-readable file on the Desktop with everything worth knowing."""
    try:
        wv_version = getattr(webview, "__version__", "?")
    except Exception:  # noqa: BLE001
        wv_version = "?"
    try:
        runtime = app_platform.webview2_runtime_version() or "(찾지 못함 — 이게 원인일 수 있음)"
    except Exception as exc:  # noqa: BLE001
        runtime = f"(확인 실패: {exc})"

    last_error = str(health.get("last_error") or "")
    if "did not return within" in last_error:
        verdict = "예매 창의 UI 스레드가 멈춤 (응답 없음). 새로고침으로는 못 살림."
    elif health.get("failures"):
        verdict = "페이지가 죽었거나 비어 있음 (스레드는 살아 있음). 자동 새로고침 대상."
    else:
        verdict = "정상"

    age = time.time() - (health.get("last_ok") or 0)
    lines = [
        f"NOL Sniper 진단  —  {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"예매 창 켜진 지    : {elapsed:.0f}초  (짧으면 아직 로딩 중일 수 있음 — 1분 뒤 다시 저장됨)",
        f"버전            : {app_update.version_tag()}",
        f"파이썬/플랫폼    : {sys.version.split()[0]} / {sys.platform}",
        f"pywebview       : {wv_version}",
        f"WebView2 런타임  : {runtime}",
        "",
        "=== 예매 창 상태 ===",
        (f"페이지 마지막 응답: {age:.0f}초 전" if health.get("last_ok")
         else "페이지가 한 번도 응답한 적 없음"),
        f"연속 실패        : {health.get('failures')}",
        f"마지막 오류      : {last_error or '(없음)'}",
        f"판정            : {verdict}",
        f"자동 복구 시각    : {PLATFORM_STATE.get('last_recovery') or '(없음)'}",
        "",
        "=== 페이지 내용 ===",
        (json.dumps(probe, ensure_ascii=False, indent=1)
         if isinstance(probe, (dict, list)) else str(probe)),
        "",
        "=== 시작 단계 / 플랫폼 훅 ===",
        json.dumps(PLATFORM_STATE, ensure_ascii=False, indent=1),
        "",
        "=== crash.log (마지막 부분) ===",
        _crash_log_tail(),
        "",
        "──────────",
        "이 파일 전체를 복사해서 그대로 보내주세요.",
    ]
    try:
        DIAG_PATH.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass


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
    started_at = time.time()
    saved_jar: list[dict[str, Any]] = []
    failures = 0
    last_ok = 0.0
    last_error = ""
    last_reload = 0.0
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

        # Auto-recover a dead page — a WebView2 renderer that crashed, or a
        # navigation that landed on nothing — by reloading it. Deliberately NOT
        # for a hung UI thread ("did not return within …"): load_url would just
        # queue behind the stuck call and never run. ~30s of misses after the
        # page had worked at least once, then one reload, then a 60s cooldown so
        # a site that is genuinely down is not reload-looped.
        is_hang = "did not return within" in last_error
        if (failures >= 75 and not is_hang and last_ok > 0
                and time.time() - last_reload > 60):
            last_reload = time.time()
            PLATFORM_STATE["last_recovery"] = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                _call_with_timeout(
                    lambda: window.load_url(START_URL),
                    timeout=_POLL_CONTEXT_TIMEOUT, label="watchdog_reload",
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"자동 새로고침 실패: {exc}"[:160]

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
        # A file the user can actually open. First one a few seconds in so it
        # exists, then every ~30s — the page may still be loading at the first
        # write, so the "예매 창 켜진 지" line says how far along this snapshot is.
        if tick in (8, 40) or tick % 80 == 0:
            write_desktop_diagnostic(
                {"last_ok": last_ok, "failures": failures, "last_error": last_error},
                _probe_page(window),
                elapsed=time.time() - started_at,
            )
        stop_event.wait(0.4)


STARTUP_LOG = "startup.log"


def _stage(msg: str) -> None:
    """A breadcrumb per startup milestone, written the instant it happens.

    A Windows run of this process produced sixty seconds of complete silence —
    no stdout, no stderr, no bridge health, no crash.log — which says only that
    it stopped somewhere before poll_context, and nothing about where. Buffered
    output is lost when the process is killed, so this flushes on every call and
    also appends to a file, and it runs before anything that could hang.
    """
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(f"[nolsniper] stage: {msg}", file=sys.stderr, flush=True)
    try:
        with (app_platform.user_data_dir() / STARTUP_LOG).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # a breadcrumb that cannot be written must not be the thing that stops us


def main() -> None:
    stop_event = threading.Event()
    _stage("main() entered")

    # DPI awareness is per-process. The panel sets it for itself; this process
    # never did, so on a scaled Windows display the WebView2 host ran
    # DPI-unaware. No-op on macOS.
    app_platform.prepare_display()
    _stage("prepare_display done")

    # The panel checks this before spawning us, so on the normal path it just
    # passes again here. It earns its place for the standalone/--browser-host
    # invocation: a missing WebView2 runtime then raises a clean crash.log
    # entry instead of putting up a blank window with nothing to diagnose.
    app_platform.ensure_ready()
    _stage("ensure_ready done — WebView2 usable")

    # Must run before create_window — that is when the WebView2 environment
    # itself gets created, and the flag this sets has no effect afterward. A
    # no-op on macOS; see app_platform.disable_gpu_rendering.
    app_platform.disable_gpu_rendering()
    _stage("disable_gpu_rendering done")

    # A target=_blank click would otherwise hand the booking flow to Safari,
    # where none of this exists. Everything stays in this one window.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    _stage("webview.settings applied")

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
    _stage("create_window returned")

    def on_loaded() -> None:
        # Never inline. pywebview fires `loaded` synchronously on WebView2's UI
        # thread, and inject_autopilot makes WebView2 calls and waits on them —
        # done from that same thread on Windows it deadlocks it, so every full
        # navigation (each Naver OAuth hop is one) froze the 예매 창 for the
        # stacked length of those waits. Windows drew that as 응답 없음 over a
        # blank window that never came back. On a worker thread the same calls
        # marshal in against a UI thread that is still pumping — the shape they
        # are built for. macOS never hit this (main-thread JS eval is fine
        # there) but the worker thread is harmless on it too.
        threading.Thread(
            target=inject_autopilot, args=(window,),
            name="inject_autopilot", daemon=True,
        ).start()

    window.events.loaded += on_loaded
    window.events.closed += lambda: stop_event.set()

    started = threading.Event()

    def _run_startup(window: webview.Window) -> None:
        """Register the script, restore the login, then load the page.

        Runs on its own thread, not inside the `shown` handler. pywebview fires
        `shown` synchronously on WebView2's UI thread, and everything below makes
        WebView2 calls and waits on them — done from that thread on Windows it
        deadlocked it, and because it deadlocked *before* the load_url in the old
        `finally`, the window sat on about:blank, "(응답 없음)", forever (measured
        at 1566s). Off-thread, each call marshals in against a UI thread that is
        still pumping, which is the shape they are built for. The document-start
        script re-registers on every navigation regardless, so a fast redirect
        beating this thread costs nothing.
        """
        PLATFORM_STATE["on_shown"] = "started"
        t0 = time.monotonic()
        try:
            install_document_start_script(window)
        except Exception as exc:  # noqa: BLE001 - the on-load fallback still covers this
            print(f"[nolsniper] doc-start: {exc}", file=sys.stderr)
        PLATFORM_STATE["doc_start_ms"] = int((time.monotonic() - t0) * 1000)
        PLATFORM_STATE["on_shown"] = "doc-start done"
        try:
            jar = browser_session.load_jar(COOKIE_PATH)
            PLATFORM_STATE["cookies_in_jar"] = len(jar)
            if jar:
                restored = browser_session.restore(window, jar)
                PLATFORM_STATE["cookies_restored"] = restored
                PLATFORM_STATE["cookie_restore"] = (
                    f"실패: {browser_session.LAST_ERROR}"[:160]
                    if browser_session.LAST_ERROR
                    else f"ok ({restored}/{len(jar)})"
                )
                print(f"[nolsniper] restored {restored} cookies", file=sys.stderr)
            else:
                PLATFORM_STATE["cookie_restore"] = "저장된 로그인 없음"
        except Exception as exc:  # noqa: BLE001 - a lost session beats a blank window
            PLATFORM_STATE["cookie_restore"] = f"실패: {type(exc).__name__}: {exc}"[:160]
            print(f"[nolsniper] cookie restore skipped: {exc}", file=sys.stderr)
        PLATFORM_STATE["on_shown"] = "load_url 호출"
        try:
            window.load_url(START_URL)
            PLATFORM_STATE["on_shown"] = "완료"
        except Exception as exc:  # noqa: BLE001
            PLATFORM_STATE["on_shown"] = f"load_url 실패: {exc}"[:160]
            print(f"[nolsniper] load_url: {exc}", file=sys.stderr)

    def on_shown() -> None:
        if started.is_set():
            return
        started.set()
        threading.Thread(
            target=_run_startup, args=(window,), name="browser-startup", daemon=True,
        ).start()

    window.events.shown += on_shown

    threading.Thread(target=watch_state, args=(window, stop_event), daemon=True).start()
    threading.Thread(target=poll_context, args=(window, stop_event), daemon=True).start()
    # DevTools (a second window on Windows) only when asked for with
    # NOLSNIPER_DEVTOOLS=1. It was on by default while the blank 예매 창 was
    # being chased, but it pops its own window unbidden — which just looked like
    # another bug to the user — and the Desktop 진단.txt now carries the page
    # state it was there to expose.
    _stage("threads started; entering webview.start()")
    webview.start(debug=bool(os.environ.get("NOLSNIPER_DEVTOOLS")))


if __name__ == "__main__":
    main()
