from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import webview
except ImportError as exc:
    raise SystemExit("pywebview is required. Run: pip install pywebview") from exc

STATE_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name(".pureclick_browser_state.json")
SCRIPT_PATH = ROOT_DIR / "browser" / "pureclick_autopilot.js"
START_URL = "https://tickets.interpark.com/ticket"


def load_script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def read_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _storage_set_js(key: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False)
    return f'localStorage.setItem({json.dumps(key)}, {json.dumps(payload)});'


def apply_state(window: webview.Window) -> None:
    state = read_state()
    if not state:
        return
    scripts: list[str] = []
    if state.get("arm") is not None:
        scripts.append(_storage_set_js("pureclick_arm_v1", state["arm"]))
    if state.get("seat") is not None:
        scripts.append(_storage_set_js("pureclick_seat_v1", state["seat"]))
    if state.get("reload_autopilot"):
        scripts.append(load_script())
    if not scripts:
        return
    window.evaluate_js(
        "\n".join(scripts)
        + """
if (window.PureClick) {
  const arm = PureClick.armConfig();
  if (arm && arm.enabled && !arm.fired && location.pathname.includes('/goods/')) {
    PureClick.runEntry();
  }
  if (arm && arm.enabled && location.pathname.startsWith('/onestop/seat')) {
    PureClick.runSeats();
  }
}
"""
    )


def inject_autopilot(window: webview.Window) -> None:
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
        stop_event.wait(0.15)


def write_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def poll_context(window: webview.Window, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            context = window.evaluate_js(
                "(function(){ return window.PureClick ? PureClick.readShowContext() : null; })()"
            )
            if isinstance(context, dict):
                state = read_state() or {}
                state["page_context"] = context
                write_state(state)
        except Exception:
            pass
        stop_event.wait(1.0)


def main() -> None:
    stop_event = threading.Event()
    window = webview.create_window(
        "PureClick Browser",
        START_URL,
        width=1280,
        height=900,
        min_size=(900, 700),
    )

    def on_loaded() -> None:
        inject_autopilot(window)

    window.events.loaded += on_loaded
    window.events.closed += lambda: stop_event.set()

    threading.Thread(target=watch_state, args=(window, stop_event), daemon=True).start()
    threading.Thread(target=poll_context, args=(window, stop_event), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
