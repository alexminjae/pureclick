"""The single entry point, for both roles.

The app runs as two processes: the 조작판 and the 예매 창. From a source checkout
the second is started as `python mac/browser_host.py …`, which works because
`sys.executable` is the interpreter.

Inside a PyInstaller bundle it is not — `sys.executable` is PureClick.exe. That
spawn would relaunch the control panel instead of the browser, endlessly. It
works from source and is silently wrong once frozen, which is the worst pairing,
so both roles go through one entry point that decides by argv.
"""

from __future__ import annotations

import datetime
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in (ROOT, ROOT / "mac"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

BROWSER_HOST_FLAG = "--browser-host"


def _crash_log_path() -> Path:
    """Where an uncaught exception from either role gets written.

    The frozen build runs with no console (console=False in pureclick.spec),
    so an exception that kills the 예매 창 subprocess before it ever writes a
    bridge health report — a missing DLL, a broken WebView2 install, anything
    — is otherwise invisible to the user and to whoever is trying to diagnose
    it after the fact. This is the one place both can look.
    """
    try:
        import app_platform

        return app_platform.user_data_dir() / "crash.log"
    except Exception:  # noqa: BLE001 - a broken logger must not hide the real crash
        return ROOT / "crash.log"


def _log_crash(role: str, exc: BaseException) -> None:
    try:
        path = _crash_log_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- {role} crashed at {datetime.datetime.now().isoformat()} ---\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except Exception:  # noqa: BLE001 - see above
        pass


def _thread_crashed(args: threading.ExceptHookArgs) -> None:
    """Catch what the try/except in `main()` structurally cannot.

    watch_state and poll_context — the browser process's two pollers, and the
    only thing that ever writes the bridge health report the panel reads —
    run as background threads, not as calls inside main()'s own call stack. An
    exception there does not propagate up to main() to be caught by its
    try/except; Python hands it to this hook instead and the thread just ends.
    That is precisely the shape the msvcrt intra-process lock bug had, and it
    is the first place to look for a report of "worked for a moment, then the
    예매 창 went blank again" — a poller dying mid-session, after the first
    successful render, rather than before it.
    """
    if args.exc_value is not None:
        _log_crash(f"thread '{args.thread.name if args.thread else '?'}'", args.exc_value)
    threading.__excepthook__(args)  # still prints to stderr when a console exists


threading.excepthook = _thread_crashed


def main() -> None:
    if BROWSER_HOST_FLAG in sys.argv:
        # browser_host reads its own argv positionally; drop the flag so the
        # state path stays at argv[1] where it expects it.
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != BROWSER_HOST_FLAG]
        try:
            import browser_host

            browser_host.main()
        except BaseException as exc:  # noqa: BLE001 - SystemExit counts too; log, then still exit
            _log_crash("browser_host (예매 창)", exc)
            raise
        return

    try:
        from pureclick import PureClickMacApp

        PureClickMacApp().mainloop()
    except BaseException as exc:  # noqa: BLE001 - see above
        _log_crash("pureclick (조작판)", exc)
        raise


if __name__ == "__main__":
    main()
