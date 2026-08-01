from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class BrowserBridge:
    def __init__(self, mac_dir: Path) -> None:
        self.mac_dir = mac_dir
        self.state_path = mac_dir / ".pureclick_browser_state.json"
        self.host_script = mac_dir / "browser_host.py"
        self.process: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.process = subprocess.Popen(
            [sys.executable, str(self.host_script), str(self.state_path)],
            cwd=str(self.mac_dir),
        )

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
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def read_page_context(self) -> dict[str, Any] | None:
        context = self.read_state().get("page_context")
        return context if isinstance(context, dict) else None

    def push(self, *, arm: dict[str, Any] | None = None, seat: dict[str, Any] | None = None, reload_autopilot: bool = False) -> None:
        current = self.read_state()
        if arm is not None:
            current["arm"] = arm
        if seat is not None:
            current["seat"] = seat
        if reload_autopilot:
            current["reload_autopilot"] = True
        else:
            current.pop("reload_autopilot", None)
        self.state_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        if not self.running:
            self.start()
