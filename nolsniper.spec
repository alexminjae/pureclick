# -*- mode: python ; coding: utf-8 -*-
"""One-file Windows build.

Three things PyInstaller does not work out on its own, each of which produces a
build that starts and then fails in a way that looks like a code bug:

  * pywebview's Windows backend loads Microsoft.Web.WebView2.{Core,WinForms}.dll
    from `webview/lib/` at import time via clr.AddReference. Without them the
    EdgeChromium backend is simply absent and pywebview falls back to MSHTML —
    an Internet-Explorer engine, on which none of this works.
  * The platform backends are imported by name at runtime, so nothing static
    references them and they get pruned.
  * The autopilot is read from disk. A one-file build unpacks to a temp
    directory, which is why browser_host resolves it through sys._MEIPASS.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import os as _os

datas = [
    ("browser/nolsniper_autopilot.js", "browser"),
]
# mac/nolsniper_seat_config.json is NOT bundled: it is per-user runtime state —
# saved 매수/전략/디스코드 웹훅 preferences, written by the running app, correctly
# gitignored, absent on a fresh checkout. Bundling it would have shipped
# whoever built the exe's own saved settings to every user, and the CI runner
# does not even have one — PyInstaller failed outright with "Unable to find
# ...nolsniper_seat_config.json" when this tried to. The app already handles a
# missing file on first run (mac/nolsniper.py's _load_seat_config).
# Written by the workflow immediately before this runs. Absent in a local build,
# and app_update treats that as "no update source", which is correct there.
for _stamp in ("VERSION", "UPDATE_URL"):
    if _os.path.exists(_stamp):
        datas.append((_stamp, "."))
# The WebView2 interop assemblies and everything else pywebview ships.
datas += collect_data_files("webview", include_py_files=False)

hiddenimports = [
    "clr",
    "app_platform",
    "app_platform.windows",
    "app_platform.darwin",
    "browser_host",
    "browser_bridge",
    "browser_session",
    "nolsniper",
    "app_update",
]
hiddenimports += collect_submodules("webview.platforms")

a = Analysis(
    ["nolsniper_main.py"],
    pathex=[".", "mac"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here needs a browser engine of its own, and cef pulls in ~100 MB.
    excludes=["cefpython3", "PyQt5", "PySide2", "PySide6", "gi"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NOLSniper",
    debug=False,
    strip=False,
    upx=False,          # UPX-packed exes are a routine false positive for AV
    console=False,      # no console window behind the panel
    icon="mac/nolsniper.ico" if __import__("os").path.exists("mac/nolsniper.ico") else None,
)
