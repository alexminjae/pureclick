# PureClick

PureClick is an Interpark ticket-drop assistant in two editions:

| Edition | Folder | Platform | Entry point |
|---|---|---|---|
| **Windows** | repo root | Windows | `pureclick.py` or `dist/PureClick.exe` |
| **Mac** | `mac/` | macOS | `mac/pureclick.py` or `mac/dist/PureClick for Mac.app` |

Both share server clock sync, seat ranking logic, and the browser seat autopilot
in `browser/`. Each edition has its own native click backend.

---

## Windows edition (repo root)

PureClick for Windows fires a native mouse click at a user-defined Interpark server time.

The default server target is the Interpark booking backend (the server that
assigns your place in the queue when sale opens):

```text
https://poticket.interpark.com/Book/BookMain.asp
```

This endpoint is uncached (`cache-control: no-store`, always `x-cache: Miss`),
returns a tiny response, and updates its `Date` header every second, which makes
it the most reliable clock to sync against. Avoid `nol.interpark.com/ticket` and
`tickets.interpark.com/...` for syncing: both are served through a CDN cache and
return a stale, frozen `Date` header.

## What It Does

- Watches the Interpark HTTP `Date` header roll over and anchors that server tick
  to a high-resolution monotonic clock, bracketing the exact second boundary
  between two consecutive polls.
- Shows the current estimated booking-server time in KST.
- Lets users enter target date, hour, minute, second, and millisecond.
- Lets users lock the click location with a 5-second cursor capture.
- Anchors server time to a monotonic high-resolution clock after sync.
- Pre-moves the cursor before the target time so the final action is only the
  click signal.
- Sends the real click on Windows with `SetCursorPos` and `SendInput`.
- Sends two optional internal retry clicks shortly after the first click.
- Provides a dry-run mode for testing without clicking.

## Run (Windows)

Use Python 3.11 or newer.

```bash
python pureclick.py
```

On macOS or Linux, use the dedicated Mac edition in `mac/` instead of the root app.

On Windows, you can also double-click:

```text
run_pureclick.bat
```

On Windows, `Arm` sends the real click. On macOS, use `mac/pureclick.py` for real clicks.

## Basic Use

1. Log in to Interpark in the browser.
2. Open the event page and select the date/round.
3. Open PureClick and wait for the server time to sync.
4. Enter the target KST server date and time.
5. Click `Lock Position`, move the cursor over `예매하기`, and wait 5 seconds.
6. Use `Test` first, then `Arm` on Windows.

## Phase 2 · Seat Autopilot (Onestop reserved shows)

Phase 1 clicks `예매하기` at the exact server time. Phase 2 locks a seat on the
onestop seat map so you can finish payment manually.

**Supported today:** shows where `isIngredientOnestop` and `isReservedSeat` are
both true (~85% of sampled concerts). Legacy `poticket` popups and GA shows are
not covered yet.

### One-time browser setup

1. Open PureClick and set grade order (default `2,3,4,1` = R, S, A, OP).
2. Click **Copy Config Snippet** and paste it in the Chrome DevTools console on
   `tickets.interpark.com` (sets `localStorage` for this browser profile).
3. Install **Tampermonkey**, click **Copy Userscript**, create a new script,
   paste, and save. It runs on `https://tickets.interpark.com/onestop/*`.

### Drop-day workflow

1. Log in, pick date/round, lock `예매하기` in PureClick (Phase 1).
2. **Arm** PureClick for the target KST server time.
3. After the queue, when `/onestop/seat` opens, the userscript scans seats in
   your grade order and calls `POST /onestop/api/seats/select`.
4. On success it redirects to `?step=price` — complete payment yourself.

The in-page overlay shows scan/lock status. In DevTools console:

```javascript
PureClickSeat.status()
PureClickSeat.setGradeOrder(["2", "3", "4", "1"])
PureClickSeat.run()
```

### Files

| File | Role |
|---|---|
| `pureclick_seat_core.py` | Grade ranking, payload builder, compatibility helpers |
| `browser/pureclick_seat_autopilot.js` | Core browser autopilot |
| `browser/pureclick_seat_autopilot.user.js` | Tampermonkey bundle |
| `pureclick_seat_config.json` | Saved desktop preferences (optional) |

## Send To A Windows User

Send the whole PureClick folder, including:

```text
pureclick.py
pureclick_core.py
pureclick_seat_core.py
browser/
run_pureclick.bat
windows_smoke_test.py
```

The recipient needs Python 3.11 or newer installed. After extracting the folder,
they can double-click `run_pureclick.bat`.

### Single `.exe` (no Python needed by the recipient)

To get one double-clickable file, build it once on a Windows computer. The
recipient of the final `.exe` does not need Python installed.

Easiest way: double-click `build_windows_exe.bat`.

Or from a terminal:

```powershell
.\build_windows_exe.ps1
```

The build itself requires Python 3.11+ on the build machine. The output is a
single file:

```text
dist\PureClick.exe
```

Send only `dist\PureClick.exe`. The recipient double-clicks it; nothing else is
required.

Note: the `.exe` must be built on Windows. It cannot be built from macOS or
Linux, because PyInstaller does not cross-compile.

## Windows Smoke Test

Before using a real click, run:

```powershell
py -3 windows_smoke_test.py
```

This prints median/worst local wait lateness and tick-catch sync quality. For
more samples:

```powershell
py -3 windows_smoke_test.py --runs 100 --samples 9
```

To test that Windows can send a real click, put the cursor somewhere harmless
and run:

```powershell
py -3 windows_smoke_test.py --click-test
```

That waits 3 seconds, then sends one click at the current cursor position.

## Target Time Format

Target time is interpreted as Interpark/Korea server time (KST):

```text
2026-05-28 20:00:00.000
2026-05-28T20:00:00.000+09:00
```

## Practical Accuracy

PureClick can schedule the local firing point very tightly, but exact server-time
clicking still depends on server header precision, network jitter, Windows
scheduling, browser focus, page rendering, popups, and display scaling. HTTP
`Date` headers are second-precision, so PureClick catches the moment that header
rolls over and anchors that tick to `perf_counter()`.

Use `Test` several times on the target Windows computer, then check
`pureclick_fire_log.csv` for recorded lateness and sync quality.

## Test

```bash
python -m unittest discover -s tests
```
