# CURSOR HANDOFF — 취켓팅 engine, build `trigger-v71` (2026-09-05)

Branch `macos-app`, working tree uncommitted on top of `21b2a85` (tagged
`v0.3.4`). `VERSION` (gitignored, read by the panel title) now says `0.3.5`.

## v71 — two blockers and defensive hardening (this pass)

**Proof:** `npm test` **184/184** · `pytest -q` **426 passed, 19 subtests, 0
failures** (two consecutive runs) · `node tests/journey_hold_lifecycle.mjs`
**66/66** (three consecutive runs) · `node tools/audit_js.mjs` **0 gating**.

### Blocker 1 — 선택 완료 씹힘 after a won preselect

`pressSequence` now does, after `waitForSeatNet("preselect")` answers ok:

1. **one full macrotask** (`await yieldFast()`, a MessageChannel hop, never a
   timer) before looking for the 선택 완료 button. The preselect answer wakes
   the sequence from inside the page's own network callback; the React commit
   that removes the seat from the page's in-flight set and enables the button
   runs later in the same task. Pressing before it landed on the page's
   `showToastIfSeatBusy` / `seat_requestPending` guard: a won race with a
   confirm that did nothing.
2. the press, as before (button must exist; hops, bounded 400 ms);
3. **commit watchdog `CONFIRM_WATCHDOG_MS = 120`**: both network hooks now
   stamp `net.selectSentAt` the moment `POST /onestop/api/seats/select`
   *leaves* (`noteSelectSent`, XHR `send` and `fetch` paths). If neither the
   send nor an answer is seen within 120 ms of the click, 선택 완료 is pressed
   once more, decisively (`confirmRepresses` counter, `lat.represses`,
   trace `confirmWatchdog`). A press the page did act on but the hook missed
   only meets the page's own busy toast, so the second press is cheap.

Journey §12: a page whose select leaves 30 ms after the click → exactly one
press; a page that never sends for 250 ms → two presses, one repress counted.
Static: the order preselect-answer → hop → click → watchdog is asserted.

### Blocker 2 — root `seatSelectHandler` stale-closure guard

`pressViaHandler` takes the root handler **only when
`selectedSeatCount() === 0` and the order's `quantity === 1`**. Otherwise it
drops to the block's `onSeatClick` (the page's own argument shape, its own
`validateSeatCount` on live state) when the fiber walk found one, else refuses
(`handlerMisses`) so `clickSeatOnMap` falls through to the pointer press.
`handlerRootRefused` counts the refusals. Journey §13: cart 1 → block handler
called as `(seat, false, blockKey)`; 매수 2 → block handler; no block handler
and cart 1 → refused. Journey §9 still proves the root path for the empty-cart
매수 1 case.

### The expiry modals are never auto-pressed

`NEVER_DISMISS_DIALOG` is now `{captcha, sessionExpired, holdExpired}` and
`onDialogMounted` checks it before the informational set, so the 10분/7분
timer modal (`좌석을 선택할 수 있는 시간 10분이 종료되었어요`) and every
session-expired copy are classified, reported (`lastDialog`), paused on, and
never clicked — from the mount observer and from `dismissAnyBlockingOverlay`
alike. New: `sessionExpireAt()` reads `initData.expireAt` /
`sessionExpireAt` / `expiresAt` (epoch s/ms or ISO) when the page carries it;
past that clock, an unlabelled small dialog is treated as the expiry modal.
Exposed in status as `sessionExpireAt`. Journey §14 proves both.

### Chrome flags, version

`mac/cdp.py` launches Chrome/Edge with `--disable-background-timer-throttling`
and `--disable-backgrounding-occluded-windows`, so an occluded 예매 창 keeps
foreground timer cadence for both the catch tick and the page's own
seatStatus poll. `VERSION` → `0.3.5`. The version test that read the
gitignored file as "unstamped" now points the lookup at an empty directory,
and a second test checks a stamped file reads as its version.

### Harness hygiene found on the way

`liveWatch()` resets the harness cart per scenario (the new root gate
correctly refused the carried-over cart, which is what surfaced it). The two
cold-first-press checks are 3 ms (measured up to 1.6 ms with pytest running
beside the harness; the sub-0.5 ms promise stays the bench's). The pytest
wrapper reports a failed check by name instead of erroring six tests.

### Diff for this pass

| file | change |
|---|---|
| `browser/nolsniper_autopilot.js` | build v71; `CONFIRM_WATCHDOG_MS`, `waitForSelectSent`, `noteSelectSent` in both hooks; macrotask hop + watchdog in `pressSequence`; root-handler gate in `pressViaHandler`; `NEVER_DISMISS_DIALOG` + `sessionExpireAt` / `sessionClockExpired`; status fields `confirmRepresses`, `handlerRootRefused`, `sessionExpireAt` |
| `mac/cdp.py` | two Chrome flags |
| `VERSION` | 0.3.5 |
| `tests/journey_hold_lifecycle.mjs` | §12 watchdog, §13 root gate, §14 expiry modals; cart reset |
| `tests/test_latency_ceilings.py` | `ReviewBlockersAreClosed` (4 tests); set literal updated |
| `tests/test_app_update.py` | isolated from the gitignored `VERSION`; stamped-file test |
| `tests/test_recovery_journeys.py` | wrapper reports instead of erroring |

---

## v70 (earlier today) — kept for continuity

Branch `macos-app`, working tree uncommitted on top of `21b2a85` (tagged `v0.3.4`).
Implements `docs/FINAL_COMPREHENSIVE_SYSTEM_AUDIT.md` findings F1, F2 (both
levers), F4–F7, M1–M4, T1–T3, L1, P2, on top of yesterday's v69 (gapless
poller, hold lifecycle). The v69 section is kept at the end for continuity.

## Test proof

| suite | result |
|---|---|
| `npm test` (tests/test_autopilot_picker.mjs) | **184/184** |
| `pytest -q` | **420 passed, 19 subtests**, 1 failure that is not ours (below) |
| `node tests/journey_hold_lifecycle.mjs` | **56/56** |
| `node tools/audit_js.mjs` | **0 gating findings** (advisory count unchanged after line-number normalisation) |

The one red test, `test_app_update.py::test_an_unstamped_checkout_reads_as_dev`,
asserts a checkout with no version stamp reads `(dev)`; HEAD now carries the
tag `v0.3.4` (created today), so `app_version()` reads `0.3.4`. It fails on the
untouched tree too. Either the tag moves off HEAD for that test or the test
learns that a tagged HEAD is a stamped build; not decided here.

## Benchmarks

`node tests/bench_catch_latency.mjs` — synthetic 21,600-seat venue, 1,800
circles mounted, median / worst ms:

| segment | v69 | **v70** |
|---|---|---|
| detect · applyBlockMask (1 block, one 0→1) | 0.2 / 0.7 | 0.2 / 0.7 |
| **press via handler · disabled circle → seatSelectHandler through the fiber** | — | **0.005 / 0.0** |
| click · clickSeatOnMap (find + pointer dispatch) | 0.015 | 0.016 |
| detect → press through pressSequence | 0.07 | **0.08** |
| cart notice lag past a 220+60 ms preselect+render | 7.2 | 5.8 |
| confirm · quiet gap held before 선택 완료 (must stay >100) | 185 | 186 |

`node tests/journey_hold_lifecycle.mjs` — the real poller and press sequence
in a vm against a stubbed feed (10 ms RTT), three workers churning:

| measurement | value |
|---|---|
| stream at the cap | 59–60 req/s, gap median **16.69 ms**, p95 17.28, max **17.44** |
| first (cold) pointer press, detect → events | 1.2 ms |
| first (cold) **handler** press, detect → `seatSelectHandler` called | **1.24 ms** |
| P41149 modal mounted → 확인 pressed | **5.3 ms** wall (0.71 ms inside the observer callback) |
| user deselect → PAUSED | 39 ms |

Ceilings asserted in `test_catch_latency_budget.py`: `handlerPressMs < 2`,
`detectToPressMs < 2` (CI-loose; the guarded regression costs milliseconds),
`quietGapMs > 100`.

## 1. The 1.75 s SWR redraw ceiling (F2) — two levers, both shipped

**Lever A — press through the page's own handler.** `pageHandlerFor(node)`
walks `fiber.return` from any drawn circle (≤40 levels) and returns, keyed on
shape only, the page root (`props.seatSelectHandler` is a function; `goods`
beside it) or, failing that, the block component (`onSeatClick` beside
`seatMeta` and `blockKey`). `pressViaHandler(seatInfoId, {blockKey, node})`
resolves the handler **at press time** (useCallback identity changes per
render), takes the seat in the page's own shape via `pageSeatObject()` — the
circle's fiber `seat` when drawn, else the seatMeta object from
`seatState.lastBlocks` — and calls
`seatSelectHandler(true, seat, blockKey, goods.isInterlocking, undefined, undefined)`
(root) or `onSeatClick(seat, false, blockKey)` (block).

Where it sits in `clickSeatOnMap` (config `press_via`, default `auto`):

| circle state | `auto` | `handler` | `pointer` |
|---|---|---|---|
| drawn, enabled | pointer (proven) | handler | pointer |
| drawn, `isDisabled` (page not yet redrawn) | **handler**, else `node-disabled` | handler | `node-disabled` |
| not drawn / detached | **handler** (needs no circle), else `no-node` | handler | `no-node` |

`pressSequence` no longer skips an undrawn seat when `handlerReachable()`.
After a handler press everything downstream is unchanged: the page's own
`PreselectSeat` lands in the XHR hook, `waitForSeatNet("preselect")` wakes,
선택 완료 goes the instant it answers, `select` locks, the hold guard starts.
Journey §9 proves the chain end to end; §10 proves the fallback (fiber
unreachable → disabled circle refused, enabled circle pointer-pressed, a
`handlerMisses` count).

`press_via` is sent by the panel (`NOLSNIPER_PRESS_VIA=handler|pointer`
forces a path for the live probe) and validated in `core/seat.py`
(`VALID_PRESS_VIA`).

**Still unprobed live, deliberately:** the audit calls this "structurally
sound and unprobed". `auto` only takes the handler where the pointer press
would be refused anyway, so a wrong guess costs nothing that was winnable.
First live check: `status().seat.lastPressVia`, `handlerPresses`,
`handlerMisses`, `lastHandlerKind`, and `lastCatchLatency.preselectMs` after
a catch on a disabled circle.

**Lever B — wake the page's SWR poll.** `nudgePageRefresh()` dispatches
`new Event("online")` on `window` from `applyBlockMask` the instant any 0→1
is seen (SWR `revalidateOnReconnect`, deduped 2 s server-side of SWR and
`SWR_NUDGE_MIN_GAP_MS` here; skipped while `document.hidden`, where SWR
would drop it). Harness bug found: the dedupe compared against time zero and
swallowed a flip inside the page's first 2 s — fixed (`swrNudgedAt > 0`).

## 2. F1 — our own polls no longer pass through our own hook

`fetchJson` sends `/onestop/api/seatStatus` through `window.__nolsniperNativeFetch`
**only while `window.fetch === window.__nolsniperWrappedFetch`** (a test spy
or a later hook installed there is honoured). Effect, proven in journey §2:
`lastFreedVia === "focus"` (the worker's own diff pressed), `pageStatusSeen`
stays 0 on a pure focus watch, the trace ring is no longer our own rows. The
page's traffic still flows through the hook (`pageFreed` path intact — node
test "a seat the page's own traffic shows opening is caught without a
request" still passes).

## 3. Queue API warming (F4, F5, F6)

- `preconnectEntWaiting()` — `<link rel=dns-prefetch>` + `<link rel=preconnect crossorigin=use-credentials>` for `ent-waiting-api.interpark.com`, once per document.
- `warmQueueApi(reason)` — two credentialed `application/json` POSTs whose results are discarded (`secure-url` `{}` and `line-up` `{key:""}`), bounded 3 s, so **both** CORS preflights are cached (no `Access-Control-Max-Age` → 5 s browser default) and TLS is up. Never inside `QUEUE_WARM_MIN_GAP_MS` (4 s). Reported in `status().arm.queueWarm` (`ms`, both HTTP statuses).
- `premintOnLanding(arm)` — `member-info` on the goods page, refreshed every `MINT_REFRESH_MS` (240 s) while parked; timer handle on `window`.
- Scheduled path: preconnect + landing pre-mint at scheduler start, the existing fresh mint at T−10 s, **warm-up at T−3 s** (inside the 5 s cache), then the burst.
- 지금 진입: on landing on `/goods/…` with an arm that is not counting down, all three run once.

Open live check (audit §2.2 caveat): that the gateway does not classify the
two empty POSTs differently from the burst's ~35 shots. `queueWarm.secureUrl`
/ `.lineUp` will show the statuses.

## 4. Modals (M1–M4) and the dead matcher

`classifyDialogText(text)` is the single classifier: `captcha` →
`sessionExpired` → `holdExpired` → `statusChanged` → `taken` → `error` →
`bookingNotice` → `unknown`.

| finding | change |
|---|---|
| M1 P41149 | `SEAT_TAKEN_DIALOG` now matches 좌석 상태가 변경 (and 이선좌); counted in `statusChangedDialogs` and `takenConflicts` |
| M2 session expired | `SESSION_EXPIRED_DIALOG` is in `NEVER_DISMISS_DIALOG`: `dismissAnyBlockingOverlay` and the observer never press its 확인 (it is `goToProduct`); `lastError` explains, `lastDialog` reports |
| M3 slider captcha | `isCaptchaPageCopy` matches 화살표를 밀어 / 퍼즐을 맞춰 |
| M4 7-minute hold | `HOLD_LIFETIME_MS = 420000`; `holdExpiresAt` set on lock, `holdRemainingMs` in status; the modal (`HOLD_EXPIRED_DIALOG`) or the timer +1.5 s → `onHoldExpired()` → `pauseWatch("holdExpired")` with the overlay text; nothing auto-pressed |
| <30 ms auto-dismiss | `installDialogWatch()`: a `MutationObserver` on `document.body` (childList, subtree) that skips records inside the seat-map root, classifies each mounted subtree, and presses 확인 on **informational** kinds only (`taken`, `statusChanged`, `error`) while the engine is running/holding. Journey §11: 5.3 ms wall, 0.71 ms in-callback; session-expired untouched; nothing pressed while idle |
| "동시 접속" | no such matcher exists anywhere in `browser/`, `mac/`, `core/` — nothing to purge; `test_m1_m4_modal_classification` now asserts it never appears |

## 5. O(1) hot-path pruning (T3, P1)

- `captchaPresent()`: one `querySelector` over `CAPTCHA_SHAPE` (class/id/name containing captcha, `input[maxlength=6|4]`, `canvas`, puzzle/slider classes) answers "no" **before** any `innerText` walk, and the answer is memoised for 100 ms so a 15 ms tick never pays twice.
- `selectedSeatCount()`: the scoped count node now has a `MutationObserver` on its parent (`watchSeatCountNode`) that drops the node the moment React swaps it out; the full-document `innerText` audit falls from every 25th read to every 200th while the observer is attached.

## 6. Clamp-safe fire path (F7), state traps (T1, T2), reload hygiene (L1)

- `waitUntilServerUnix` final approach and the secure-url burst spacing use `pauseFor` (clamp-detecting), never a bare `sleep`. The focused catch tick's 15 ms period too.
- T1: a `locked` flag with `selectedSeatCount() === 0` heals on any start, not only a button press.
- T2: while `document.visibilityState === "hidden"` the watch overlay appends **예매 창이 가려짐 — 앞으로 꺼내 두세요** in warn tone; `status().seat.pageHidden`.
- L1: overlay header interval handle on `window.__nolsniperOverlayHeadId`; seat-map observer on `window.__nolsniperSeatObserver`; dialog observer on `window.__nolsniperDialogObserver` — each disconnected/cleared by the next script instance.

## Diff, file by file (`git diff --stat`: 9 files, +1242 / −72, plus 3 new)

| file | what |
|---|---|
| `browser/nolsniper_autopilot.js` | build v70; `press_via` default; F1 bypass in `fetchJson`; `nudgePageRefresh`; `pageHandlerFor` / `pageSeatObject` / `anyRenderedCircle` / `pressViaHandler` / `handlerReachable`; `clickSeatOnMap` decision table; `pressSequence` undrawn-seat path; modal constants + `classifyDialogText` + `INFORMATIONAL_DIALOG` / `NEVER_DISMISS_DIALOG`; `dialogRootOf` / `onDialogMounted` / `installDialogWatch` / `onHoldExpired` / `holdRemainingMs`; captcha pre-check + memo; `watchSeatCountNode`; `preconnectEntWaiting` / `warmQueueApi` / `premintOnLanding` / `stopMintRefresh`; scheduler T−3 s warm-up; goods-landing warm; `pauseFor` on the fire path; T1/T2; L1 handles; status fields; `__test` hooks |
| `core/seat.py` | `VALID_PRESS_VIA`, `press_via` field, parse, mapping |
| `mac/nolsniper.py` | sends `press_via` (env-overridable) |
| `tests/journey_hold_lifecycle.mjs` | drivable `MutationObserver`, stable body, fiber tree above circles, `window.dispatchEvent` capture; §2 F1 + pulse checks; §9 handler; §10 fallback; §11 dialogs |
| `tests/bench_catch_latency.mjs` | fiber tree; `press via handler` row → `handlerPressMs` |
| `tests/test_catch_latency_budget.py` | `handlerPressMs` ceiling |
| `tests/test_latency_ceilings.py` | `AuditFindingsAreClosed` (9 tests) |
| `tests/test_autopilot_picker.mjs` | tick period regex → `pauseFor` |
| `docs/INTERPARK_NOL_ENDPOINT_SPEC.md` | four rows in the latency table |

## State-machine invariants (unchanged from v69, re-proven in the journey)

- held ⇒ no `seatStatus` request; PAUSED ⇒ no request, no press, `haltedByUser` sticky; only 감시 시작 resumes.
- `pauseWatch` never clears the map nor releases a hold; `userDeselect` and `holdExpired` drop the local lock only.
- a trusted `pointerdown` on a circle yields; our own synthetic events never do.
- `step=price` kills every poller.
- cap stays 60 req/s, even-spaced; `yieldFast` settles one waiter per message.

## Live checks for the next run, in order

1. A catch on a circle the page still draws disabled: `lastPressVia === "handler"`, `lastHandlerKind`, `preselectMs` one RTT, `handlerMisses` 0. If the page toasts 이전 요청을 처리 중이에요, the seat was already in the page's in-flight set (a double press) — check `fastClickedId` memo.
2. `swrNudges` rising with flips and `domAgreedMs` (page redraw after a flip) collapsing from ~1750 ms toward one RTT.
3. `status().arm.queueWarm` statuses after a scheduled open; no `GATEWAY_ABUSE_BLOCKED` from the two warm-up POSTs.
4. `pageStatusSeen` stays 0 on a pure focus watch; the trace ring holds real events again.
5. `fastDismissed` / `lastFastDismissMs` after any 이미 선점 / 좌석 상태가 변경.

---

## v69 (yesterday) — kept for continuity

Gapless poller under the 60/s cap (3 workers, 15 ms floor, spacing + window
pacer), `pauseFor` clamp detection, `yieldFast` FIFO fix, press-path warm-up,
hold lifecycle (userDeselect / humanTouch / priceStep), `dispatchGapMs` on the
line-up. Why the cap is not raised: `GATEWAY_ABUSE_BLOCKED` is measured (~165 s),
its threshold is not; 60/s is the highest rate that has run whole watches
unblocked. Why 선택 완료 is not pressed in the seat's tick: NOL answers
`seat_requestPending` → P40021 and the seat is lost; it is pressed the instant
the page's own preselect answers.
