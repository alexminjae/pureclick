"""Prove the Chrome host works: Chrome up, autopilot live on the real page.

Runs on the Windows CI runner on every push, and locally with the same command.
It is the check the whole Windows revamp turns on — if window.NOLSniper is on
nol.yanolja.com with its API surface intact, the 조작판 has something to drive.

Exit code is the result, so CI fails loudly rather than printing a wall of text
nobody reads.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mac"))

import cdp  # noqa: E402

AUTOPILOT = ROOT / "browser" / "nolsniper_autopilot.js"
START_URL = "https://nol.yanolja.com/ticket"
PROFILE = Path.home() / ".nolsniper-smoke-profile"

# The methods the 조작판's buttons and poll loop actually call. Missing any one
# of them is a dead button, so the count is the assertion.
REQUIRED = [
    "runEntry", "runCatch", "runSeats", "stopAll", "status",
    "readShowContext", "readShowCatalog", "setWatchTrigger",
]

PROBE = """
(() => {
  const a = window.NOLSniper;
  return {
    href: location.href,
    ready: document.readyState,
    bodyChars: (document.body && document.body.innerHTML.length) || 0,
    isMainFrame: window.top === window.self,
    hasAutopilot: !!a,
    methods: a ? %s.filter((m) => typeof a[m] === "function") : [],
    popupShimmed: !/\\[native code\\]/.test(String(window.open)),
    webdriver: navigator.webdriver === true,
  };
})()
""" % json.dumps(REQUIRED)


def report(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}", flush=True)
    return ok


def main() -> int:
    if PROFILE.exists():
        shutil.rmtree(PROFILE, ignore_errors=True)

    chrome = cdp.find_chrome()
    print(f"chrome: {chrome}", flush=True)
    if not chrome:
        print("FAIL: no Chrome/Edge/Chromium on this machine", flush=True)
        return 1

    source = AUTOPILOT.read_text(encoding="utf-8")
    print(f"autopilot: {len(source):,} chars", flush=True)

    proc = cdp.launch(chrome, PROFILE, "about:blank")
    conn = None
    checks: list[bool] = []
    try:
        port = cdp.read_port(PROFILE)
        conn = cdp.Connection(cdp.browser_ws_url(port))
        checks.append(report("chrome up, devtools attached", True, f"port {port}"))

        page = cdp.attach_page(conn)
        # Before navigating: the popup shim has to govern the first document.
        ident = page.add_document_start_script(source)
        checks.append(report("autopilot registered at document-start", bool(ident)))

        page.navigate(START_URL)

        probe: dict | None = None
        deadline = time.monotonic() + 90
        last_error = ""
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                probe = page.evaluate(PROBE, timeout=15.0)
            except Exception as exc:  # noqa: BLE001 - the page is still settling
                last_error = f"{type(exc).__name__}: {exc}"
                page.refresh_root_frame()
                continue
            if probe and probe.get("bodyChars", 0) > 10000:
                break

        print(f"  probe: {json.dumps(probe, ensure_ascii=False)}", flush=True)
        if probe is None:
            print(f"  last evaluate error: {last_error}", flush=True)
            checks.append(report("page readable", False, last_error))
            return 1

        checks.append(report(
            "reading the MAIN frame, not a tracking iframe",
            bool(probe.get("isMainFrame")) and "nol.yanolja.com" in probe.get("href", ""),
            probe.get("href", ""),
        ))
        checks.append(report(
            "the real page loaded", probe.get("bodyChars", 0) > 10000,
            f"{probe.get('bodyChars', 0):,} chars",
        ))
        missing = sorted(set(REQUIRED) - set(probe.get("methods", [])))
        checks.append(report(
            "window.NOLSniper exposes every method the panel calls",
            not missing, f"missing: {missing}" if missing else f"{len(REQUIRED)}/{len(REQUIRED)}",
        ))
        checks.append(report(
            "popup shim installed before page scripts", bool(probe.get("popupShimmed"))))
        checks.append(report(
            "not flagged as automated", probe.get("webdriver") is False,
            f"navigator.webdriver={probe.get('webdriver')}",
        ))
        return 0 if all(checks) else 1
    finally:
        if conn:
            conn.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print(f"\n{sum(checks)}/{len(checks)} checks passed", flush=True)


if __name__ == "__main__":
    sys.exit(main())
