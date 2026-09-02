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

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in (ROOT, ROOT / "mac"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

BROWSER_HOST_FLAG = "--browser-host"


def main() -> None:
    if BROWSER_HOST_FLAG in sys.argv:
        # browser_host reads its own argv positionally; drop the flag so the
        # state path stays at argv[1] where it expects it.
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != BROWSER_HOST_FLAG]
        import browser_host

        browser_host.main()
        return

    from pureclick import PureClickMacApp

    PureClickMacApp().mainloop()


if __name__ == "__main__":
    main()
