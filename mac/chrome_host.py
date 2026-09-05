"""The 예매 창, as a real Chrome window driven over CDP.

Same contract as browser_host.py — argv[1] is the shared state file, arm/seat
config and commands come in through it, page_context/show_catalog/
autopilot_status and the bridge health go back out through it — so the 조작판
does not know or care which host it spawned.

What changes is everything underneath. browser_host embeds a WebView2 through
pywebview and pythonnet, where a blocked marshalled call holds the GIL and
freezes the whole interpreter; measured on a Windows runner,
`worker.join(timeout=8)` never returned, which makes every timeout, watchdog and
diagnostic in this app unreachable exactly when it is needed. Here the browser
is a separate process and the only channel is a WebSocket, so a stall is one
socket with a deadline on it and the rest of the program keeps running.

The login lives in Chrome's own profile directory, so there is no cookie
dump/restore at all — browser_session.py is not imported.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app_platform  # noqa: E402
import app_update  # noqa: E402
import cdp  # noqa: E402
from browser_bridge import load_state, locked_state, merge_if_changed, patch_state, save_state  # noqa: E402

START_URL = "https://nol.yanolja.com/ticket"

# Kept identical to browser_host's copies on purpose — the autopilot's API is
# the contract both hosts drive, and tests/test_host_parity.py fails if they
# drift. Duplicated rather than shared because importing browser_host would drag
# pywebview into a host whose whole point is not to need it.
_COMMAND_JS = {
    "run_entry": "window.NOLSniper && NOLSniper.runEntry()",
    # 지금 진입 — enter immediately rather than waiting for 티켓 오픈. For a show
    # that is already open there is nothing to count down to, and a greyed
    # 대기 시작 leaves the user with no action at all.
    "enter_now": "window.NOLSniper && NOLSniper.enterNow()",
    "run_seats": "window.NOLSniper && NOLSniper.runSeats()",
    "run_catch": "window.NOLSniper && NOLSniper.runCatch()",
    "probe_seats": "window.NOLSniper && NOLSniper.probeSeats()",
    # The soft-hold spike: hold one seat over the API, watch whether the page's
    # own 선택 좌석 notices, hand it straight back. Never presses 선택 완료 —
    # the question it answers is whether pressing it would be safe.
    "probe_soft_hold": "window.NOLSniper && NOLSniper.probeSoftHold()",
    # One /waiting request from wherever the 예매 창 is, to settle whether entry
    # can be done over the API from this page. Never enters, never navigates.
    "probe_queue_origin": "window.NOLSniper && NOLSniper.probeQueueOrigin()",
    # 진입 점검 — what the entry would do from wherever the 예매 창 is,
    # reported without pressing or navigating anything. Safe before an
    # open, which is the whole point: it replaces shifting the clock.
    "probe_entry": "window.NOLSniper && NOLSniper.probeEntry()",
    "sync_grades": "window.NOLSniper && NOLSniper.syncGrades()",
    "fetch_show": "window.NOLSniper && NOLSniper.fetchShowCatalog()",
    "stop_all": "window.NOLSniper && NOLSniper.stopAll()",
    # Give back every held seat and unlock, without stopping the rest.
    "release_seats": "window.NOLSniper && NOLSniper.releaseHeld()",
    # Verification hooks: keep/return a server hold without stopping the watch.
    "forget_hold": "window.NOLSniper && NOLSniper.forgetHold()",
    "release_only": "window.NOLSniper && NOLSniper.releaseOnly()",
    "diagnose": "window.NOLSniper && NOLSniper.diagnose()",
    "clear_trace": "window.NOLSniper && NOLSniper.clearTrace()",
}

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

STATE_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    app_platform.user_data_dir() / ".nolsniper_browser_state.json"
)
HEALTH_PATH = STATE_PATH.with_name(".nolsniper_bridge_health.json")
PROFILE_DIR = app_platform.user_data_dir() / "chrome-profile"

PLATFORM_STATE: dict[str, Any] = {
    "platform": f"{app_platform.NAME}/chrome",
    "host": "chrome-cdp",
    "chrome": "",
    "document_start": "unknown",
    "document_start_error": "",
    "autopilot_source": "bundled",
    "profile": str(PROFILE_DIR),
}

POLL_SECONDS = 0.4
EVALUATE_TIMEOUT = 10.0


def _bundled_script_path() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "browser" / "nolsniper_autopilot.js"
    return ROOT_DIR / "browser" / "nolsniper_autopilot.js"


SCRIPT_PATH = _bundled_script_path()


def autopilot_cache_path() -> Path:
    return app_platform.user_data_dir() / "nolsniper_autopilot.js"


def load_script() -> str:
    """A checksum-verified update if one was written, else the bundled copy."""
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


def _stage(msg: str) -> None:
    """A breadcrumb per milestone, flushed immediately and also written to disk.

    Kept from browser_host for the same reason it was added there: a host that
    stops early otherwise leaves nothing at all to say where.
    """
    print(f"[nolsniper] chrome-host: {msg}", file=sys.stderr, flush=True)
    try:
        line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n"
        with (app_platform.user_data_dir() / "startup.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def write_bridge_health(health: dict[str, Any]) -> None:
    try:
        HEALTH_PATH.write_text(json.dumps(health), encoding="utf-8")
    except OSError:
        pass


def _window_flags() -> list[str]:
    """Tile Chrome beside the panel, when the panel said where."""
    try:
        x, y, width, height = (int(value) for value in sys.argv[2:6])
    except ValueError:
        return []
    return [f"--window-position={x},{y}", f"--window-size={max(760, width)},{max(640, height)}"]


def _storage_js(body: str) -> str:
    return f"try {{ {body} }} catch (e) {{ }}"


def _storage_set_js(key: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False)
    return _storage_js(f"localStorage.setItem({json.dumps(key)}, {json.dumps(payload)});")


def install_document_start_script(page: cdp.Page) -> bool:
    try:
        page.add_document_start_script(load_script())
        PLATFORM_STATE["document_start"] = "ok"
        PLATFORM_STATE["document_start_error"] = ""
        return True
    except Exception as exc:  # noqa: BLE001 - the on-load re-eval still covers it
        PLATFORM_STATE["document_start"] = "failed"
        PLATFORM_STATE["document_start_error"] = str(exc)[:160]
        _stage(f"document-start injection failed: {exc}")
        return False


def apply_state(page: cdp.Page) -> None:
    """Push whatever the panel wrote into the page. Mirrors browser_host."""
    with locked_state(STATE_PATH):
        state = load_state(STATE_PATH)
        navigate_url = state.pop("navigate_url", None)
        if navigate_url:
            save_state(STATE_PATH, state)

    if navigate_url:
        page.navigate(navigate_url)
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
    # The show on screen (goods + place), so rounds can load before any arm.
    if state.get("show") is not None:
        scripts.append(_storage_set_js("nolsniper_show_v1", state["show"]))
    if state.get("reload_autopilot"):
        install_document_start_script(page)
        scripts.append(load_script())

    trigger = state.get("watch_trigger")
    if isinstance(trigger, dict):
        scripts.append(f"window.NOLSniper?.setWatchTrigger?.({json.dumps(trigger)});")

    if state.get("arm") is not None or state.get("seat") is not None:
        scripts.append("window.NOLSniper?.configApplied?.();")

    command = state.get("command")
    if command and command in _COMMAND_JS:
        scripts.append(_COMMAND_JS[command])

    if not scripts:
        return

    page.evaluate("\n".join(scripts), timeout=EVALUATE_TIMEOUT)

    drop = []
    if command:
        drop.append("command")
    if state.get("reload_autopilot"):
        drop.append("reload_autopilot")
    if drop:
        patch_state(STATE_PATH, drop=drop)


class PageRef:
    """The tab the two poller threads are currently driving.

    A level of indirection, so recovering from a closed tab replaces the page
    rather than the threads. Restarting the threads instead would mean tearing
    down and rebuilding the only two things that report health, at exactly the
    moment something has already gone wrong.
    """

    def __init__(self) -> None:
        self.page: cdp.Page | None = None
        self.generation = 0

    def set(self, page: cdp.Page | None) -> None:
        self.page = page
        self.generation += 1


def watch_state(ref: PageRef, stop: threading.Event) -> None:
    _stage("watch_state thread alive")
    last_mtime = 0.0
    last_generation = -1
    while not stop.is_set():
        page = ref.page
        if page is None:
            stop.wait(0.12)
            continue
        try:
            # A re-attached tab is a fresh document with none of the panel's
            # config in its localStorage, so the next push has to be applied in
            # full rather than skipped as "the file has not changed".
            if ref.generation != last_generation:
                last_generation = ref.generation
                last_mtime = 0.0
            mtime = STATE_PATH.stat().st_mtime if STATE_PATH.exists() else 0.0
            if mtime != last_mtime:
                last_mtime = mtime
                apply_state(page)
        except OSError:
            pass
        except cdp.PageGone:
            # The supervisor owns recovery; this loop only has to not die.
            stop.wait(0.2)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad push
            _stage(f"apply_state failed: {type(exc).__name__}: {exc}")
        stop.wait(0.12)


def poll_context(ref: PageRef, stop: threading.Event) -> None:
    _stage("poll_context thread alive")
    failures = 0
    last_ok = 0.0
    last_error = ""
    while not stop.is_set():
        page = ref.page
        if page is None:
            write_bridge_health({
                "seen_at": time.time(),
                "last_ok": last_ok,
                "failures": failures + 1,
                "last_error": "예매 창을 다시 여는 중…",
                **PLATFORM_STATE,
            })
            stop.wait(POLL_SECONDS)
            continue
        try:
            snapshot = page.evaluate(_SNAPSHOT_JS, timeout=EVALUATE_TIMEOUT)
            if isinstance(snapshot, dict):
                for key, field in (
                    ("page_context", "context"),
                    ("show_catalog", "catalog"),
                    ("autopilot_status", "status"),
                ):
                    value = snapshot.get(field)
                    if isinstance(value, dict):
                        merge_if_changed(STATE_PATH, key, value)
                    elif value is None and key == "show_catalog":
                        # No show on this page: let the panel drop the old one.
                        merge_if_changed(STATE_PATH, key, {})
                failures = 0
                last_ok = time.time()
                last_error = ""
            else:
                failures += 1
                last_error = "페이지에서 자동화 스크립트를 찾지 못했습니다"
        except Exception as error:  # noqa: BLE001 - report, never die
            failures += 1
            last_error = f"{type(error).__name__}: {error}"[:160]

        write_bridge_health({
            "seen_at": time.time(),
            "last_ok": last_ok,
            "failures": failures,
            "last_error": last_error,
            **PLATFORM_STATE,
        })
        stop.wait(POLL_SECONDS)


def _open_page(conn: cdp.Connection) -> cdp.Page:
    """Attach to a tab, install the automation, and put it on the start page."""
    page = cdp.attach_page(conn)
    # Before the first navigation, so the popup shim governs the first document.
    install_document_start_script(page)
    page.navigate(START_URL)
    return page


def _launch_chrome(chrome: str) -> tuple[subprocess.Popen, cdp.Connection]:
    flags = _window_flags()
    proc = cdp.launch(chrome, PROFILE_DIR, "about:blank", extra_flags=flags)
    _stage(f"chrome launched pid {proc.pid}, profile {PROFILE_DIR}, window {flags or 'default'}")
    port = cdp.read_port(PROFILE_DIR)
    conn = cdp.Connection(cdp.browser_ws_url(port))
    _stage(f"devtools websocket connected on {port}")
    return proc, conn


# How many times to rebuild the 예매 창 before leaving it to the panel. The
# panel has its own restart and a 다시 열기 button, so this only has to survive
# the ordinary accidents — a closed tab, a closed window — not fight a Chrome
# that refuses to start.
RECOVERY_LIMIT = 5
RECOVERY_PAUSE_SECONDS = 1.5


def main() -> None:
    _stage("main() entered")
    stop = threading.Event()

    chrome = cdp.find_chrome()
    if not chrome:
        raise cdp.ChromeNotFound(
            "Chrome 또는 Edge를 찾지 못했습니다. Chrome을 설치한 뒤 다시 실행하세요:\n"
            "https://www.google.com/chrome/"
        )
    PLATFORM_STATE["chrome"] = chrome
    _stage(f"chrome: {chrome}")

    proc, conn = _launch_chrome(chrome)
    ref = PageRef()
    ref.set(_open_page(conn))
    _stage(f"attached to a page target; document-start: {PLATFORM_STATE['document_start']}")

    threading.Thread(target=watch_state, args=(ref, stop), daemon=True).start()
    threading.Thread(target=poll_context, args=(ref, stop), daemon=True).start()
    _stage("threads started; supervising chrome")

    recoveries = 0
    try:
        while not stop.is_set():
            # Closing the 예매 창 used to end this process, and closing only the
            # *tab* used to be worse: Chrome stayed up, so nothing here noticed,
            # and both pollers ran against a dead session for the rest of the
            # session. Either way the only fix was quitting and relaunching the
            # whole app. Both are recoverable, and the login survives both
            # because it lives in Chrome's own profile directory.
            page = ref.page
            browser_gone = proc.poll() is not None or conn.closed.is_set()
            tab_gone = page is None or not page.alive
            if not browser_gone and not tab_gone:
                recoveries = 0
                time.sleep(0.5)
                continue

            recoveries += 1
            if recoveries > RECOVERY_LIMIT:
                _stage(f"giving up after {RECOVERY_LIMIT} recoveries")
                break

            ref.set(None)
            try:
                if browser_gone:
                    _stage(f"chrome exited; relaunching ({recoveries}/{RECOVERY_LIMIT})")
                    conn.close()
                    if proc.poll() is None:
                        proc.terminate()
                    proc, conn = _launch_chrome(chrome)
                else:
                    _stage(f"tab closed; re-attaching ({recoveries}/{RECOVERY_LIMIT})")
                ref.set(_open_page(conn))
                _stage("예매 창 recovered")
            except Exception as exc:  # noqa: BLE001 - report and try again, don't die
                _stage(f"recovery failed: {type(exc).__name__}: {exc}")
                write_bridge_health({
                    "seen_at": time.time(),
                    "last_ok": 0.0,
                    "failures": recoveries,
                    "last_error": f"예매 창을 다시 열지 못했습니다: {str(exc)[:120]}",
                    **PLATFORM_STATE,
                })
                time.sleep(RECOVERY_PAUSE_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        _stage("shutting down")
        stop.set()
        conn.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
