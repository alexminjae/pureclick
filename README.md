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
exe carries its own Python. **It drives Chrome** (or Edge, which every Windows
machine has) as the 예매 창, so one of those must be present — it is not
embedded, and no WebView2 runtime is involved.

To run from source on Windows instead — which is what you want when the exe
misbehaves and you need to see the error — double-click `NOLSniper.bat`. That
one needs Python installed; if it is missing, or `pip install` fails, or the
app exits with an error, the window stays open, says which of those happened
in Korean, and shows the tail of `nolsniper_setup.log` / `crash.log` before
pausing. It is saved CRLF (`.gitattributes` enforces it) because an LF-only
batch file can make `cmd.exe` lose a `goto` label and close instantly.

Two windows open: the 조작판 (control panel) and the 예매 창. You log in and
type any 보안문자 yourself in the 예매 창; everything else is driven from inside
that page. **The login is remembered** — on Windows it lives in the browser
profile under `%LOCALAPPDATA%\NOLSniper\chrome-profile`, so it survives
restarts on its own.

### Why the two platforms differ

macOS embeds WKWebView through pywebview. Windows used to embed WebView2 the
same way and cannot: reaching WebView2 goes Python → pywebview → pythonnet →
.NET, and a call that blocks in there holds Python's GIL, which freezes the
whole process — every thread, permanently. Measured on a Windows runner, a
`join(timeout=8)` never returned. That makes every timeout, watchdog and
diagnostic in this app unreachable at exactly the moment they matter, which is
why the 예매 창 could sit white and "(응답 없음)" forever with nothing logged.

So Windows drives a real Chrome over the DevTools protocol instead
(`mac/chrome_host.py`, `mac/cdp.py`): a separate process, spoken to over a
WebSocket, where a stall is one socket with a deadline on it. Set
`NOLSNIPER_HOST=chrome` or `=webview` to force either on either platform.

The Chrome host prefers Chrome and falls back to Edge, which every Windows
machine has and which is the same engine — a working fallback, not a
compromise. `NOLSNIPER_BROWSER=<full path to the exe>` picks one explicitly.

**[mac/README.md](mac/README.md) is the manual** — how each function works, what
was measured, and what it will not do. This page is only the map.

## Layout

| | |
|---|---|
| `mac/` | the app — panel, both browser hosts, bridge, session store |
| `mac/chrome_host.py`, `mac/cdp.py` | the Windows 예매 창: a real Chrome over DevTools |
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
suite on a Windows runner before it packages anything. Every push uploads an
`NOLSniper-windows` artifact; tag `v*` to publish a Release. Both carry
`NOLSniper_Windows.zip` — the exe plus a Korean one-page README — which is the
file to send over KakaoTalk.
