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

import platform

if platform.system() == "Windows":
    from app_platform import windows as backend
else:
    from app_platform import darwin as backend

NAME = backend.NAME

prepare_display = backend.prepare_display
ensure_ready = backend.ensure_ready
lock_exclusive = backend.lock_exclusive
unlock = backend.unlock
install_document_start_script = backend.install_document_start_script
cookie_store = backend.cookie_store
dump_cookies = backend.dump_cookies
restore_cookies = backend.restore_cookies
timing_precision = backend.timing_precision

__all__ = [
    "NAME",
    "prepare_display",
    "ensure_ready",
    "lock_exclusive",
    "unlock",
    "install_document_start_script",
    "cookie_store",
    "dump_cookies",
    "restore_cookies",
    "timing_precision",
]
