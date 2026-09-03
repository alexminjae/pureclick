"""The one place the app knows which operating system it is on.

Everything platform-specific lives behind this interface, chosen once at import.
The rule the rest of the codebase relies on: **no module outside
`app_platform.darwin` may import WebKit, Foundation, objc or fcntl, and none
outside `app_platform.windows` may import msvcrt or touch `ctypes.windll`.**
`tests/test_platform_seam.py` enforces it, because a stray import is exactly how
Windows support breaks again without anyone noticing until it is on someone
else's machine.

Both backend modules import cleanly on either OS. Their native calls are behind
small functions so the Windows logic can be exercised on a Mac with those calls
faked — which is the only way any of it gets tested before it ships.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

if platform.system() == "Windows":
    from app_platform import windows as backend
else:
    from app_platform import darwin as backend

NAME = backend.NAME


def user_data_dir() -> Path:
    """Where files meant to outlive one run of the app live.

    Plain OS convention, not a native call, so it needs no backend split. Used
    unconditionally by the update cache (app_update.py), and by the panel and
    browser bridge only in a frozen build — a source checkout keeps its simpler,
    existing behaviour of writing next to the running script.

    That "only when frozen" qualifier is load-bearing. A frozen build's
    `__file__` resolves inside `sys._MEIPASS`, the directory PyInstaller
    extracts to fresh and deletes on every single launch — so the Naver
    session, saved seat preferences and the bridge state would all silently
    reset every time the app started, which defeats the entire point of
    porting cookie persistence to Windows in the first place.
    """
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / "Library" / "Application Support"
    path = base / "NOLSniper"
    path.mkdir(parents=True, exist_ok=True)
    return path

prepare_display = backend.prepare_display
disable_gpu_rendering = backend.disable_gpu_rendering
ensure_ready = backend.ensure_ready
lock_exclusive = backend.lock_exclusive
unlock = backend.unlock
install_document_start_script = backend.install_document_start_script
cookie_store = backend.cookie_store
dump_cookies = backend.dump_cookies
restore_cookies = backend.restore_cookies
timing_precision = backend.timing_precision
webview2_runtime_version = backend.webview2_runtime_version

__all__ = [
    "NAME",
    "user_data_dir",
    "prepare_display",
    "disable_gpu_rendering",
    "ensure_ready",
    "lock_exclusive",
    "unlock",
    "install_document_start_script",
    "cookie_store",
    "dump_cookies",
    "restore_cookies",
    "timing_precision",
    "webview2_runtime_version",
]
