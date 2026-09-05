# NOL Sniper — Final Comprehensive System Audit

**Date:** 2026-09-05 · **Scope:** `browser/nolsniper_autopilot.js` (working tree, build `trigger-v69`, uncommitted), `core/`, `mac/`, `app_platform/`, and the live Interpark Onestop front-end bundle (`onestop-v2-front/a71854`, fetched today) plus the waiting-room bundle (`tickets.interpark.com/waiting`, build `qoIJ7uuLug2fK0FCoFRMZ`).
**Method:** read-only. No source file was modified. Every claim is tagged:

- **[bundle]** read from the site's own minified JavaScript, downloaded today
- **[measured]** observed on this machine today, or recorded in the repo from a live run
- **[code]** what NOL Sniper's source does, with a `file:line` pointer into the current working tree
- **[inferred]** follows from the above but has not been exercised live

The line numbers refer to the working tree as of 17:40 today. The file is being edited concurrently (v68 → v69, +306/−30), so treat them as anchors, not guarantees.

---

## 0. Executive verdict

1. **The in-page hot path is at its floor.** Detection is RTT-bound (three chained `seatStatus` workers, 60 req/s cap), detection→press is a synchronous function chain ending in a real `pointerup`, and both the preselect and the confirm are awaited from network hooks rather than polled. Nothing sub-millisecond remains inside that chain.
2. **The race is not decided inside that chain.** It is decided by the page's own `PreselectSeat` reaching the origin. NOL Sniper cannot press a seat until NOL's own React tree has redrawn the circle as enabled, and **[bundle]** that redraw comes from an SWR poll with `refreshInterval` = random 3000–4000 ms. A freed seat is therefore pressable, on average, ~1.75 s after it frees, worst case 4 s. This is the single largest latency in the system and the primary cause of 이선좌.
3. **There is a bypass that is structurally sound and unprobed.** **[bundle]** The disabled gate lives only in the leaf `SeatSvgCircle`; the map-level `onSeatClick` → page-level `seatSelectHandler` chain never re-checks it. Calling that handler through the React fiber with our own seat object runs the page's own validation, optimistic cart update and `PreselectSeat`. This is not the "bare API preselect" dead end; it is the page's own code path minus the render wait. It must be probed before shipping (§1.6).
4. **A second, softer lever exists:** **[bundle]** the page's SWR instance revalidates on window `online` and `focus` events. A synthetic `online` event forces the page's own `seatStatus` refetch (deduped at 2 s), collapsing the 0–4 s render wait to ~one RTT plus a paint without touching the fiber.
5. **Queue entry has ~150–250 ms of removable latency on 지금 진입** (bridge poll, unminted signature, cold TLS, and two uncached CORS preflights — **[measured]** the queue API sends no `Access-Control-Max-Age`). The scheduled 오픈 대기 path still has one timer-clamp hazard on the final approach.
6. **Zero 이선좌 is not physically reachable.** It is a first-arrival race at the origin against other actors. A colocated headless client is faster than any browser-driven macro on physics the browser cannot access. What follows maximizes win probability; it does not and cannot guarantee it.

---

## 1. Domain 1 — Seat-map hot path and preselect chain

### 1.1 The React tree, verified from the bundle

Chunk `3225-00bfd03843b9b2aa.js` and page chunk `seat-6151b5e917961037.js`.

| Component / hook | Props or signature | What it does |
|---|---|---|
| `SeatSvgCircle` (memo, `displayName` set) | `{ id, className, fill, stroke, posX, posY, radius, isSelected, isDisabled, seat, blockKey, onSeatClick }` | Renders a transparent hit `rect`, the `circle`, and a check `image`. All three carry `onPointerDown: e=>e.stopPropagation()` and `onPointerUp: g`. **`g = (e) => { (!isDisabled \|\| isSelected) && (e.stopPropagation(), e.preventDefault(), onSeatClick(seat, isSelected, blockKey)) }`**. This is the only place `isDisabled` is consulted. |
| `isDisabled` computation (`L.il`) | `{ seat, seatIndex, isSelected, isBinaryMode, seatStatus, status }` | Binary mode (the normal mode): `!seatStatus \|\| seatStatus[seatIndex] === "0" \|\| !seat.seatGrade`. So the circle is disabled exactly when the **page's own** decoded `seatStatus` string for that block says `"0"` at that index. Constants: occupied `Gx="0"`, free `Fr="1"`. |
| `SeatSvgBlockBase` | `{ blockKey, seatMeta, seatStatus, ..., onSeatClick }` | Maps seats to circles; passes the map-level `onSeatClick` down unchanged. |
| Map hook `H` → `onSeatClick: S` | `(seat, isSelected, blockKey)` | `if (zoom?.pinchJustEnded) return; if (!bookWaitCount && seatClickableRef.current && seat) { group = seat.seatGroupId ? seatGroupMap.get(id) : undefined; seatSelectHandler(!isSelected, seat, blockKey, goods.isInterlocking, undefined, group?.length ? group : undefined); logEvent(...) }`. **`seatClickableRef.current = !disabled && !zoom.isPanning`, updated 100 ms after any pan state change.** |
| Page hook `B` → `seatSelectHandler: em` | `(select, seat, blockKey, skipNetwork, _, groupSeats)` | Refuses with toast `seat:seat_seatSelecting` if `b.current` (remove-all in flight) or any target id is in `E.current` (in-flight Set). Otherwise → `ed` (single select), `er` (group select) or `eu` (deselect). |
| `ed(seat, blockKey, skip)` | | `if (!validateSeatCount(seat)) return;` → `saveSelectedSeatInfo([...selected, new SeatInfo(blockKey, seat)])` **(optimistic: React state + `interpark/context.seats` in sessionStorage, BEFORE any network)** → `if (!skip) { setPreReserveExpireTime(now + 420000); await ea(seatInfo) }`. |
| `ea(seatInfo)` | | `if (hasValidContext()) { E.add(id); try { await api.preSelectSeat(blockKey, playSeq, seatInfoId, seatGrade); convertToBinaryMode(true) } catch (err) { removeContextSeat([id]); code = getErrorCode(err); if sessionExpired(code) → handleSessionExpiredError; switch(code) { P40054 SEAT_ALREADY_OCCUPIED → manualUpdateSeatStatus(blockKey,id,"OCCUPIED"); P41150 SEAT_ALREADY_PRESELECTED → manualUpdateSeatStatus(...,"PRESELECTED"); P41149 SEAT_STATUS_CHANGED → forceRefreshBlocks([blockKey]) } openModal(message) } finally { E.delete(id) } }`. |
| 선택 완료 → `validateAndSubmit: eh` | | `if (!validateSeatComplete() \|\| showToastIfSeatBusy()) return;` reads `seats` from `getContextData().seats` (sessionStorage) and requires `seats.length === selectedSeat.length`; POSTs `/onestop/api/seats/select` with `{goodsCode, placeCode, playSeq, seatType: "DEFAULT"\|"SPORTS", seats:[{seatGrade, seatInfoId}], sessionId, autoAssign:false}`; on 200 → `setReserveExpireTime(now + 420000)`; `router.push("/seat?step=price")`. |

**What this settles:**

- **The API soft-hold dead end is explained.** 선택 좌석 is React state written by `saveSelectedSeatInfo`, not by the preselect response. A bare mutation can never move it. (Consistent with the repo's 2026-09-03 probe.)
- **The disabled gate is leaf-only.** Neither `S` nor `em`/`ed`/`ea` re-checks `isDisabled`. They check: `pinchJustEnded`, `seatClickableRef` (panning), `bookWaitCount`, in-flight set, remove-all flag, `validateSeatCount`, `hasValidContext`.
- **Two seats clicked < 5 ms apart:** the second is refused only if its id is already in `E.current` (same seat) or a remove-all is running. Different seats proceed independently and both preselects go out. The page's own "one at a time" guard is per seat, not global. `validateSeatCount` (ticket max, mixed-grade rules) is the real limiter.
- **Selection is idempotent per click, not toggling at the network layer:** the circle's handler passes `isSelected`, and the page dispatches deselect when it is true. NOL Sniper's `fastClickedId` 1.5 s memo (`clickSeatOnMap`, line 6039) is what prevents a double press from turning into a deselect.

### 1.2 How the page refreshes `seatStatus` (the source of the render wait)

**[bundle]** Hook `T` in chunk 3225:

```js
[g, v] = useState(Math.floor(1001 * Math.random() + 3000));   // 3000–4000 ms, fixed per mount
useSWR(isBinaryMode && !bookWaitCount && !disabled && currentBlocks.length > 0
         ? `/seat-status?goodsCode=…&playSeq=…&currentBlocks=…` : null,
       fetchAllCurrentBlocks,
       { refreshInterval: serviceType === "PREVIEW" ? 0 : g, revalidateIfStale: true, keepPreviousData: true });
```

- Batching: a dataloader with `windowMs: 10, maxBatchSize: 2` — the same two-block cap NOL Sniper measured server-side is also enforced client-side.
- `getSeatsStatus` returns `seatModifiedAt` from the **`last-seat-modified` response header**; a change in it bumps the `/seat-meta` key (re-fetches layout), it does not shorten the status poll.
- Hook `X` sets the interval to `0` while the map is `disabled` (price step) and re-randomizes 3000–4000 when enabled.
- **SWR defaults apply:** `revalidateOnFocus: true` (`document visibilitychange` + `window focus`, throttled 5 s), `revalidateOnReconnect: true` (`window online`, deduped by `dedupingInterval` 2 s), `refreshWhenHidden: false`. **[bundle]** `initFocus`/`initReconnect` confirmed in `_app-0192555b6744e23f.js` @1272964.
- A `"WEBSOCKET"` `connectionMode` exists in the code paths but **no WebSocket client is present in this build** (the only `new WebSocket` is Next.js HMR). In that mode the page merely switches to per-seat `seatPlainStatus` REST. There is no push feed to exploit.

**Consequences for NOL Sniper:**

| Situation | Effect |
|---|---|
| Seat frees; NOL Sniper sees the bit within ~20 ms | The circle stays `isDisabled` until the page's next SWR tick: uniform 0–4 s, mean ~1.75 s. `clickSeatOnMap` returns `node-disabled` (line 6083) and the fast press is skipped; the seat falls back to the general `live` path on later loop ticks. |
| 예매 창 fully occluded (`document.visibilityState === "hidden"`) | `refreshWhenHidden: false` → **the page's own polling stops entirely.** Every fast press is refused as `node-disabled` until the window is visible again. A 조작판 dragged over the 예매 창 silently disables the whole take path. **[inferred from SWR defaults; unmeasured on WKWebView.]** |
| NOL Sniper pans/fits the block (`dragMapBy`, `fitBlockToView`) | `seatClickableRef` is false during the pan and for 100 ms after; a synthetic `pointerup` in that window is dropped by the map hook, not by the leaf. `parkInWatchedBlock` (line 3931) runs before the loop, so this only bites on re-parks. |

### 1.3 Network payloads and headers, verified

**Axios instance for `/onestop/api`** (`_app` @594808): `withCredentials: true`, **`timeout: 5000`**, headers `Content-Type: application/json`, `X-Requested-With: XMLHttpRequest`; interceptor (chunk 9732 @9790) adds `X-Onestop-Channel` (`channelType` or `"ONESTOP"`), `X-Onestop-Session` (`sessionId` or `"session_undefined"`), `X-Ticket-BFF-Language`, `Authorization: Bearer <goods.preOpt>` when present, `X-OneStop-Trace-ID` (16 chars).
**GraphQL instance** (`9732` @8548): `baseURL: /onestop`, `POST /gql` with body **`{ query, variables, operationName }`**, `timeout: 60000`.

```
POST /onestop/gql
{"query":"mutation PreselectSeat($command: PreselectSeatCommand!) { preselectSeat(command: $command) }",
 "variables":{"command":{"playSeq":"001","blockKey":"001:001","seatGrade":"1","seatInfoId":"…"}},
 "operationName":"PreselectSeat"}
→ {"data":{"preselectSeat":true}}   |   errors[].extensions.backendErrorCode = P40054 / P41150 / P41149 / …

POST /onestop/api/seats/select
{"goodsCode","placeCode","playSeq","seatType":"DEFAULT","seats":[{"seatGrade","seatInfoId"}],"sessionId","autoAssign":false}
→ 200 {…,"unselectableSeatInfoIds":[]}  |  400 P40021 / P40054 / P41149 / P41154 / P41155 / P40051
```

NOL Sniper's own `gql()` (line ~7440) omits `operationName`. Harmless functionally; a trivial fingerprint difference from the page. `onestopHeaders()` (line ~1170) otherwise matches the interceptor.

### 1.4 Server error codes and the exact modal copy

**[bundle]** code map (chunk 4895 @4168, module 6285) joined with **[measured]** `https://tickets.interpark.com/onestop/locales/ko/error.json` (28 KB, fetched today):

| Code | Constant | Modal text (ko) | Page's reaction |
|---|---|---|---|
| P40054 | SEAT_ALREADY_OCCUPIED | 이미 선점된 좌석입니다. | preselect: mark seat OCCUPIED locally, modal. select: same per unselectable id. |
| P41150 | SEAT_ALREADY_PRESELECTED | 이미 선점된 좌석입니다. | mark PRESELECTED locally, modal |
| P41149 | SEAT_STATUS_CHANGED | 좌석 상태가 변경되었습니다.\<br\>다른 좌석을 선택해 주세요. | `forceRefreshBlocks([blockKey])` (re-fetch meta), modal |
| P40021 | CONFIRM_PRESELECTION_INVALID | 좌석 요청이 잘못 되었습니다. | on select: deselect unselectables, modal |
| P40051 | EXCEED_TICKET_MAX_COUNT | 예매 가능 매수를 초과하였습니다. | modal |
| P41154 | SEAT_GROUP_COUNT_MISMATCH | (generic) | deselect, modal |
| P41155 | DEAD_SEAT_VALIDATION_FAILED | (dead-seat notice) | modal with Kinesis log |
| P41147 / P41148 | UNUSUAL_APPROACH / LONG_TERM_INACTIVITY | 예매를 진행할 수 없습니다. / (session) | treated as **session expired** → modal → `goToProduct` |
| IE0006 | GATEWAY_SESSION_EXPIRED | 세션이 만료되었습니다. | session expired |
| P40057 | PRODUCT_NOT_OPEN | 오픈 전 상품입니다. | deselect all, `router.replace("/error?errorCode=P40057")` |
| P40056 | (no constant) | 부정 예매 좌석 선점입니다. | generic modal |
| P41151/2/3 | LIMIT_EXCEPT_* | 이미 선택하신 좌석과 종류가 달라… / 추가로 선택할 수 없어요 / 이미 예매한 내역이 있어… | modal |
| P00002 | INVALID_VALUE | 값이 잘못되었습니다. | modal |
| toast | `seat:seat_seatSelecting` | 이전 요청을 처리 중이에요. 잠시 후 다시 시도해 주세요. | shown when a click lands while the same seat's request is in flight |
| toast | `seat:seat_exceedCountLimit` | 선택 가능한 매수를 초과했어요. | `validateSeatCount` failure |
| modal | `seat:seat_errorSelect` | 선택하신 좌석이 이미 선점되었습니다.\<br\>다른 좌석을 선택해주세요. | |
| modal | `seat:captcha_title` | 화면의 문자를 입력해주세요 | captcha; also a slider-puzzle variant `captcha_slider_title` 화살표를 밀어 퍼즐을 맞춰주세요 |
| modal | `common:session_timer_expired_title` | 좌석을 선택할 수 있는 시간 10분이 종료되었어요 | |
| `error_title_E05` | | 현재 접속이 원활하지 않습니다. | gateway congestion |
| `error_message_1800` | | 판매시작 시간 이전에 접속한 기록이 있습니다. 예매창을 닫고 다시 시도해주세요. | pre-open access flagged |

**There is no "동시 접속이 감지되었습니다" string anywhere in the onestop locale files or bundle.** The nearest real messages are P41147 (예매를 진행할 수 없습니다), `error_message_1800`, and `ME7922` (동일 세션으로 이미 예매가 완료되었습니다). If a user reports "동시 접속", it is coming from the NOL/SSO side, not the seat map.

**Hold lifetime:** `su = Jd = 420000` ms (7 minutes) for both the pre-reserve (after preselect) and reserve (after select) timers. The UI copy says 10분; the constant is 7.

### 1.5 The detection→press chain in NOL Sniper, and one ordering defect

**[code]** `focusWorkerLoop` (line 5617): `fetchMasksFor` → `applyBlockMask` → `rankCandidates` → `pressSequence` (line 5675) → `clickSeatOnMap` (6039) → `firePointerSelect` (6095) dispatching `pointerdown` + `pointerup` with `pointerType: "mouse"`, `isPrimary`, `button: 0`, coordinates from `getBoundingClientRect`. v69 passes the verified node in and defers the post-press `elementFromPoint` trace by a `MessageChannel` hop. That is the floor for a DOM press.

**F1 — The fetch hook diffs our own `seatStatus` before the worker does, so the worker's in-callback press never fires.** **[code, high confidence; inferred live effect]**
`installNetworkWatch` (7381) wraps `window.fetch`. NOL Sniper's own `fetchJson` (6144) calls the wrapped `fetch`. For every matching URL the wrapper `await response.clone().text()` → `notePageSeatStatus(url, text)` → `applyBlockMask(block, mask)` **before** returning the response to `fetchJson`. `applyBlockMask` (8936) sets `block.mask = mask` and pushes freed seats into `seatState.pageFreed`. When the worker then calls `applyBlockMask` with the same decoded mask, `previous` already equals `mask`, so `freed` is empty and the `pressSequence` branch in the worker is dead. Detections instead travel through `pageFreed` → the main loop tick (`await sleep(CATCH_FOCUS_POLL_MS)` = 15 ms, **a `setTimeout`, clamp-prone**) → the loop's "focused fast press" branch. Evidence: the live state file shows `pageStatusSeen = 12117` on a run whose only traffic was our own single-block focus poll, and `trace` holds nothing but `page:seatStatus` rows for one block. Cost: up to one loop period plus timer clamp (≈1 s when the 예매 창 is not frontmost) inserted between detection and press.
**Fix (either):** route our own requests through `window.__nolsniperNativeFetch` in `fetchJson`, or make `notePageSeatStatus` ignore responses whose `X-OneStop-Trace-ID` we minted (store the trace id in a Set in `onestopHeaders()` and check the request headers in the wrapper).

```js
// fetchJson — bypass our own hook for our own polls
const doFetch = window.__nolsniperNativeFetch || fetch;
const response = await doFetch(url, { credentials: "include", ...options, headers });
```

**F2 — Detect→press is instantaneous only when the circle is already enabled; otherwise it is the page's 3–4 s poll.** See §1.2 and §1.6.

**F3 — `pressSequence` presses only the top 4 (`PRESS_SNAP_MAX`) and awaits each preselect serially.** Correct behavior for 매수 1 (a second concurrent preselect would exceed the allowance), so not a defect. Noted because a two-seat order (`quantity ≥ 2`) never uses the fast path at all (`config.quantity === 1` guard, line ~5666).

### 1.6 The bypass: calling the page's handler through the fiber

**Why it is not the dead end:** the dead end was a bare GraphQL mutation that never touched React. The page's own `seatSelectHandler` writes `selectedSeat` and `interpark/context.seats` **before** it sends the mutation and removes them on failure. Invoking it is invoking the page.

**What must hold [bundle-verified]:** `hasValidContext()` (goods + playSeq in context), the seat not in the in-flight Set, `validateSeatCount(seat)` true, `seatClickableRef.current` is irrelevant (that check lives in the map hook `S`, which we skip too, so avoid calling during our own pan).

**Locating it:** from any rendered seat circle walk `fiber.return` until `memoizedProps.onSeatClick` is a function **and** `memoizedProps.seatMeta`/`blockKey` exist (that is `SeatSvgBlockBase`, whose `onSeatClick` is the map hook's `S`). Calling `S(seat, false, blockKey)` still applies the `pinchJustEnded`/`seatClickableRef` guards, which is acceptable. To skip those too, walk further to the component whose props carry `seatSelectHandler` (the `F.J`/`er` map root) and call `seatSelectHandler(true, seat, blockKey, goods.isInterlocking, undefined, groupSeatsOrUndefined)`.

```js
// PROPOSED, UNPROBED. Walk from a rendered circle to the block component and press via its handler.
function pageHandlerFor(node) {
  const key = Object.keys(node).find((k) => k.startsWith("__reactFiber"));
  let fiber = key ? node[key] : null;
  for (let depth = 0; fiber && depth < 24; depth += 1) {
    const p = fiber.memoizedProps || {};
    if (typeof p.seatSelectHandler === "function") return { kind: "root", fn: p.seatSelectHandler, goods: p.goods };
    if (typeof p.onSeatClick === "function" && p.blockKey && p.seatMeta) return { kind: "block", fn: p.onSeatClick };
    fiber = fiber.return;
  }
  return null;
}
function pressViaHandler(seat, blockKey) {
  const anyCircle = seatIndex.byId.values().next().value;   // any rendered circle in the open 구역
  const h = anyCircle && pageHandlerFor(anyCircle);
  if (!h) return false;
  if (h.kind === "root") h.fn(true, seat, blockKey, Boolean(h.goods?.isInterlocking), undefined, undefined);
  else h.fn(seat, false, blockKey);
  return true;
}
```

`seat` must be the page's shape: `{ seatInfoId, seatGrade, blockKey?, seatGroupId?, rowIdx?, colIdx?, posLeft, posTop, ... }`. Use the object from `seatMeta` (`block.seats[pos]`, already held in `seatState.lastBlocks`), not a `toCandidate()` projection.

**Probe before shipping:** extend `probeSoftHold` (line ~9236) with a `via: "handler"` mode: call `pressViaHandler` on one free seat while the circle is still drawn enabled, watch `selectedSeatCount()` rise and `net.preselectOk`, then `releasePreselected`. Then repeat on a seat whose circle is still `isDisabled` (immediately after a bit flip) to prove the gate is bypassed. Success criteria: cart rises within one RTT; no `seat_seatSelecting` toast; 선택 완료 subsequently answers 200 in a controlled run.

**Risks:** minified prop names are stable only within a build; key the lookup on shape (`seatSelectHandler`, `onSeatClick` + `seatMeta`), never on names like `em`/`eb`. The React `useCallback` identity changes on every render; resolve it at press time, not at start.

### 1.7 The soft lever: forcing the page's own refresh

**[bundle]** SWR `initReconnect` listens to `window online`; `revalidateOnReconnect` is true by default and revalidates the `/seat-status` key, deduped at 2 s. `initFocus` listens to `window focus` and `document visibilitychange`, throttled 5 s.

```js
// PROPOSED, UNPROBED. Ask the page to redraw now instead of in 0–4 s.
function nudgePageRefresh() {
  window.dispatchEvent(new Event("online"));
}
```

Fire it from `applyBlockMask` the instant a `0→1` flip is seen. Expected effect: the page re-fetches status for the open blocks within ~10 ms batching + one RTT, re-renders, and the DOM press then succeeds on the next loop tick. Bound: once per 2 s (dedupe). This is the cheapest change in the whole document and it attacks the largest latency.

---

## 2. Domain 2 — Open-drop queue entry and line-up chain

### 2.1 The chain as built **[code]**

```
지금 진입 (panel)  →  state file  →  host mtime poll 120 ms  →  evaluate_js  →  NOLSniper.enterNow()
  → mintMemberInfo()   GET  tickets.interpark.com/api/ticket/v2/reserve-gate/member-info      (line 2690)
  → fetchSecureUrl()   POST ent-waiting-api.interpark.com/waiting/api/secure-url             (line 2122)
  → enterQueueDirect() POST …/waiting/api/line-up  →  GET …/waiting/api/rank every 150 ms    (line 2858)
  → location.href = oneStopUrl   (…/onestop/…)     or fallback location.href = waitingUrl (/waiting?key=…)
```

`/gates/partner` is **not** on this route. It is the SSO fallback taken only by `enterFromNolPage` when the 예매 창 is on nol.yanolja.com or when `place_code` is known and the button never enabled.

### 2.2 Hidden costs, measured today from this machine

| Item | Measurement | Where it bites |
|---|---|---|
| Cold TCP+TLS to `ent-waiting-api.interpark.com` | connect 9.5–17.8 ms, TLS complete 29–38 ms, first byte 42–48 ms | 지금 진입 always (no preconnect); scheduled path only on the first −400 ms shot |
| Cold TCP+TLS to `tickets.interpark.com` | connect 14.6 ms, TLS 29.9 ms, TTFB 55 ms | member-info if the page tab has not talked to the origin recently (rare) |
| **CORS preflight on every POST to the queue API** | `OPTIONS` answered 204 with `access-control-allow-origin: https://tickets.interpark.com`, `allow-credentials: true`, `allow-headers: Accept, Content-Type, X-Requested-With, Authorization`, `allow-methods: GET, POST, …` and **no `Access-Control-Max-Age`** | Browser default preflight cache is 5 s (Chrome and WebKit). `secure-url` and `line-up` are different URLs, so each gets its own preflight. On 지금 진입 that is **two extra round trips** (≈10 ms each warm, ≈40 ms each cold) in front of the position-deciding `line-up`. On the scheduled path the −400 ms shot warms `secure-url`'s preflight, but `line-up`'s preflight is still cold at the open. |
| member-info minted at press time | ~60 ms warm **[measured, repo]** | 지금 진입 only; the scheduler pre-mints 10 s early (`PREMINT_LEAD_MS`) |
| Bridge poll | `mac/browser_host.py:391` and `mac/chrome_host.py:289` stat the state file every 120 ms | 0–120 ms, mean 60 ms, before `enterNow()` even starts |
| Panel park | `_wait_for_entry_origin` polls every 200 ms a context that the host refreshes every 400 ms (`mac/nolsniper.py:3355`) | 1–2 s when the 예매 창 was on NOL |

**F4 — Preflights.** Both POSTs are `Content-Type: application/json` with credentials, hence non-simple. The page's own waiting-room bundle does the same (`fetch(…, {method:'POST', credentials:'include', headers:{'Content-Type':'application/json'}})` **[bundle]**), so this is not a NOL Sniper mistake; it is a cost NOL Sniper can pre-pay and the page cannot. A warm-up preflight can be triggered ~3 s before the fire with a request that the server will reject harmlessly but that shares the URL: a `line-up` POST with an empty key returns an error body (handled by `decideLineUp` → fallback path is not taken because we discard the result). Alternatively send the warm-up as `Content-Type: text/plain` — **not advisable**: the server may 415, and it would not warm the `content-type` preflight anyway. Preferred: dedicated warm-up POSTs at T−3 s to both URLs, results discarded.

```js
// PROPOSED. Pre-pay TLS + both preflights ~3 s before the burst (scheduled) or on page land (지금 진입).
async function warmQueueApi() {
  const opts = (body) => ({ method: "POST", credentials: "include", body: JSON.stringify(body),
                            headers: { "Content-Type": "application/json", Accept: "application/json" } });
  await Promise.allSettled([
    fetch(`${ENT_WAITING_ORIGIN}${SECURE_URL_PATH}`, opts({})),       // 400 UnableReservationTime / invalid — discarded
    fetch(`${ENT_WAITING_ORIGIN}${LINE_UP_PATH}`,    opts({ key: "" })), // error body — discarded
  ]);
}
```
Caveat: an empty-body `secure-url` may be counted by the gateway's abuse logic like any other shot. The scheduled burst already sends ~35 shots; two more 3 s early are within the same budget. Verify the gateway does not classify the empty POST differently (one live run).

**F5 — Pre-mint on 지금 진입.** `mintMemberInfo` caches for `SIGNATURE_MAX_AGE_MS` = 300 s. The signature was still accepted at 601 s of age **[measured, repo]**; the exact TTL is unmeasured (it is stamped `…<hex>.<unix>`, so it is time-based, not rollover-based). Mint it when the page lands on `tickets.interpark.com/goods/…` and refresh every 240 s while parked; `enterNow` then skips the GET.

**F6 — Preconnect the queue API.** `preconnectQueueHost` (line 3686) warms only the remembered queue *page* host, never `ENT_WAITING_ORIGIN`. Add a `<link rel="preconnect" href="https://ent-waiting-api.interpark.com" crossorigin>` at document start on the goods page. Saves ~30 ms on the first POST.

**F7 — `waitUntilServerUnix` still uses `setTimeout` in 20 ms steps** (line 881, `sleep(Math.min(20, remainingMs - 4))`). v69 added `pauseFor` (line 829) with clamp detection but wired it only into the focus worker. WKWebView clamps timers to ~1 s when the window is not frontmost **[measured, repo]**; one clamped step lands the fire up to a second late. Same for the 20 ms burst spacing in `enterViaSecureUrlWithRetries` (line ~2760).

```js
// waitUntilServerUnix — replace the final approach
await pauseFor(Math.min(20, remainingMs - 4));
// enterViaSecureUrlWithRetries — burst spacing
await pauseFor(Math.max(0, interval - (performance.now() - startedPerf)));
```

### 2.3 Non-200 from `line-up`, and every fallback **[code + core/entry.py]**

`decideLineUp(data, status)`: any `status !== 200`, any `error` string, or a missing `waitingId` → `action: "fallback"`. `enterQueueDirect` then returns `{navigated:false, outcome:"line-up: <reason>"}` and `enterViaSecureUrl` does `location.href = waitingUrl`. The waiting page lines the key up **again** (line-up is not idempotent **[measured, repo]**), so this fallback costs the place that the failed call may already have assigned. A thrown fetch (timeout 3 s, network) takes the same fallback. `exist: true` (key already lined up) is treated as our own earlier line-up and polled. `rank` fallbacks: `-1/-1` ExpiredSession, `myRank 0` without `sessionId` after 4 s grace → ExpiredExistedSession → navigate. Five consecutive rank fetch throws → navigate. 15-minute rank window.

**[bundle, new today]** The waiting page also calls **`POST /waiting/api/refresh-url {key}`** (obfuscated chunk `index-2d4cb1c81dff671f.js` @606825) on expiry. Not modelled in `core/entry.py`; document it as the page's own recovery for a stale key.

### 2.4 Signature expiry

Format `<…hex>.<unix>` **[measured, repo]**. Accepted at 121 s, 301 s, 601 s. Upper bound unknown. The JS cache (`SIGNATURE_MAX_AGE_MS` 300 s) is conservative. A single probe minting once and retrying `secure-url` every 60 s on a quiet open show would fix the number; `tools/probe_entry_chain.py` is the place.

---

## 3. Domain 3 — State machine and modal coverage

### 3.1 Coverage matrix

| Modal / condition | Detected by **[code]** | Dismissed / handled | Gap |
|---|---|---|---|
| 이미 선점된 좌석입니다 (P40054/P41150) | `SEAT_TAKEN_DIALOG` regex (line 1429), `seatTakenDialogVisible`; also `net.preselectOk === false` via the XHR hook | `dismissSeatTakenDialog`; seat marked taken 30 s (`markSeatTaken`), next candidate pressed | none functional; the dialog dismiss runs from the 400 ms watcher only while `seatState.running` |
| 좌석 상태가 변경되었습니다 (P41149) | not matched by name; caught by `SEAT_ERROR_DIALOG` only if copy contains 좌석 선택 도중 오류 / 좌석 요청이 잘못 … — **it does not** | `blockingOverlayNodes` → `dismissAnyBlockingOverlay` (generic 확인 press) | **Gap:** P41149 is neither "taken" nor "error"; it lands in `unknownDialog`. Add `좌석\s*상태가\s*변경` to `SEAT_TAKEN_DIALOG` (semantically "gone, move on"). |
| 좌석 요청이 잘못 되었습니다 (P40021) / 선택 도중 오류 / seat_requestPending | `SEAT_ERROR_DIALOG` | `dismissSeatErrorDialog`; `recoverFailedConfirm` | ok |
| 선택 가능한 매수를 초과했어요 (toast) | `SEAT_ERROR_DIALOG` | `held > quantity` branch → 전체삭제 | ok |
| 예매 가능 매수를 초과하였습니다 (P40051, on select) | `net.selectOk=false`; text via `unknownBlockingDialogText` | releases held ids (line ~10808) | ok |
| 이전 요청을 처리 중이에요 (toast, `seat_seatSelecting`) | `SEAT_ERROR_DIALOG` matches 요청 처리 중 | waits | ok |
| 세션이 만료 / 예매를 진행할 수 없습니다 (IE0006, P41147, P41148) | `loginState()` header scan; select 4xx text 로그인/세션 | reported as 로그인 필요 | **Gap:** the seat page's session-expired modal navigates to the product page via `goToProduct` on 확인. `dismissAnyBlockingOverlay` pressing 확인 there **triggers that navigation**. Exclude copy 세션이 만료 / 예매를 진행할 수 없습니다 from auto-dismiss. |
| 오픈 전 상품입니다 (P40057) | no pattern | page itself navigates to `/error?errorCode=P40057` | URL change → `bootRoute`; acceptable |
| 보안문자 (`captcha_title`, image or slider puzzle) | `captchaPresent` (line 1238) by copy 화면의 문자 + `img/canvas/input` | waits up to 120 s for the user | **Gap:** the slider variant's copy is 화살표를 밀어 퍼즐을 맞춰주세요; `isCaptchaPageCopy` must be checked to match it (it currently keys on 화면의 문자). |
| 좌석 선택 시간 10분 종료 (`session_timer_expired_title`) | none | none | **Gap:** after the 7-minute (`420000` ms) pre-reserve timer the page opens a blocking modal; the watch keeps polling under it. Detect 종료되었어요 and stop with a clear message. |
| 취소/환불 안내, 확인하고 예매하기 | `refundNoticeVisible`, `findBookingNoticeConfirmButton` | pressed | ok |
| 현재 접속이 원활하지 않습니다 (E05) / GATEWAY_ABUSE_BLOCKED | `readGatewayBlock` on every 4xx | run stops; countdown | ok |
| 동시 접속이 감지되었습니다 | — | — | **Not a seat-map string.** Exists in neither `error.json`, `seat.json`, `common.json` nor the bundle. If seen, it is NOL/SSO. |

### 3.2 State traps found **[code]**

- **T1 — `seatState.locked` survives `stopAll`** by design (a second 취켓팅 after a catch must not release the held seat), but `pauseWatch("userDeselect")` clears it only on that path. A user who releases the seat by hand while the watch is stopped leaves `locked=true`; the next 감시 시작 refuses. `runSeatAutopilot` (line ~9848) reports this; it should also self-heal when `selectedSeatCount() === 0` at start.
- **T2 — Occluded window stops the page's own poll** (§1.2). The overlay reads 감시 중 while no press can ever succeed. Detect `document.visibilityState === "hidden"` and say so on the overlay; the `online` nudge (§1.7) does not help here because SWR checks `isVisible()` before revalidating.
- **T3 — The catch loop's per-tick `captchaPresent()` reads `innerText` on every `div/section/article/aside/[role=dialog]`** (line 1245). On a 15k-node map this is milliseconds every 15 ms tick, and it is the one per-tick call that walks the whole document. Guard it with a cheap pre-check (`document.querySelector('input[maxlength="6"], [class*="captcha" i]')`) before the text walk.
- **T4 — `dismissAnyBlockingOverlay` presses the first 확인** it finds in any node whose class contains dialog/modal. With the session-expired modal that is a navigation (see matrix).
- **T5 — `configApplied()` re-runs `bootRoute()`** whenever the panel writes config; refused while running, but a config push during the ~1–3 s startup window before `seatState.running` flips true can start a second run. `runSeatAutopilot` bumps `__nolsniperRunGen` so the first exits; the cost is a repeated park (up to 2 s).

---

## 4. Domain 4 — Timers, loops, and memory

### 4.1 Every scheduler in the page script **[code]**

| Site | Kind | Period | Lifecycle across `reload_autopilot` |
|---|---|---|---|
| line 950 | `setInterval` overlay header | 500 ms | **no handle → one extra interval per reload (leak, ~µs each)** |
| line 11797 | `setInterval` URL/route watcher | 400 ms | cleared via `window.__nolsniperWatchId` (line 249) ✔ |
| line 5882 | `setInterval` hold guard | 300 ms | cleared by `stopHoldGuard`, called from `stopAll` ✔ |
| line 853 | `MessageChannel` `fastChannel` | per yield | one per reload, unreferenced afterwards (GC'd) |
| line 829 | `pauseFor` probe timer | 4 ms one-shot | ok |
| line 5617 | focus workers ×3, fetch-paced | ~RTT, ≤60/s | old epoch exits after its in-flight fetch ✔ |
| main catch loop | `sleep(15)` (`setTimeout`) | 15 ms | run-gen retired ✔; **clamp-prone when not frontmost** |
| line 5309 | `MutationObserver` on the seat root | per mutation | **closure-scoped; not disconnected on reload** → after N reloads, N observers each run `indexSeatNode` + `checkDomAgreement` per mutation |
| line 5134 | window `pointerdown/up/cancel` (drag guard) | — | guarded by `__nolsniperMapPointerWatch` ✔ but handler closes over the **old** `pointerHeldOnMap` after reload (new script's flag never set) |
| line 5938 | document `pointerdown` capture (human-touch guard) | — | guarded; handler indirected via `window.__nolsniperOnHumanPointer` ✔ |
| line 7484 | XHR `loadend` per page request | — | per request, released ✔ |
| Python `browser_host.py` | `watch_state` 120 ms, `poll_context` 400 ms, `heartbeat` 500 ms, cookie dump every 10 s | — | threads, daemon |
| Python `nolsniper.py` | `_poll_show` 500 ms, `_tick_server_time` 100 ms, trigger worker 500 ms | — | tk `after` chain |

### 4.2 Bounded collections **[code]**

`trace` 24 (`TRACE_LIMIT`), `catchTimings` 12, `waitingLog`/`clickLog` 40, `__nolsniperPopups` 20, `focusPoller.sent/responses` pruned to a 1 s window, `domAgreeWatch` pruned at 15 s, `takenUntil`/`unreachableUntil` swept, `seatNetWaiters` resolved or timed out, `pageFreed` spliced per tick, `heldSeatIds` cleared on release. **No unbounded growth found in a single script instance.** Growth across reloads: the observer and the overlay interval above.

### 4.3 Per-request and per-tick CPU on the main thread (30-minute projection)

At 60 req/s for 30 minutes = 108,000 requests. Per request **[code]**:

| Work | Estimated cost | Notes |
|---|---|---|
| `withLivePlaySeq` → `querySelectorAll("circle.js-seat")` for a count | 0.2–3 ms on large maps | redundant: the MutationObserver already invalidates `livePlaySeq` on `childList` |
| `getInitData()` → `readInterparkContext()` → up to 3× `JSON.parse(sessionStorage)` | 50–300 µs | called again inside `fetchJson` although `initData` was passed |
| `randomTraceId` + header object | ~5 µs | |
| fetch wrapper: `response.clone().text()` + 3 regexes + `traceCall` | 50–200 µs | plus one trace ring rotation per request |
| `parseSeatStatus` → Boolean array (4 per hex char) | ~20 µs / 500 seats | |
| `applyBlockMask` diff | O(seats) ~10 µs | runs twice per request today (F1) |

≈ 0.5–4 ms per request on the main thread → 3–24 % of one core at 60 req/s on a big venue, plus per-tick work every 15 ms: `captchaPresent` (T3), `blockingOverlayNodes` (substring-class selector + rect + computed style per hit), `selectedSeatCount` (scoped, but a full `body.innerText` every 25th read ≈ every 0.4 s), `collectFromBlocks` + `rankCandidates` + `liveSignature` over all polled seats, `freeSeatCount()` twice. Memory: request-scoped allocations only; GC churn, not growth. **Measured live proxy:** `focusTickWorkMs = 1` and `domScanWorstMs = 7` on this machine's last run (a small block). The status field `focusTickWorkMs` is the number to watch on a large venue.

### 4.4 The trace ring is polluted by our own polls

Because the fetch hook labels our requests `page:seatStatus`, the 24-entry trace on a focus watch contains nothing else (verified in the live state file), and `pageStatusSeen` counts our polls. Diagnostics for a lost race are thereby erased. Fixing F1 fixes this too.

---

## 5. Domain 5 — Host parity (macOS WKWebView vs Windows Chrome/CDP)

| Aspect | `mac/browser_host.py` (WKWebView via pywebview) | `mac/chrome_host.py` + `mac/cdp.py` (real Chrome/Edge) | Parity risk |
|---|---|---|---|
| Document-start injection | `WKUserScript` at document start, all frames (`app_platform/darwin.py:62`) | `Page.addScriptToEvaluateOnNewDocument` with explicit remove of the previous id (`cdp.py:368`) | equal; Chrome path avoids stacking, WebKit replaces via `removeAllUserScripts` |
| Snapshot read | `evaluate_js` marshalled to the UI thread, wrapped in a thread with **8 s** ceiling (`_call_with_timeout`, line 210) | `Runtime.evaluate` pinned to the main-frame context, **10 s** (`EVALUATE_TIMEOUT`), real socket deadline | Chrome path is safe against GIL/UI-thread hangs by design (repo memory); WebKit path relies on the watchdog |
| Poll cadence | 400 ms / 120 ms / 500 ms heartbeat | identical | equal |
| Timer clamping when not frontmost | WKWebView clamps to ~1 s **[measured, repo]** | Chrome: 1 s alignment for hidden tabs; **intensive throttling** (1 wake/min for timer chains) after 5 min hidden. A separate window that is visible is not "hidden", but an occluded one is. | v69 `pauseFor` covers the worker; F7 covers the fire; the main catch loop's `sleep(15)` is still a timer on both |
| `document.visibilityState` when occluded | WebKit marks occluded windows hidden | Chrome marks fully occluded windows hidden (occlusion tracking) | both stop the **page's** SWR poll (T2) |
| Fingerprint | Safari UA, `window.open` shim, form-target rewrite | `--disable-blink-features=AutomationControlled`, no `--enable-automation`; `navigator.webdriver === false` **[measured, repo]** | Chrome path is closer to a real user |
| Cookies | explicit jar save/restore every 10 s (`browser_session.py`) | Chrome profile dir | Chrome path survives crashes without the 10 s window |
| Recovery | hang → `os._exit(3)`, panel relaunches; 75 misses → `load_url` | supervisor relaunches Chrome or re-attaches the tab (`RECOVERY_LIMIT`) | both ok |
| File lock | `flock` | `msvcrt.locking` + in-process `threading.Lock` (`browser_bridge.py`) | handled |
| State file replace | `os.replace` | retried on `PermissionError` (`_replace_atomic`) | handled |
| Popup shim | needed (pywebview refuses `window.open`) | still applied; harmless | equal |

**Net:** no behavioral divergence in the race path. The two differences that matter are (a) timer clamping semantics, both covered by `pauseFor` once F7 is applied, and (b) Chrome's stronger recovery story.

---

## 6. Findings index, ranked

| # | Finding | Domain | Severity | Fix cost |
|---|---|---|---|---|
| F2 | Press waits for the page's 3–4 s SWR redraw | 1 | **critical** (the 이선좌 cause) | probe + medium (§1.6) / trivial (§1.7) |
| F1 | Fetch hook pre-empts the worker's press path; detections take the timer-clamped loop | 1 | high | trivial |
| T2 | Occluded 예매 창 stops the page's poll; watch runs blind | 3 | high | small (overlay warning + `online` nudge cannot help) |
| F4 | Two uncached CORS preflights before `line-up` | 2 | medium (지금 진입) / low (scheduled) | small |
| F5 | member-info minted at press time on 지금 진입 | 2 | medium | small |
| F6 | No preconnect to `ent-waiting-api` | 2 | low–medium | trivial |
| F7 | Fire approach uses clamp-prone `setTimeout` | 2 | medium when 예매 창 not frontmost | trivial |
| T3 | `captchaPresent` full-document `innerText` walk every tick | 4 | medium on large venues | small |
| M1 | P41149 좌석 상태가 변경 unclassified | 3 | medium | trivial regex |
| M2 | Session-expired modal auto-dismissed → navigation | 3 | medium | trivial exclusion |
| M3 | Slider-puzzle captcha copy not matched | 3 | medium | trivial |
| M4 | 7-minute hold expiry modal unhandled | 3 | low | small |
| L1 | MutationObserver and overlay interval leak per script reload | 4 | low | small |
| P1 | `withLivePlaySeq` `querySelectorAll` per request; `getInitData` re-parse per request | 4 | low | small |
| P2 | Our own polls fill the 24-entry trace | 4 | low (diagnostics) | fixed by F1 |
| T1 | `locked` sticks after a manual release with the watch stopped | 3 | low | small |

---

## 7. Roadmap

Ordered by expected effect on 이선좌 per unit of risk. Each step is independent.

**Step 1 — Stop diffing our own responses twice (F1).** `fetchJson` uses `window.__nolsniperNativeFetch`. Restores the worker's in-callback press, removes the loop-timer from the detect→press path, and cleans the trace. Verify: `lastFreedVia === "focus"` after a catch; `pageStatusSeen` stays 0 on a pure focus watch.

**Step 2 — Nudge the page's own refresh on every 0→1 flip (§1.7).** One line in `applyBlockMask`. Verify with `domAgreedMs`: the median should fall from ~1750 ms toward one RTT + one frame. Bound: 2 s dedupe.

**Step 3 — Probe the handler-direct press (§1.6) using the existing `probeSoftHold` harness.** Only if Step 2's floor (≈50–80 ms) is still losing races. If the probe passes, `pressSequence` tries `pressViaHandler` first when `seatNodeDisabled(node)` is true, and falls back to the pointer press when the circle is already enabled.

**Step 4 — Queue entry: warm and pre-mint (F4, F5, F6).** On landing on `tickets.interpark.com/goods/…`: preconnect `ENT_WAITING_ORIGIN`, mint member-info, and at T−3 s (scheduled) or immediately (지금 진입 park) send the two warm-up POSTs. Expected: 지금 진입 to `line-up` drops from ~230 ms + 2 preflights to ~60 ms.

**Step 5 — Clamp-safe waits everywhere on the fire path (F7).** Replace the two `sleep` calls with `pauseFor`. Consider `pauseFor` for the main catch loop's 15 ms period too.

**Step 6 — Modal classification (M1–M4) and visibility warning (T2).** Regex additions; an exclusion list for auto-dismiss; overlay line when `document.visibilityState === "hidden"`.

**Step 7 — Per-tick CPU (T3, P1) and reload hygiene (L1).** Pre-check before the captcha text walk; move `seatIndex.observer` and the overlay interval handle onto `window.__nolsniper*` so a reload disconnects them.

**Step 8 — Measure what is still unmeasured.** (a) exact signature TTL; (b) `onSeatClick`-direct behavior; (c) `online`-nudge latency on WKWebView; (d) whether the gateway counts empty warm-up POSTs; (e) `focusTickWorkMs` on a 15k-seat venue.

### What this roadmap does not deliver

- **A guaranteed win.** After Steps 1–3 the browser-driven path is bounded by one `seatStatus` RTT to notice the flip, the page's own `PreselectSeat` (one RTT plus React's handler), and the origin's first-arrival rule. A headless client colocated in the same region as the CloudFront edge (this machine sees ~10–18 ms TCP connect to all three hosts; a Seoul-region VM sees low single digits) and committing over `preselect` → `seats/select` directly is faster on every stage by physics. That is a different product, not a tuning of this one.
- **Elimination of the two-block cap or the 60 req/s budget.** Both are gateway rules (the second is self-imposed to stay under an unmeasured abuse threshold whose lockout is ~165 s, which is unrecoverable during an open).
- **A push feed.** None exists in this build.

---

## 8. Provenance

- Onestop bundle chunks (29 files, 3.0 MB) and locale files fetched 2026-09-05 from `tickets.interpark.com/onestop/seat` and `/onestop/locales/ko/{error,seat,common}.json`; scratchpad copies were used for extraction and are not part of the repo.
- Waiting-room bundle (`/waiting/_next/static/chunks/pages/index-2d4cb1c81dff671f.js`, 634 KB, obfuscated) fetched the same day.
- CORS and handshake numbers: `curl` from this machine, one `OPTIONS` per queue URL, no credentials, no body.
- Live NOL Sniper state: `mac/.nolsniper_browser_state.json` at the time of reading (build trigger-v68 run; `pageStatusSeen 12117`, `fastClicks 0`, `lastFreedVia ""`, `focusTicks 7160`).
- Repo documents relied on: `docs/INTERPARK_NOL_ENDPOINT_SPEC.md` §2.1, §2.4, §3.1, §4; `mac/README.md`; `core/entry.py` header; memory notes `api-soft-hold-dead-end`, `focus-poll-cap-stays-60`.

---

# Addendum A — Red-team pass (2026-09-05, later the same day)

Worst-case re-audit: heavy load, thousands of concurrent bots, multi-hour watches. Same tags as above. Two statements in the main report are **corrected** here (A2, A6).

## A1. The seat page has a server-issued dwell timer; a watch cannot outlive it **[bundle]** — CRITICAL, previously unknown to this codebase

`_app` (@639125) mounts the session provider with `expireAt: pageProps.initData.expireAt` and `serverNow: pageProps.initData.serverNow`. The provider computes `expiredAtMs = new Date(expireAt) + (Date.now() - serverNow)`, persists it in `sessionStorage["interpark/bsRef"]` (skew in `interpark/bsSkew`), counts down, toasts at 180 s and 60 s, and sets `isExpiredSession` at 0. The seat page (`tE.V`, `respectReserveExpireTime: true`) then shows a **persistent** modal (`좌석을 선택할 수 있는 시간 10분이 종료되었어요`) unless a live reserve exists, and its 확인 runs `onEnd` → leave the page. On mount the page also asks the server: `POST /onestop/gql {"query":"query ExpiredSession { isExpiredSession }"}` — so the expiry is enforced server-side too, and a preselect after it fails (IE0006 / P41148, both routed to the session-expired handler → `goToProduct`).

- **Duration:** server-provided; the copy says 10 minutes. NOL Sniper already parses `__NEXT_DATA__.props.pageProps.initData` (`getInitData()`), so `expireAt`/`serverNow` are one property read away. **Verify the live value before relying on "10".**
- **No refresh exists for it.** The only periodic re-auth in the bundle is `commAuthIframeLib.reissueToken()` every 600 000 ms via a hidden iframe to `accounts.interpark.com/reissuetoken`, and it refreshes the *account* token, only when the `tempinterparkGUEST` cookie is present and the UA is not the Interpark app. It does not extend the onestop bootstrap session.
- **What NOL Sniper does today:** nothing. No reference to `expireAt`, `bsRef`, or the expiry copy exists in the autopilot or the docs. When the modal appears, `dismissAnyBlockingOverlay` (it is a `dialog`-class node with a 확인 button, not captcha copy, not on the `NEVER_CLICK` list) **presses 확인 and navigates the 예매 창 to the product page.** `seatState.userCatch` is in-memory, so after that navigation the catch does not resume; `maybeReenter` fires only when `seat.reentry && arm.enabled` and only re-enters, then `bootRoute` runs 좌석 잡기 (not the watch) if `auto_seats_after_entry`. **Net: every 취켓팅 ends silently at the dwell limit with the window parked on the product page.**
- **Architectural limit:** re-entering through `secure-url → line-up → rank` before expiry yields a new session, but on a hot show the queue wait can exceed the dwell window, so continuous coverage from one account is not guaranteed. `line-up` answers `exist: true` for a key already in line, and `ME7922` exists for "same session already booked"; running two sessions from one account is unverified and likely refused.
- **Defensive pattern:** publish `initData.expireAt` and the remaining seconds in `status()`; at T−90 s (before the page's own 60 s toast) stop the workers, release nothing (nothing is held), persist `userCatch` + the watch config in `localStorage`, run the entry chain, and on landing resume the watch from the persisted flag. Exclude the expiry copy (`종료되었어요`, `세션이 만료`, `예매를 진행할 수 없습니다`) from `dismissAnyBlockingOverlay` so the page is never navigated by our own hand.

## A2. Correction to §3.1 / §6 M4

The "7-minute hold expiry modal" row conflated two timers. `su = Jd = 420000` ms is the **hold** timer set only after a preselect/select. The 10-minute modal is the **bootstrap dwell** above and fires with nothing held. Both need handling; A1 is the one that kills a plain watch.

## A3. 선택 완료 can be swallowed by the page's own in-flight guard **[bundle + code; partially observed in repo history]**

`validateAndSubmit` (`eh`) begins `if (!q() || ec()) return;` where `ec = () => (E.current.size > 0 || b.current) && (toast("이전 요청을 처리 중이에요"), true)`. `E.current.delete(seatInfoId)` runs in the `finally` of `ea`, several microtask hops after axios resolves. NOL Sniper's XHR hook fires `resolveSeatNetWaiters` from `loadend` via one `queueMicrotask`, and `pressSequence` calls `clickConfirmSelect()` on the first iteration of its confirm loop, before any `yieldFast()`. The 선택 완료 button is already enabled at that moment (the cart was written optimistically before the network). So the click can land while `E.current` still holds the id → toast, **no `/seats/select` request is sent**, `waitForSeatNet("select")` times out after 3 s, outcome `unconfirmed`, `locked = true`, `confirmStarted = true`, and **nothing retries.** The seat is held for 7 minutes and the user must press 선택 완료 by hand. The general path already knows this failure (`waitForSoftHoldIdle`, "NOL's own bundle refuses it while its in-flight flag is still set"); the fast path does not.

**Defensive pattern (confirm-with-verification):** after `pre.ok`, wait one macrotask (`await pauseFor(0)` or two `yieldFast()` hops) before the first click; then, if no `page:select` request is observed within ~150 ms of a click, click again, bounded to 3 attempts and spaced ≥100 ms. Never treat `select` timeout as terminal while the cart still shows the seat.

## A4. Hung requests have no timeout on the two paths that matter most **[code]**

- `fetchJson` (seatStatus/seatMeta/grades/block-data) uses bare `fetch` with no `AbortController`. Under load a request the server accepts but never answers stalls that focus worker until the browser's own socket timeout (minutes). Three stalled workers = zero polling; `focusPoller.inFlight` sits at 3 and nothing restarts them. Only `fetchQueueJson` (line-up/rank) has abort timers (3 s / 5 s).
- `fetchMemberInfo` and `fetchSecureUrl` are also bare `fetch`. One stalled `secure-url` POST at the open freezes the entire burst loop (`await enterViaSecureUrl`) with no further shots. This is the most plausible way to lose an open under a 10 000-user spike.

**Pattern:** `AbortController` with 1500 ms on `seatStatus` (the page's own axios uses 5000 ms; ours should be tighter), 2500 ms on `member-info`/`secure-url`; on abort count it as `others` for the burst and as `statusFailures` for the watch; add a worker watchdog: if `inFlight === FOCUS_WORKERS` and no response for 2 × timeout, bump `epoch` and respawn.

## A5. Five 5xx answers in 100 ms end the entry permanently **[code]**

`enterViaSecureUrlWithRetries`: any answer that is neither `UnableReservationTime` nor an auth error increments `others`; at `SECURE_URL_OTHER_ERROR_LIMIT = 5` it throws and the arm is over. Burst spacing is 20 ms. A 502/503/504 storm at 정각 — the *normal* state of a popular open — therefore aborts the fire within ~100 ms, and `maybeReenter` cannot save it (it requires the target time to have passed and spaces attempts 3 s apart). **Pattern:** classify 5xx and network errors separately from 4xx logic errors; for 5xx keep bursting through the window with capped exponential spacing (20 → 40 → 80 → 160 ms), and reserve the 5-strike rule for 4xx bodies that are not `UnableReservationTime`.

## A6. Correction to §1.7: the `focus` lever is dead; the `online` lever stands **[bundle]**

`_app` wraps the tree in `SWRConfig value={{ revalidateOnFocus: false }}` and the react-query client in `{ refetchOnReconnect: false, refetchOnWindowFocus: false, retry: 0 }`. So a synthetic `focus`/`visibilitychange` does nothing. `revalidateOnReconnect` is left at its default (true) and the `/seat-status` hook does not override it, so a synthetic `window` `online` event still revalidates it (deduped 2 s). Keep §1.7 with `online` only.

## A7. A bare 403 stops the watch for 165 s **[code]**

`readGatewayBlock` maps any 403/429 without `Retry-After` to `BLOCK_FALLBACK_MS` = 165 000 ms. Cloudflare sits in front of `tickets.interpark.com`; a managed challenge or a `__cf_bm` rotation answers an XHR with 403 HTML and `cf-mitigated: challenge`. One such answer parks 취켓팅 for 165 s while the map is fine. **Pattern:** treat 403 as a block only when the body or `errorCode` says ABUSE/BLOCKED or `Retry-After` is present; otherwise count it as a `statusFailure` and back off 1–2 s.

## A8. `rank` window expiry throws the queue position away **[code]**

`RANK_POLL_WINDOW_MS` = 15 min. On a large drop (the 김동률 3000th-in-line case) the turn can take longer. At expiry `enterQueueDirect` returns `navigated: false` and `enterViaSecureUrl` does `location.href = waitingUrl`; the waiting page lines the key up **again** (not idempotent) — a new, later place. Also 150 ms rank polling for 15 min is 6 000 requests to the queue host against a page that polls every 2–3 s; the host's tolerance is unmeasured. **Pattern:** never navigate to the waiting page after a successful line-up; keep polling rank (slowing to 500 ms after the first minute) until `oneStopUrl` or a terminal error; surface the position on the panel instead of giving up.

## A9. Under a 5xx storm the focus poller does not back off **[code]**

`fetchSeatStatus` catches, increments `statusFailures`, returns `null`; the worker loops at the 60 req/s cap. A gateway that is already failing receives the full rate from us, which is exactly the shape that earns `GATEWAY_ABUSE_BLOCKED`. `statusFailures` is published but never acted on. **Pattern:** after 3 consecutive failures halve the send budget; after 10, pause 2 s; reset on the first success.

## A10. Fiber-direct press: stale-closure hazard and the guard that makes it safe **[bundle]**

`seatSelectHandler` (`em`) is a plain arrow recreated on every render of the seat page hook; it closes over `G` (`selectedSeat`). A host node's `__reactFiber$` may reference the alternate (previous) fiber whose `return` chain carries one-render-old `memoizedProps`, so a handler resolved from the fiber can carry a stale `G`. `saveSelectedSeatInfo([...G, a])` would then rewrite the cart (React state **and** `interpark/context.seats`) from a stale list. Refs (`E.current`, `b.current`) are unaffected. Blocks re-mount when the viewport changes (`currentBlocks` keyed by `blockKey_index`), zoom does not remount. **Guards:** resolve the handler at press time from the block's own host `<g id="seat_block_…">`, never cache it; prefer the fiber with the newest `alternate` when both exist; call it only when `selectedSeatCount() === 0` and `quantity === 1`, which makes a stale `G` harmless; wrap in `try/catch` with optional chaining and fall back to the pointer press on any throw. Never call it during our own pan (the map hook's `seatClickableRef` is false for 100 ms after a pan; the root handler skips that check, so a press during a pan would go through while the page's own guard would have refused it).

## A11. Platform: two switches the hosts never set **[code]**

- **Windows Chrome** (`mac/cdp.py:launch`) launches without `--disable-background-timer-throttling`, `--disable-renderer-backgrounding`, `--disable-backgrounding-occluded-windows`, or `--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion`. Without the last one an occluded 예매 창 is treated as hidden: the page's SWR poll stops (T2) and, after 5 minutes, chained timers wake once per minute. These flags are the direct fix for T2 on Windows and cost nothing.
- **macOS**: nothing disables App Nap. `app_platform/darwin.py` never calls `NSProcessInfo.processInfo.beginActivityWithOptions_reason_(NSActivityUserInitiatedAllowingIdleSystemSleep | NSActivityLatencyCritical, …)`. A non-frontmost pywebview process is a candidate for App Nap (timer coalescing, reduced I/O priority) on top of WebKit's own hidden-page throttling. One pyobjc call at startup removes the process-level half; the WebKit half remains and is why the 예매 창 must stay unoccluded.
- CDP `Runtime.evaluate` and WKWebView `evaluateJavaScript` both run the 400 ms snapshot on the page's main thread; `snapshot_ms` is the measured cost (<2 ms target). Not a divergence.

## A12. Remaining unknowns that cannot be closed from the outside

1. The live value of `initData.expireAt` (and whether it differs per show or channel).
2. The gateway's request-rate threshold and whether empty warm-up POSTs count toward it.
3. Whether `line-up`'s `exist: true` ever yields a usable second position for the same account.
4. The exact `member-info` signature TTL (≥601 s).
5. Whether the `online` nudge revalidates on WKWebView identically to Chrome.
6. P41147 UNUSUAL_APPROACH: its trigger is unknown; it ends the session via `/error?errorCode=P41147`. A sustained 60 req/s from one session for hours is the obvious candidate. Only a long live run answers it.

## A13. Revised priority

1. **A1** dwell-timer awareness + planned re-entry + persisted `userCatch` (a watch that dies at 10 minutes makes every other optimization moot).
2. **A3** confirm-with-verification (a won race that never commits).
3. **A4/A5** timeouts and 5xx-tolerant burst on the entry path (a lost open).
4. §1.5 F1, §1.7 `online` nudge, A7, A9 (win rate and resilience of the watch).
5. A11 platform switches; A10 only after its probe.

---

# Addendum B — Code review of build `trigger-v70` (2026-09-05, read-only)

Reviewed: `browser/nolsniper_autopilot.js` (+954/−72 vs HEAD), `core/seat.py`, `mac/nolsniper.py`, tests. Test runs on this machine: `pytest tests/` 420 passed, 1 failed (`test_app_update.py::VersionTagTests`, expects `(dev)` and got `(v0.3.4)` — a stamped checkout, unrelated to v70); `node tests/test_autopilot_picker.mjs` 184/184; `node tests/journey_hold_lifecycle.mjs` 56/56; `bench_catch_latency.mjs` runs clean.

## B1. Verified correct

| Item | Verdict | Evidence |
|---|---|---|
| `pageHandlerFor` walk | ✔ | Shape-keyed (`seatSelectHandler` function, or `onSeatClick` beside `seatMeta`+`blockKey`), depth 40, resolved at press time, never cached. Matches the bundle's `F.J`/`q` root props and `SeatSvgBlockBase`. |
| `pressViaHandler` arguments | ✔ | Root: `fn(true, seat, blockKey, goods.isInterlocking, undefined, undefined)` = `em(select, seat, blockKey, skipNetwork, _, group)`. Block: `fn(seat, false, blockKey)` = `S(seat, isSelected, blockKey)`. Seat object is the page's own `seatMeta` object (from the circle's fiber or `lastBlocks`), not a projection. |
| Fallback to pointer press | ✔ | `via === "auto"`: handler only when `seatNodeDisabled(node)`; enabled circle → `firePointerSelect`. Handler unreachable → `node-disabled`/`handler-unreachable` traced, `false` returned. Journey test covers both. |
| Not-drawn seat | ✔ (new capability) | A seat with no circle is pressed through the root handler with its `blockKey`; the page's cart and preselect do not need a mounted circle. |
| F1 hook bypass | ✔ | `fetchJson` uses `__nolsniperNativeFetch` for `/onestop/api/seatStatus` only, and only while `window.fetch === __nolsniperWrappedFetch`. Worker now diffs first; `pageStatusSeen` and the trace no longer count our polls. |
| `online` pulse | ✔ mechanically | Fired from `applyBlockMask` on any freed seat, gap 2 s against the last pulse (not time zero), skipped while `document.hidden`. |
| F7 clamp-safe waits | ✔ | `waitUntilServerUnix` approach and the secure-url burst spacing both use `pauseFor`. |
| Queue warm-up (F4/F5/F6) | ✔ mechanically | `preconnect` + `dns-prefetch` with `crossOrigin="use-credentials"`; two discard-result POSTs at T−3 s and on goods-page landing, 3 s abort, ≥4 s between warms; member-info minted on landing and re-minted every 240 s (`maxAgeMs` 235 s, so the refresh does mint). |
| P41149 | ✔ | In `SEAT_TAKEN_DIALOG`, own class `statusChanged`, counted as 이선좌, fast-dismissed by the new body-level dialog observer (5.7 ms in the journey). Observer skips mutations inside the seat-map root, so circle mounts do not trigger it. |
| Session-expired modal | ✔ | `sessionExpired` is in `NEVER_DISMISS_DIALOG`; surfaced as `lastDialog` + `lastError`; never pressed. Journey test asserts it. |
| Slider captcha | ✔ | `isCaptchaPageCopy` now matches the puzzle copy (journey test). |
| `press_via` config | ✔ | `core/seat.py` validates `auto|handler|pointer`; panel reads `NOLSNIPER_PRESS_VIA` for a live probe. |

## B2. Remaining defects and edge cases

**B2.1 — The "won race that never commits" (A3) is not fixed.** `pressSequence` still calls `clickConfirmSelect()` on the first iteration of the confirm loop, one microtask after the preselect `loadend`. The page's `E.current.delete(id)` runs in a `finally` several hops later; `validateAndSubmit` begins with `if (!q() || ec()) return;` and `ec()` toasts and returns true while the id is still in the set. Outcome unchanged from A3: no `/seats/select` request, 3 s timeout, `unconfirmed`, `locked = true`, no retry. The bench's 185 ms "quiet gap" belongs to the general path (`waitForSoftHoldIdle`), not to this one. **Blocker for shipping the handler press**, because the handler press makes this path the common case. Fix: `await pauseFor(0)` (one macrotask) before the first click, then if no `page:select` XHR is seen within 150 ms, click again, ≤3 tries.

**B2.2 — Dwell timer (A1) is classified but not handled.** The 10-minute copy matches `HOLD_EXPIRED_DIALOG` (the `\s*` swallows the line break) and routes to `onHoldExpired`, which pauses without pressing — so the accidental navigation is prevented, good. But the message reads 좌석 선점 시간(7분) for a modal that has nothing to do with a hold, `locked=false`/`heldSeatIds.clear()` run on a session that held nothing, and there is still no reading of `initData.expireAt`, no countdown, no pre-expiry re-entry, and `userCatch` is still in-memory. A watch on a hot show still ends at the dwell limit; it now ends paused on the seat map instead of navigated away. Rename the class to `dwellExpired` when no hold is live, and implement A1.

**B2.3 — `online` pulse effectiveness is overstated.** The page's `/seat-status` SWR hook keeps `dedupingInterval` at its 2 s default, and the page's own poll fires every 3–4 s. A pulse landing within 2 s of the page's last fetch is swallowed. Expected render wait falls from ~1.75 s to ~1.4 s, not to one RTT. It also revalidates every other SWR hook (`seat-meta`, `blocks`, context) — extra page-side requests against the same gateway budget on every flip. Acceptable now that the handler press bypasses the gate, but do not rely on it, and consider a second pulse at +2 s if the seat is still unpressed.

**B2.4 — Warm-up body safety is unproven and one of the two is unnecessary.** `POST secure-url {}` is a malformed command; the gate's classification of it is unknown (`AccessDenied_Blacklist` is account-level and unrecoverable). Safer: send a *well-formed* secure-url for the armed show at T−3 s — before the open it answers `UnableReservationTime`, identical to the burst's own −400 ms shots. `POST line-up {key:""}` is needed only for the preflight cache; its 5 s window expires at T+2 s, so a late open (the server flips "some moment after 정각") pays the preflight anyway. Re-warm line-up every 4 s while the burst is still running. Also: the landing warm fires on every goods-page `bootRoute` while parked, hours before an open — harmless but pointless traffic to the one host whose block is unrecoverable; gate it to arms whose open is within ~10 minutes or to the 지금 진입 park.

**B2.5 — Stale-closure guard (A10) missing.** `pressViaHandler` does not require `selectedSeatCount() === 0`. `clickSeatOnMap` is also reached from `selectSeats` (quantity ≥ 2), where a one-render-stale `seatSelectHandler` closure rewrites the cart from a stale list (React state and `interpark/context.seats`). Guard the root-handler path with `quantity === 1 && selectedSeatCount() <= 0`, else use the block handler or the pointer.

**B2.6 — Group seats through the handler.** The root call passes `undefined` for `groupSeats`; the map hook would pass the `seatGroupMap` entry for a `seatGroupId` seat and route to bulk preselect. A grouped seat pressed singly answers P41154 at select. Either pass the group (readable from `lastBlocks` by `seatGroupId`) or skip the handler for `seatGroupId` seats.

**B2.7 — Not addressed in v70 (still open from Addendum A):** A4 no `AbortController` on `seatStatus`/`member-info`/`secure-url` (one stalled request freezes a worker or the whole burst); A5 five 5xx answers in 100 ms still end the entry; A7 bare 403 still parks the watch 165 s; A8 rank window still navigates to the waiting page and re-lines-up; A9 no backoff on 5xx storms; A11 Chrome launch flags unchanged (`mac/cdp.py` untouched), no App Nap opt-out.

**B2.8 — Minor.** `holdGuardTick` checks `holdExpiresAt` before `onPriceStep()`, so 7 min after a catch it pauses and clears `heldSeatIds` while the user may be on `/payment` (the page expires at the same moment, so mostly cosmetic). `onHoldExpired`'s `heldSeatIds.clear()` skips `releasePreselected`, which is right (expired server-side) but the comment does not say so.

## B3. All-round sweep (김동률, rounds 001–007) — cleanest shape

Facts that bound the design **[bundle/spec]**: one onestop session is bound to one `playSeq`; the seat page's dwell timer forbids camping; `seatMeta`/`seatStatus` answer without a session for most shows; block keys embed the round (`${playSeq}:${block}`); `secure-url` takes `playSeq`; the panel already has a per-round remains fetch (`fetch_round_remains(goods, date, play_seq=…)`, ~132 ms, no CORS issue from Python) and `enter_now` already carries `play_seq`.

1. **Detect from Python, not the page.** A `RoundSweeper` thread on the panel: for each of the 7 rounds keep a `TriggerState` (`core/watch_trigger.next_trigger_state`) fed by `fetch_round_remains(…, play_seq=R)` in a round-robin, one request every ~300 ms (≈7 rounds every 2 s, well under the ticketfront budget). The remains count is the whole-venue "did anything free" signal per round.
2. **On a rise in round R:** confirm with one `seatStatus` sweep for that round's watched blocks (Python, no session needed; 2 blocks per request) and check the drawn 감시 구역 — venue coordinates are identical across rounds, only the `playSeq` prefix changes.
3. **Enter for R:** `enter_now` with `play_seq = R` (the payload already carries it; `rememberPendingRound` handles the schedule step). The 예매 창 stays parked on `tickets.interpark.com/goods/{code}` between catches, which has no dwell timer and is the origin the entry needs.
4. **On landing:** run the watch in `live` mode (free seats already present, not only 0→1 flips) with `press_via=auto`; the handler press does not need the seat drawn.
5. **Reality check:** if the show has a queue, minutes pass between the signal and the seat map, and the seat is gone; the sweep is worth it only for rounds that answer `oneStopUrl` immediately (`rank` with no line) or for cancellations that sit for minutes. Log `signal→landing` per attempt and let the numbers decide whether to keep it.

## B4. Sign-off

Not ready to ship as the default `press_via=auto`. Ship-blockers: B2.1 (a caught seat that never commits) and B2.5 (stale cart rewrite on multi-seat). Ship-ready today: F1 bypass, F7 clamp-safe waits, P41149 and session-modal handling, the dialog observer, the queue preconnect and pre-mint. Recommended order: fix B2.1 and B2.5, change the secure-url warm body (B2.4), then run the `NOLSNIPER_PRESS_VIA=handler` live probe on a quiet map with the soft-hold harness before enabling `auto`. A1 (dwell re-entry) and A4/A5 (timeouts, 5xx-tolerant burst) remain the largest open risks for a real drop.
