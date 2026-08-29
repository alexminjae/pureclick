# Interpark / NOL ticketing flow (full sweep)

Live UI, onestop JS bundles (`research/onestop_bundles`), NOL/gates research, and PureClick autopilot.

## What `/onestop/seat` is

`https://tickets.interpark.com/onestop/seat` is **not** a public product URL. It is a **session-bound** step of the onestop SPA (NOL-branded). Opening it cold does nothing useful.

Required session:

- Cookies (logged-in NOL / Interpark)
- `sessionStorage["interpark/context"]` with `sessionId`, `goods`, `playSeq`, `bizCode`, …
- Often also `__NEXT_DATA__.props.pageProps.initData`

Without that context the app clears and bounces to the product page.

## End-to-end path (reserved onestop show)

```
nol.yanolja.com/ticket/products/{goodsCode}
  → login + identity
  → 예매하기 → date/round modal
  → SSO  sso.yanolja.com/sso/v1/bridge/token
        ?source=YANOLJA&serviceDomainCode=NOL_TICKET
        &redirectUrl=…/gates/partner?bc=61776&gc&pc&ps&cc=Gates
  → gates/partner builds booking session
  → waiting (ticketfront /waiting) OR BookSession if "N"
  → /onestop/schedule (sometimes skipped)
  → /onestop/seat          ← block map (your screenshot 1)
       → click zone (스탠딩 A / A1 / …)
       → entry-number / seat circle grid (screenshot 2)
       → GraphQL preselect (soft hold)
       → 선택 완료
       → POST /onestop/api/seats/select
  → /onestop/seat?step=price
  → /onestop/payment
  → /onestop/complete
```

Also present: `/waiting` book-wait twin flow (`예매대기` button on the map).

## Real seat UI: sections first, seats only after zoom

This is the critical behavior. The map is **not** “all seats visible at once”.

### What you see first (zoomed out)

On `/onestop/seat` load:

1. `GET /onestop/api/seats/block-data` → section polygons only (`blockKey`, `absoluteLeft/Right/Top/Bottom`, labels like 스탠딩 A, A1)
2. Full venue background + colored **section shapes** (your first screenshot)
3. **No seat circles** — only blocks/regions

### What zoom actually does (lazy load)

From `research/onestop_bundles/13_6705-*.js`:

1. Pan/zoom updates the visible viewport (debounced ~100–300ms)
2. An R-tree finds which `blockKey`s intersect the viewport → `currentBlocks[]`
3. **New** blocks in view trigger fetch for **only those blocks**:
   - `GET /onestop/api/seatMeta?blockKeys=…`
   - `GET /onestop/api/seatStatus?blockKeys=…`
4. Seat circles (`circle.js-seat`) render only after meta loads for visible blocks

So: **zoom in → section enters viewport → API fetch → dots appear**. That matches what you see.

### Zoom threshold before seats are clickable

Target dot sizes: `z = [0, 4, 12, 24, 48]` px.

- `seatVisibleLevelScale` = zoom scale where seat diameter reaches ~**12px**
- Below threshold: click = **zoom toward click** (`zoomToClickPosition`), not seat select
- At/above threshold: circles clickable → GraphQL `preselectSeat` (“포도알 선택”)

### Pick → confirm → price

1. Circle click → `preSelectSeat` (soft hold)
2. Sidebar: e.g. `A구역 입장번호 364번`
3. **선택 완료** → `POST /seats/select`
4. `/onestop/seat?step=price` → payment

### PureClick vs official UI

| Official | PureClick today |
|---|---|
| Zoom → lazy `currentBlocks` → per-block meta | Fetches all blocks via API (UI bypass) |
| Waits for zoom ≥ `seatVisibleLevelScale` | Looks for DOM circles; empty if not zoomed |
| Block click when zoomed out = zoom in | Does not programmatically zoom to section |

**Flow test:** zoom into target section until dots appear, then **프로브/START**.

**Full automation:** needs programmatic zoom-to-block + wait for meta + wait for `seatsVisible`.

## Waiting API (correct)

```
GET api-ticketfront.interpark.com/v1/goods/{goodsCode}/waiting
  ?channelCode=pc|cp|lo&preSales=N|Y&playDate=yyyyMMdd&playSeq=
```

| Result | Action |
|--------|--------|
| `https://…` | Navigate to queue |
| `"N"` | No queue → POST `poticket…/Book/BookSession.asp` (`GroupCode`, `Tiki=N`, `Point=N`, `PlayDate`, `PlaySeq`) |
| `"NP"` | Presale auth failed |
| `"BL"` | Blocked |

Legacy mistake: POSTing BookSession fields to `/waiting`.

## Official onestop seat APIs

REST (`/onestop/api`):

- `GET /seats/init/{goodsCode}`
- `GET /seats/block-data`
- `GET /seatMeta?blockKeys[]&bizCode`
- `GET /seatStatus?blockKeys[]&bizCode` (+ `last-seat-modified` header)
- `POST /seats/select` / `select-external`
- `GET /seats/clear-select/…`
- `GET /seats/grades`
- `GET /seats/book-wait/…`
- `POST /captcha/image` · `GET /captcha/verify`

GraphQL (`POST /onestop/gql`):

- `preselectSeat` / `bulkPreselectSeats` — soft hold on circle click
- `deselectSeat` / `bulkDeselectSeats`
- `AutoAssignSeats` — non-map / auto path
- `GetRemainSeats` — grades, `isNonReservedSeat`, `isAutoAssignSeat`

Headers: `X-Onestop-Session`, `X-Onestop-Channel`.

## PureClick Mac architecture

```
mac/pureclick.py
  ↔ .pureclick_browser_state.json
mac/browser_host.py (pywebview) + captcha_ocr
  → injects browser/pureclick_autopilot.js
  → ticketfront + /onestop/api + /onestop/gql
```

Commands: `run_entry`, `run_seats`, `run_catch`, `probe_seats`, `sync_grades`.

## Codeham (Postype marketing) vs reality

Public post claims: timed entry, zone + instant preemption, grade sync / catch, captcha/puzzle auto, multi-OS, old popup + “whac-a-mole” + Yanolja. It does **not** document `/onestop/seat` UI or APIs. Treat it as a feature checklist, not a technical source.

## PureClick coverage after full sweep

| Step | Status |
|------|--------|
| Clock sync (poticket Date) | Yes |
| NOL 예매하기 / SSO gates URL | Yes |
| Waiting API schema | Yes |
| Waiting `"N"` → BookSession | Yes |
| Session from `__NEXT_DATA__` + `interpark/context` | Yes |
| `X-Onestop-*` headers | Yes |
| block-data → seatMeta → rank | Yes |
| GraphQL bulkPreselect before select | Yes |
| REST seats/select | Yes |
| Click 선택 완료 / navigate `step=price` | Yes (best-effort) |
| seatStatus with blockKeys | Yes |
| DOM circles + API fallback on block overview | Partial — API works; DOM needs manual zoom |
| Programmatic zoom-to-block + wait for seats | No |
| Official captcha image/verify APIs | No |
| Non-reserved / AutoAssignSeats | No |
| 예매대기 waiting wizard | No |
| Full payment / PG | No |

## Ticketing-flow-only test (skip ARM)

1. Log in, open any product, complete **예매하기** until `/onestop/seat` shows.
2. Use **프로브** (no lock) or **START** (lock). Do not use ARM/TEST.
