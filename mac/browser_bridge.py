from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def locked_state(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


class BrowserBridge:
    def __init__(self, mac_dir: Path) -> None:
        self.mac_dir = mac_dir
        self.state_path = mac_dir / ".pureclick_browser_state.json"
        self.host_script = mac_dir / "browser_host.py"
        self.process: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, *, geometry: tuple[int, int, int, int] | None = None) -> None:
        """Launch the 예매 창. `geometry` tiles it beside the panel."""
        if self.running:
            return
        argv = [sys.executable, str(self.host_script), str(self.state_path)]
        if geometry:
            argv += [str(int(value)) for value in geometry]
        self.process = subprocess.Popen(argv, cwd=str(self.mac_dir))

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

    def read_autopilot_status(self) -> dict[str, Any] | None:
        status = self.read_state().get("autopilot_status")
        return status if isinstance(status, dict) else None

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
