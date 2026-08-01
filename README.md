# PureClick

Windows Interpark ticket-drop assistant with two phases:

1. **Timed click** — syncs to the booking server clock and fires a native mouse click at an exact KST millisecond.
2. **Cancellation watch** — watches a user-framed screen area for a seat bubble to appear (취켓팅) and clicks it.

No Mac edition, no browser scripts, no Tampermonkey. Pure Windows native click + screen capture.

## Requirements

- Windows 10/11
- Python 3.11+ (or the built `PureClick.exe`)

## Run

```powershell
python pureclick.py
```

Or double-click `run_pureclick.bat`.

## Phase 1 · Timed Click

1. Log in to Interpark in the browser and open the event page.
2. Open PureClick and wait for server time to sync.
3. Enter the target KST date/time.
4. **Lock Position** — move the cursor over `예매하기`, wait 5 seconds.
5. **Test** (dry run) a few times, then **Arm**.

Sync target:

```text
https://poticket.interpark.com/Book/BookMain.asp
```

This endpoint is uncached (`cache-control: no-store`, `x-cache: Miss`) and updates its `Date` header every second. PureClick catches the header rollover over a keep-alive connection and anchors that tick to `perf_counter()`.

## Phase 2 · Cancellation Watch

For catching seats when someone else's reservation is cancelled:

1. Open the seat map in the browser (keep it fully visible, no overlapping windows).
2. **Select Watch Area** — drag a box around the seat map.
3. Tune if needed:
   - **Tolerance** — color channel delta that counts as a change (default 40)
   - **Min points** — minimum changed samples before a candidate (default 3)
   - **Confirm frames** — consecutive candidate frames required before click (default 2)
   - **Poll ms** — capture interval (default 60)
   - **Auto-refresh s** — press F5 every N seconds while quiet (0 = off)
4. **Test Watch** first (detects but does not click), then **Start Watch**.

When a colored seat bubble appears in the framed area, PureClick clicks its center. Settings are saved to `pureclick_watch_config.json`.

## Build `.exe`

On a Windows machine:

```powershell
.\build_windows_exe.bat
```

Output: `dist\PureClick.exe` — no Python needed by the recipient. Cannot be built from macOS/Linux.

## Smoke test

```powershell
py -3 windows_smoke_test.py
py -3 windows_smoke_test.py --click-test
py -3 windows_smoke_test.py --capture-test
```

## Unit tests

```bash
python -m unittest discover -s tests
```

## Layout

| File | Role |
|---|---|
| `pureclick.py` | Tk GUI |
| `pureclick_core.py` | Server clock sync, precise wait, Windows click |
| `pureclick_watch_core.py` | Color-change detection, GDI screen capture |
| `windows_smoke_test.py` | Timing / click / capture benchmarks |
| `docs/interpark_flow.md` | API recon notes (reference only) |

## Accuracy notes

HTTP `Date` is second-precision. PureClick brackets the rollover and fires from a monotonic clock under elevated timer resolution. Exact server-time hitting still depends on network jitter, Windows scheduling, browser focus, and display scaling. Check `pureclick_fire_log.csv` after Test runs.
