"""Drive a real Chrome over the DevTools Protocol.

Why this exists: on Windows the pywebview/pythonnet path can block a Python
thread inside a .NET call *while holding the GIL*, which freezes the whole
interpreter — proved on a Windows runner, where `worker.join(timeout=8)` never
returned. Every timeout, watchdog and diagnostic in the app is Python code that
needs the GIL, so once that happens nothing can fire and nothing can report it.

Everything here is plain sockets and Python threading. A blocked read is one
socket with a deadline on it; it cannot stop another thread, and every wait in
this module has a real timeout that actually fires.

Three details that are easy to get wrong and were each measured against the live
NOL page:

  * Chrome 111+ refuses a DevTools WebSocket handshake that carries an `Origin`
    header. Send none (`suppress_origin`) rather than opening Chrome up with
    `--remote-allow-origins=*`.
  * `--remote-debugging-port=0` makes Chrome pick a free port and write it to
    `DevToolsActivePort` in the profile directory. Hard-coding a port collides
    with whatever else is on the machine.
  * `Runtime.evaluate` with no `contextId` drifts into whichever execution
    context was created last — on NOL that is a tracking iframe, where
    `document.body.innerHTML.length` is 0 and the injected autopilot is present
    too, so every assertion passes against the wrong document. The main frame's
    default context has to be pinned from `Runtime.executionContextCreated`.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:
    import websocket  # websocket-client
except ImportError as exc:  # pragma: no cover - surfaced by ensure_ready
    raise ImportError(
        "websocket-client is required for the Chrome host. Run: pip install websocket-client"
    ) from exc


class ChromeNotFound(RuntimeError):
    """No Chrome/Edge/Chromium on this machine."""


def find_chrome() -> str | None:
    """The Chromium-family browser to drive, preferring Chrome, or None.

    Edge is accepted because it is the one browser guaranteed present on
    Windows — the same engine, and it speaks the identical protocol, so it is a
    working fallback rather than a compromise.

    NOLSNIPER_BROWSER=<full path to the exe> overrides the search entirely.

    The loop is browser-major, location-minor on purpose. The other way round —
    checking every browser under Program Files before looking in LOCALAPPDATA —
    hands Edge the job on any machine where Chrome was installed per-user,
    which is what an install without admin rights does. Reported from a real
    machine: Edge opened while Chrome was present.
    """
    override = (os.environ.get("NOLSNIPER_BROWSER") or "").strip()
    if override:
        return override if Path(override).exists() else None

    system = platform.system()
    if system == "Windows":
        roots = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        ]
        rels = [
            r"Google\Chrome\Application\chrome.exe",
            r"Chromium\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
        ]
        for rel in rels:
            for root in roots:
                if not root:
                    continue
                candidate = Path(root) / rel
                if candidate.exists():
                    return str(candidate)
    elif system == "Darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ):
            if Path(path).exists():
                return path
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def launch(
    chrome: str,
    profile: Path,
    start_url: str = "about:blank",
    extra_flags: list[str] | None = None,
) -> subprocess.Popen:
    """Start Chrome on its own persistent profile with DevTools open.

    The profile is the whole point of using a real browser: Chrome keeps the
    Naver login, the NOL session and cf_clearance in it by itself, so none of
    browser_session.py's dump/restore — the source of several Windows failures —
    is needed at all.
    """
    profile.mkdir(parents=True, exist_ok=True)
    # Stale from a previous run; read_port would otherwise return the old port.
    (profile / "DevToolsActivePort").unlink(missing_ok=True)
    argv = [
        chrome,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        # Deliberately NOT --enable-automation: that sets navigator.webdriver
        # and shows the "controlled by automated software" infobar. Measured
        # without it: navigator.webdriver === false on the live page.
    ]
    # Window position/size, when the panel said where. Before start_url, which
    # Chrome takes positionally.
    argv.extend(extra_flags or [])
    argv.append(start_url)
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags
    )


def read_port(profile: Path, timeout: float = 30.0) -> int:
    """The port Chrome chose, from the file it writes on startup."""
    deadline = time.monotonic() + timeout
    marker = profile / "DevToolsActivePort"
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            if first:
                return int(first)
        except (OSError, IndexError, ValueError) as exc:
            last = exc
        time.sleep(0.1)
    raise TimeoutError(f"Chrome never wrote {marker} ({last})")


def browser_ws_url(port: int, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 - localhost DevTools endpoint
                f"http://127.0.0.1:{port}/json/version", timeout=5
            ) as response:
                return json.loads(response.read())["webSocketDebuggerUrl"]
        except Exception as exc:  # noqa: BLE001 - Chrome may not be listening yet
            last = exc
            time.sleep(0.2)
    raise TimeoutError(f"DevTools endpoint never answered on {port} ({last})")


class CDPError(RuntimeError):
    """A command came back with an error, or the connection went away."""


class Connection:
    """One DevTools WebSocket, with a reader thread and real timeouts.

    Replies and events arrive interleaved on a single socket, so a reader thread
    owns `recv()` and hands replies to whoever is waiting on that id. Callers
    block on a `threading.Event` with a timeout — which releases the GIL and
    always fires, unlike the .NET marshalling this replaces.
    """

    def __init__(self, ws_url: str) -> None:
        self._ws = websocket.create_connection(
            ws_url,
            max_size=64 * 1024 * 1024,  # a 350 KB script plus page payloads
            # See the module docstring: Chrome 111+ rejects a handshake carrying
            # an Origin header, and sending none is what the official clients do.
            suppress_origin=True,
            timeout=30,
        )
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self.closed = threading.Event()
        self._on_event: list[Callable[[dict[str, Any]], None]] = []
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    def on_event(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_event.append(handler)

    def _read_loop(self) -> None:
        while not self.closed.is_set():
            try:
                raw = self._ws.recv()
            except Exception:  # noqa: BLE001 - a closed socket ends the loop, by design
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            reply_id = msg.get("id")
            if reply_id is not None:
                with self._pending_lock:
                    slot = self._pending.get(reply_id)
                if slot is not None:
                    slot["msg"] = msg
                    slot["event"].set()
                continue
            for handler in list(self._on_event):
                try:
                    handler(msg)
                except Exception:  # noqa: BLE001 - one bad handler must not kill the reader
                    pass
        self.closed.set()
        # Wake everyone still waiting; their timeout would fire anyway, but this
        # turns "the connection died" into an immediate, accurate error.
        with self._pending_lock:
            for slot in self._pending.values():
                slot["event"].set()

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        if self.closed.is_set():
            raise CDPError(f"{method}: connection closed")
        with self._id_lock:
            self._id += 1
            call_id = self._id
        payload: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        done = threading.Event()
        with self._pending_lock:
            self._pending[call_id] = {"event": done, "msg": None}
        try:
            self._ws.send(json.dumps(payload))
            if not done.wait(timeout):
                raise TimeoutError(f"{method} did not answer within {timeout:g}s")
            with self._pending_lock:
                msg = self._pending[call_id]["msg"]
            if msg is None:
                raise CDPError(f"{method}: connection closed while waiting")
            if "error" in msg:
                raise CDPError(f"{method}: {msg['error']}")
            return msg.get("result", {})
        finally:
            with self._pending_lock:
                self._pending.pop(call_id, None)

    def close(self) -> None:
        self.closed.set()
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class Page:
    """One attached tab: document-start injection and main-frame evaluation."""

    def __init__(self, conn: Connection, target_id: str, session: str) -> None:
        self.conn = conn
        self.target_id = target_id
        self.session = session
        self._script_id: str | None = None
        # frameId -> default (main-world) execution context id
        self._frame_ctx: dict[str, int] = {}
        self._root_frame: str | None = None
        self._lock = threading.Lock()
        conn.on_event(self._absorb)

    def _absorb(self, msg: dict[str, Any]) -> None:
        if msg.get("sessionId") != self.session:
            return
        method = msg.get("method")
        if method == "Runtime.executionContextCreated":
            ctx = msg["params"]["context"]
            aux = ctx.get("auxData") or {}
            if aux.get("isDefault") and aux.get("frameId"):
                with self._lock:
                    self._frame_ctx[aux["frameId"]] = ctx["id"]
        elif method == "Runtime.executionContextDestroyed":
            gone = msg["params"].get("executionContextId")
            with self._lock:
                for frame, ctx_id in list(self._frame_ctx.items()):
                    if ctx_id == gone:
                        self._frame_ctx.pop(frame, None)
        elif method == "Runtime.executionContextsCleared":
            with self._lock:
                self._frame_ctx.clear()
        elif method == "Page.frameNavigated":
            frame = msg["params"].get("frame") or {}
            if not frame.get("parentId"):
                with self._lock:
                    self._root_frame = frame.get("id")

    def enable(self) -> None:
        self.conn.send("Page.enable", session=self.session)
        self.conn.send("Runtime.enable", session=self.session)
        self.conn.send("Network.enable", session=self.session)
        self.refresh_root_frame()

    def refresh_root_frame(self) -> str | None:
        try:
            tree = self.conn.send("Page.getFrameTree", session=self.session)
            root = tree["frameTree"]["frame"]["id"]
        except (CDPError, TimeoutError, KeyError):
            return None
        with self._lock:
            self._root_frame = root
        return root

    def main_context(self) -> int | None:
        with self._lock:
            root = self._root_frame
            if root is None:
                return None
            return self._frame_ctx.get(root)

    def add_document_start_script(self, source: str) -> str:
        """Register `source` to run before any page script, on every document.

        The direct equivalent of WKUserScript on macOS and
        AddScriptToExecuteOnDocumentCreated on WebView2 — and the reason the
        popup shim is in place before NOL's own bundle wires up 예매하기.
        Replacing a previous registration is explicit: Chrome would otherwise
        stack another copy of a 350 KB script onto every document.
        """
        if self._script_id is not None:
            try:
                self.conn.send(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": self._script_id},
                    session=self.session,
                )
            except (CDPError, TimeoutError):
                pass  # a stale id is not worth failing the re-registration for
            self._script_id = None
        result = self.conn.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": source, "runImmediately": False},
            session=self.session,
            timeout=30.0,
        )
        self._script_id = result.get("identifier")
        if not self._script_id:
            raise CDPError("Chrome returned no script identifier")
        return self._script_id

    def navigate(self, url: str, timeout: float = 60.0) -> None:
        self.conn.send("Page.navigate", {"url": url}, session=self.session, timeout=timeout)

    def current_url(self) -> str:
        try:
            tree = self.conn.send("Page.getFrameTree", session=self.session, timeout=10.0)
            return str(tree["frameTree"]["frame"].get("url") or "")
        except (CDPError, TimeoutError, KeyError):
            return ""

    def evaluate(self, expression: str, *, timeout: float = 20.0, await_promise: bool = False) -> Any:
        """Run JS in the MAIN frame and return its value.

        Pinning the context is not optional — see the module docstring. Falling
        back to an unpinned evaluate would silently start answering from a
        tracking iframe, which is worse than failing.
        """
        ctx = self.main_context()
        if ctx is None:
            self.refresh_root_frame()
            ctx = self.main_context()
        if ctx is None:
            raise CDPError("no main-frame execution context yet")
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "contextId": ctx,
        }
        result = self.conn.send("Runtime.evaluate", params, session=self.session, timeout=timeout)
        details = result.get("exceptionDetails")
        if details:
            text = details.get("exception", {}).get("description") or details.get("text")
            raise CDPError(f"page raised: {str(text)[:200]}")
        return result.get("result", {}).get("value")


def attach_page(conn: Connection, *, prefer_url: str | None = None) -> Page:
    """Attach to a page target, creating our own tab if none is suitable."""
    conn.send("Target.setDiscoverTargets", {"discover": True})
    targets = [t for t in conn.send("Target.getTargets")["targetInfos"] if t["type"] == "page"]
    chosen = None
    if prefer_url:
        for target in targets:
            if target.get("url", "").startswith(prefer_url):
                chosen = target["targetId"]
                break
    if chosen is None:
        # Own the tab rather than guessing: the launch tab may already have
        # navigated, and third-party beacons show up as page targets too.
        chosen = conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = conn.send(
        "Target.attachToTarget", {"targetId": chosen, "flatten": True}
    )["sessionId"]
    page = Page(conn, chosen, session)
    page.enable()
    return page
