# NOL Sniper

NOL / Interpark ticket assistant for macOS and Windows. Two jobs, and nothing
else:

- **오픈 대기** — be first into the queue the instant a show opens
- **취켓팅** — watch an area of the seat map you drew and take whatever
  cancellation appears in it, at any grade

## Run

**macOS** — from a checkout:

```bash
cd mac && ./run_nolsniper.sh
```

**Windows** — download `NOLSniper.exe` from the
[Releases](../../releases) page and double-click it. Nothing to install: the
exe carries its own Python. It needs the WebView2 runtime, which ships with
Windows 11 and with current Edge; if it is missing the app says so and links to
it rather than failing quietly.

To run from source on Windows instead — which is what you want when the exe
misbehaves and you need to see the error — double-click `NOLSniper.bat`. That
one needs Python installed.

Two windows open: the 조작판 (control panel) and the 예매 창 (an embedded
browser). You log in and type any 보안문자 yourself in the 예매 창; everything
else is driven from inside that page.

**[mac/README.md](mac/README.md) is the manual** — how each function works, what
was measured, and what it will not do. This page is only the map.

## Layout

| | |
|---|---|
| `mac/` | the app — panel, browser host, bridge, session store |
| `app_platform/` | the only place the OS matters: WKWebView vs WebView2, flock vs msvcrt |
| `nolsniper_main.py` | the entry point, for both the panel and the browser process |
| `app_update.py` | version check, and a checksum-verified refresh of the automation |
| `tools/` | the static audit, and the Windows icon |
| `browser/nolsniper_autopilot.js` | everything that happens inside the booking page |
| `core/` | pure logic — no tkinter, no pywebview, no filesystem, all tested |
| `tests/` | `pytest tests/` and `node tests/test_autopilot_picker.mjs` |
| `research/probes/` | one script per measurement the design rests on |
| `research/seatmaps/`, `api_shapes/` | captured venue layouts and API response shapes |
| `docs/` | how the Interpark/NOL booking flow actually works |

## Tests

```bash
python3 -m pytest tests/ -q
node tests/test_autopilot_picker.mjs
```

## Legal

Korea's 공연법 (amended, effective March 2024) makes macro ticket purchasing an
offence when combined with resale — up to 1 year imprisonment or a ₩10M fine.
Automated booking also breaches the site's terms of use.

## Updates on Windows

A downloaded exe is a snapshot, but the part that goes stale fastest is not the
Python — it is `browser/nolsniper_autopilot.js`, because it tracks NOL's markup.
So on launch the app checks the release manifest:

- a newer exe is **reported** with a link; nothing installs itself
- newer automation is downloaded, **verified against the manifest's SHA-256**,
  and only then used

Any failure — offline, bad checksum, unreachable manifest — falls back to the
bundled copy and says so on the panel. The checksum refuses a tampered or
truncated download; it does not protect against whoever can publish to this
repo, which is the trust root for anyone you hand the exe to.

Builds come from `.github/workflows/windows-build.yml`, which runs the test
suite on a Windows runner before it packages anything. Tag `v*` to publish a
Release.
