from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Both processes import this module, and neither can be relied on to have put the
# repo root on the path first.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app_platform  # noqa: E402

# `browser_host.py` runs watch_state and poll_context as two background
# threads that both take this lock. `flock` on macOS queues a second thread of
# the same process behind the first one, same as it would a second process —
# `msvcrt.locking` on Windows does not: it raises
# `OSError: [Errno 36] Resource deadlock avoided` the instant it sees this
# process already holding an overlapping lock, rather than waiting. Measured
# directly on the Windows CI runner. Uncaught in a background thread with no
# console to show it, that is a thread dying silently on its very first
# contended acquire — consistent with the 예매 창 process going quiet forever
# the moment its own two polling threads first overlapped. The threading.Lock
# below serialises this process's own threads before either platform's file
# lock is ever asked to arbitrate between processes, which is the one thing it
# does not need to be relied on for.
_intra_process_lock = threading.Lock()


@contextmanager
def locked_state(path: Path) -> Iterator[None]:
    """Serialise access to the state file across the panel and browser processes.

    The file lock itself is platform-specific — `flock` on macOS, `msvcrt.locking`
    on Windows — and lives behind app_platform. `import fcntl` used to sit at
    module scope here, which meant both processes died on import on Windows
    before a line of the app ran.
    """
    with _intra_process_lock:
        lock_path = path.with_name(path.name + ".lock")
        with open(lock_path, "a+") as fh:
            app_platform.lock_exclusive(fh)
            try:
                yield
            finally:
                app_platform.unlock(fh)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Replace the state file in one step, never truncate-and-rewrite.

    This was `path.write_text(...)`, which empties the file and then fills it.
    Two processes rewrite this 300 KB file several times a second, so a reader
    landing mid-write got a truncated document — `load_state` catches the
    JSONDecodeError and returns `{}`, which silently resets the whole panel.
    The flock made that unreachable on macOS; it would not have on Windows, and
    `os.replace` is atomic on both, so the class is gone rather than relocated.
    """
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    _replace_atomic(temp, path)


def _replace_atomic(temp: Path, path: Path) -> None:
    """`os.replace`, retried against Windows' transient sharing violation.

    Measured on the Windows CI runner: this raised `PermissionError: [WinError
    5] Access is denied` under exactly the concurrent read/write load the atomic
    replace exists to survive. `os.replace` is `MoveFileExW` on Windows, which
    refuses to replace a file another handle currently has open without
    `FILE_SHARE_DELETE` — and that is the sharing mode plain `open()`/
    `read_text()` uses. Two processes read this file several times a second, so
    a reader can be mid-open at the instant a writer replaces it. The failure is
    transient — the reader's handle closes within microseconds — so a short
    backoff resolves it. POSIX rename has no such restriction and never takes
    this path; the loop is a no-op there on the first attempt.
    """
    delay = 0.001
    for attempt in range(8):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(delay)
            delay *= 2


def patch_state(
    path: Path,
    updates: dict[str, Any] | None = None,
    *,
    drop: list[str] | None = None,
) -> dict[str, Any]:
    with locked_state(path):
        state = load_state(path)
        if updates:
            state.update(updates)
            changed = True
        else:
            changed = False
        for key in drop or []:
            if key in state:
                state.pop(key, None)
                changed = True
        if changed:
            save_state(path, state)
        return state


def merge_if_changed(path: Path, key: str, value: Any) -> bool:
    """Write `key` only when it actually changed, so the host does not re-apply."""
    with locked_state(path):
        state = load_state(path)
        if state.get(key) == value:
            return False
        state[key] = value
        save_state(path, state)
        return True


def _host_module() -> str:
    """Which 예매 창 implementation the panel spawns.

    Windows gets Chrome over CDP. The embedded WebView2 path reaches WebView2
    through pywebview and pythonnet, and a blocked marshalled call there holds
    the GIL — measured on a Windows runner, `worker.join(timeout=8)` never
    returned — which freezes the whole process and makes every timeout, watchdog
    and diagnostic in this app unreachable at exactly the moment they are needed.

    macOS keeps WKWebView. It has no equivalent failure (verified: the same page,
    520,833 characters, document-start ok, 83/83 cookies restored) and it is what
    has actually been running, so there is nothing to gain by moving it.

    NOLSNIPER_HOST=chrome|webview forces either, which is how both are tested.
    """
    choice = (os.environ.get("NOLSNIPER_HOST") or "").strip().lower()
    if choice == "chrome":
        return "chrome_host"
    if choice == "webview":
        return "browser_host"
    return "chrome_host" if sys.platform == "win32" else "browser_host"


def host_flag() -> str:
    """The argv flag the frozen build dispatches on. See nolsniper_main."""
    return "--chrome-host" if _host_module() == "chrome_host" else "--browser-host"


class BrowserBridge:
    def __init__(self, mac_dir: Path) -> None:
        self.mac_dir = mac_dir
        # A frozen build's mac_dir resolves inside sys._MEIPASS and is wiped on
        # every launch; the bridge state (and, through it, the health file) has
        # to survive a restart there. host_script keeps pointing at mac_dir —
        # it is only read in the non-frozen spawn branch below.
        data_dir = app_platform.user_data_dir() if getattr(sys, "frozen", False) else mac_dir
        self.state_path = data_dir / ".nolsniper_browser_state.json"
        self.health_path = data_dir / ".nolsniper_bridge_health.json"
        self.host_script = mac_dir / f"{_host_module()}.py"
        self.process: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, *, geometry: tuple[int, int, int, int] | None = None) -> None:
        """Launch the 예매 창. `geometry` tiles it beside the panel."""
        if self.running:
            return
        # Frozen builds have no interpreter to invoke: sys.executable is the app
        # itself, so it is re-run with a flag the entry point dispatches on.
        # Passing host_script there would relaunch the control panel instead.
        if getattr(sys, "frozen", False):
            argv = [sys.executable, host_flag(), str(self.state_path)]
        else:
            argv = [sys.executable, str(self.host_script), str(self.state_path)]
        if geometry:
            argv += [str(int(value)) for value in geometry]
        # Otherwise a console window opens beside the 예매 창 on Windows and
        # stays there for the life of the app.
        extra: dict[str, Any] = {}
        if sys.platform == "win32":
            extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(argv, cwd=str(self.mac_dir), **extra)

    def stop(self) -> None:
        if not self.running or self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None

    def read_state(self) -> dict[str, Any]:
        with locked_state(self.state_path):
            return load_state(self.state_path)

    def read_page_context(self) -> dict[str, Any] | None:
        context = self.read_state().get("page_context")
        return context if isinstance(context, dict) else None

    def read_show_catalog(self) -> dict[str, Any] | None:
        catalog = self.read_state().get("show_catalog")
        return catalog if isinstance(catalog, dict) else None

    def read_bridge_health(self) -> dict[str, Any]:
        """Whether the browser host is still reading the page, and how recently.

        Its own file rather than a key in the shared state: it is rewritten
        every 400ms by design, so its timestamp is what goes stale when the host
        stops — the payload in the shared state would just sit there looking
        current forever.
        """
        try:
            return json.loads(self.health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def read_autopilot_status(self) -> dict[str, Any] | None:
        status = self.read_state().get("autopilot_status")
        return status if isinstance(status, dict) else None

    def read_snapshot(self) -> dict[str, dict[str, Any]]:
        """Everything the panel polls, from a single parse of the state file.

        `read_page_context`, `read_show_catalog` and `read_autopilot_status`
        each load and parse the whole thing — 300KB once a seat map has been
        read — so the 500ms poll parsed it five times a tick and held the lock
        away from the host for every one of them.
        """
        state = self.read_state()

        def as_dict(key: str) -> dict[str, Any]:
            value = state.get(key)
            return value if isinstance(value, dict) else {}

        return {
            "context": as_dict("page_context"),
            "catalog": as_dict("show_catalog"),
            "status": as_dict("autopilot_status"),
        }

    def push(
        self,
        *,
        arm: dict[str, Any] | None = None,
        seat: dict[str, Any] | None = None,
        reload_autopilot: bool = False,
        command: str | None = None,
        clear_arm: bool = False,
    ) -> None:
        with locked_state(self.state_path):
            current = load_state(self.state_path)
            if clear_arm:
                current.pop("arm", None)
            if arm is not None:
                current["arm"] = arm
            if seat is not None:
                current["seat"] = seat
            if reload_autopilot:
                current["reload_autopilot"] = True
            else:
                current.pop("reload_autopilot", None)
            if command:
                current["command"] = command
            else:
                current.pop("command", None)
            save_state(self.state_path, current)
        if not self.running:
            self.start()

    def push_trigger(self, trigger: dict[str, Any]) -> None:
        """Publish the whole-venue "did anything free?" verdict.

        Written on its own key and only when it changes, so a quiet venue does
        not rewrite the state file twice a second — and so it never disturbs the
        seat config, which the page reloads when that changes.
        """
        merge_if_changed(self.state_path, "watch_trigger", trigger)

    def send_command(self, command: str, *, clear_arm: bool = False) -> None:
        self.push(command=command, clear_arm=clear_arm)

    def navigate(self, url: str) -> None:
        patch_state(self.state_path, {"navigate_url": url})
        if not self.running:
            self.start()
