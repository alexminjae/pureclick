# Interpark / NOL Ticketing — Endpoint Specification

Read-only forensic reference, compiled 2026-09-05 from this repository's code
(`browser/nolsniper_autopilot.js`, `core/*.py`, `mac/*.py`), its research
captures (`research/api_shapes/`, `research/seatmaps/`, `research/probes/`),
the flow write-up in `docs/interpark_flow.md`, and the live-session artefacts
under `mac/` (cookie jar names, bridge state, seat sketches).

Every claim is tagged:

| Tag | Meaning |
|---|---|
| **[measured]** | Observed against the live site and recorded in code, a probe, or a capture in this repo |
| **[bundle]** | Read out of NOL / onestop JS bundles at the time of mining; endpoint exists but the app may never call it |
| **[inferred]** | Follows from measured behaviour but has not been directly observed |
| **[not observed]** | Asked for by the audit checklist, but nothing in this repo has ever seen it |

Goods codes used throughout as worked examples: `26011315` (Maroon 5, KINTEX,
`placeCode=26000914`, `bizCode=29283`), `26005128` (극장 용, `placeCode=17000549`),
`26012391` (화성예술의전당), `26006903` (75-block venue), `26007416` (겨울왕국).

---

## 0. Host map

| Host | Role | Auth realm | CORS to page origin |
|---|---|---|---|
| `nol.yanolja.com` | NOL product pages (Next.js App Router, RSC flight payload), product-side JSON (`/ticket/products/api/*`) | `.yanolja.com` cookies (`yanolja_sid`, `nol_session_id`) | same-origin only |
| `sso.yanolja.com` | SSO bridge that turns a NOL login into an Interpark session | Yanolja | n/a (navigation) |
| `tickets.interpark.com` | Onestop SPA (`/onestop/*`), gates (`/gates/partner`), queue landing (`/waiting?key=`), reserve-gate API (`/api/ticket/v2/reserve-gate/*`), goods page (`/goods/{code}`) | `.interpark.com` cookies (`id_token`, `partner_token`, `interparkSNO`) | `/onestop/api/*` and `/onestop/gql` are same-origin from the seat page; `reserve-gate/goods-info` answers cross-origin with no cookies **[measured]** |
| `ent-waiting-api.interpark.com` | Queue admission (`/waiting/api/secure-url`) | none — credential travels in the body **[measured]** | CORS open to `tickets.interpark.com` **[measured]**; from `nol.yanolja.com` / `poticket` → 403, no ACAO header **[measured]** |
| `api-ticketfront.interpark.com` | Public catalogue (`/v1/goods/*`, `/v1/Place/*`, `/v1/bizInfo/*`, `/v1/ranking/realtime`) and the **legacy** waiting endpoint | old ticketfront realm — an SSO login **never** populates it **[measured]** | ACAO only for `https://tickets.interpark.com` **[measured]**; page fetch from NOL → 403 |
| `poticket.interpark.com` | Legacy engine: `Book/BookSession.asp`, `Book/BookMain.asp` (clock sync target) | legacy | n/a (form POST navigation) |
| `aspseat-ticket.interpark.com` | Captcha audio (`CommonAPI/Captcha/GetCaptchaAudio`) **[bundle]** | ? | ? |
| `order-gw.yanolja.com`, `nol-payment.yanolja.com` | Payment gateway (cookies only observed; no request ever made by this codebase) | Yanolja | n/a |
| `ticketimage.interpark.com` | Static goods images | none | n/a |

Server clocks **[measured, `research/probes/probe_clock.py`, `mac/README.md`]**,
by bracketing the second-boundary of the `Date` header:

| Host | Offset vs local | Note |
|---|---|---|
| `poticket.interpark.com` | +18 ms | the 26 ms outlier |
| `api-ticketfront.interpark.com` | −8 ms | |
| `tickets.interpark.com` | +4 ms | the two hosts that matter agree within 12 ms |

`Date` is 1-second resolution; a single read understates the offset by
`frac(second)`. Estimators: max-of-N converges on the truth
(`validate_clock_estimator.py`), `ServerClock.sync_tick` brackets the rollover
over one keep-alive connection (`core/clock.py`).

---

## 1. Authentication, cookies, and session tokens

### 1.1 What the audit checklist asked for vs. what exists

| Name | Status |
|---|---|
| `encMemberHash`, `isLogin`, `MEM_NO`, `O_NO` | **[not observed]**. These are names from the pre-onestop `poticket` / `ticket.interpark.com` era. Nothing in this repo, its captures, or its 80-cookie live jar carries any of them. The current NOL→onestop flow does not use them. |
| `interpark/context` (sessionStorage) | **[measured]** — the real booking-session token carrier, see §1.4 |
| `X-Onestop-Session` / `X-Onestop-Channel` | **[measured]** — the per-request session headers on `/onestop/*`, see §1.5 |
| `signature` / `secureData` | **[measured]** — the queue-entry credential pair, see §2.1 |

### 1.2 Cookie inventory (live jar, values withheld)

Captured from `mac/.nolsniper_cookies.json` (WKHTTPCookieStore dump). Only
names, domains, flags, and expiry classes are listed. `session` = no `Expires`.

**Identity provider — Naver (`.naver.com`, `.nid.naver.com`)**

| Cookie | HttpOnly | Secure | Expiry | Role |
|---|---|---|---|---|
| `NID_AUT` | yes | yes | session | Naver auth token |
| `NID_SES` | no | yes | session | Naver session (584 bytes) |
| `NID_JST` | yes | yes | ~200 d | Naver "keep me logged in" |
| `nid_inf`, `nid_slevel`, `NAC`, `NNB`, `BUC`, `SRT5`, `SRT30`, `NACT` | mixed | mixed | 30 d – 1 y | Naver device / telemetry |

NOL's Naver button is a full OAuth redirect with `auth_type=reauthenticate`
**[measured, mac/README.md]** — a logged-out start means a manual Naver login.

**NOL / Yanolja (`.yanolja.com`, `nol.yanolja.com`)**

| Cookie | HttpOnly | Secure | Expiry | Role |
|---|---|---|---|---|
| `yanolja_sid` | yes | no | ~3 h from issue | Yanolja session id (82 bytes) |
| `nol_session_id` | yes | yes | ~2.5 h | NOL web session (UUID) |
| `cf_clearance` | yes | yes | 1 y | Cloudflare challenge pass — **must be persisted** or a new challenge appears **[measured]** |
| `__cf_bm` | yes | yes | 30 min | Cloudflare bot-management, rotates |
| `exp-groups` | yes | no | session | A/B bucket (453 bytes) |
| `nol_platform`, `nol_device_id`, `nol_app_version`, `app-device`, `platform`, `device-type`, `cgntId` | no | no | session – 1 y | device descriptors (`nol_platform` = 3-char code) |
| `_ga*`, `_gcl_au`, `_fbp`, `_fwb`, `__rtbh.*`, `wcs_bt` | no | mixed | 90 d – 1 y | analytics |

**Interpark (`.interpark.com`, `tickets.interpark.com`, `accounts.interpark.com`)**

| Cookie | HttpOnly | Secure | Expiry | Role |
|---|---|---|---|---|
| `id_token` | yes | yes | session | OIDC id token (1021 bytes) — the Interpark login |
| `partner_token` | yes | yes | ~3 h from issue | Partner (NOL→Interpark) access token (608 bytes) |
| `partner_token_r` | yes | yes | ~6 h from issue | Partner refresh token (334 bytes) |
| `interparkSNO` | no | yes | session | Interpark member serial (48 bytes) |
| `userId` | no | no | session | 10-char user id |
| `tempinterparkGUEST` | no | yes | session | guest marker (28 bytes) |
| `interparkstamp`, `firstStep`, `TodayGoodsList`, `pcid`, `tbid`, `_kmpid`, `nclab` | no | mixed | session – 1 y | tracking / recently-viewed |
| `__cf_bm` | yes | yes | 30 min | Cloudflare bot-management |
| `ab.storage.*` (deviceId, userId, sessionId) | no | no | 1 y | Braze SDK |
| `_csrfSecret` (`accounts.interpark.com`) | yes | yes | session | login-form CSRF |

**Payment gateway (`order-gw.yanolja.com`, `nol-payment.yanolja.com`)** — all
HttpOnly+Secure session cookies, set by a previous visit to the payment step:
`pay-correlation-id`, `pay-goods-type`, `pay-cancel-uri`, `result-send-way`,
`pay-lang`, each with a `legacy-` twin, plus `nol-payment-trace-id`. **This
codebase never sends a request to either host** (checkout is deliberately
manual, §2.7).

### 1.3 Which cookie each call actually needs **[measured]**

| Call | Needs | Evidence |
|---|---|---|
| `GET tickets.interpark.com/api/ticket/v2/reserve-gate/member-info` | `.interpark.com` session (`id_token` / `partner_token`) sent **first-party**. From `nol.yanolja.com` the request completes but answers 401 — SameSite, not CORS. | `core/entry.py`, `mac/nolsniper.py:_park_for_entry` |
| `POST ent-waiting-api…/waiting/api/secure-url` | `signature`+`secureData` in the body are the credential; works with no cookies **[measured]**. The gate's own bundle and the app now send `credentials: "include"` anyway, because `line-up` (§2.1.5) is made with them | `fetchSecureUrl()`, `fetchQueueJson()` |
| `POST …/waiting/api/line-up`, `GET …/waiting/api/rank` | `.interpark.com` jar sent (`credentials: "include"`); rank also answered without cookies in the probe | §2.1.5, `tools/probe_entry_chain.py` |
| `GET tickets.interpark.com/api/ticket/v2/reserve-gate/goods-info` | none — answers 200 cross-origin | `fetchSchedule()` comment |
| `/onestop/api/seatMeta`, `/onestop/api/seatStatus` | none on most shows (43/63 full, 16/63 ungraded skeleton, 4/63 nothing) | `demo_no_login.py`, `mac/README.md` |
| `/onestop/api/seats/block-data`, `/onestop/api/seats/grades` | booking session (HTTP 400 without) | `probe_seatmap.py`, `watch_trigger.py` docstring |
| `/onestop/gql` mutations, `/onestop/api/seats/select` | booking session + `X-Onestop-Session` | `gql()`, `selectSeats()` |
| `api-ticketfront…/v1/goods/{code}/waiting` | old ticketfront realm — an SSO login **never** satisfies it; 401 for every show | `core/entry.py`, `research/api_shapes/waiting_probe.json` |
| `api-ticketfront…/v1/goods/*` (summary, prices, remain) | none; requires `Referer`/`Origin: https://tickets.interpark.com` or 403 | `core/showinfo.py:API_HEADERS` |

### 1.4 The booking session (`interpark/context`) **[measured]**

Created by `/gates/partner` (or BookSession) and consumed by every `/onestop/*`
page. Lives in `sessionStorage` under the key `interpark/context`
(alternative keys seen: `onestop/context`, `interpark_context`; the autopilot
falls back to scanning both storages for any object with `sessionId` and
`goods`). Also mirrored into `window.__NEXT_DATA__.props.pageProps.initData`
on the pages-router onestop build, but **not always** — a live seat map with the
countdown running had neither, so the scan exists.

```jsonc
{
  "sessionId":     "<opaque>",          // → X-Onestop-Session
  "channelType":   "ONESTOP",           // → X-Onestop-Channel
  "bizCode":       "WEBBR",             // channel biz code used in seat queries (NOT the goods bizCode)
  "lang":          "ko",
  "entMemberCode": "<member code>",     // p4 of captcha verify [bundle]
  "goods": {
    "goodsCode":   "26011315",
    "placeCode":   "26000914",
    "preOpt":      "<bearer>",          // present only on presale; → Authorization: Bearer
    "isMultiPlay": true,                // multi-round show: playSeq absent, /onestop/schedule is forced
    "isInterlocking": false,            // true → /seats/select-external instead of /seats/select
    "isSportOneStop": false, "isSportsGroup": false, "kindOfGoods": "01003"
  },
  "playSeq": { "playSeq": "001" }       // absent on isMultiPlay sessions until the schedule step
}
```

Staleness trap **[measured]**: `initData` is captured at page load and never
refreshed. Changing 회차 in place leaves it on the old round (`stated 007`,
DOM `002` in the last trace). The seat circles' React props carry
`blockKey = "<playSeq>:<block>"`, so the live round is recovered from the DOM
(`currentPlaySeqFromDom`), not from storage.

### 1.5 Per-request headers on `/onestop/*` **[measured, `onestopHeaders()`]**

```
Accept: application/json
X-Onestop-Session: <sessionId>
X-Onestop-Channel: ONESTOP
X-Ticket-BFF-Language: KO           # ko→KO, en→EN, ja→JA, zh→ZH
X-OneStop-Trace-ID: <16 chars [A-Za-z0-9], random per request>
X-Requested-With: XMLHttpRequest
Authorization: Bearer <goods.preOpt>  # presale only
Content-Type: application/json        # POST bodies
```

Plus browser cookies (`credentials: "include"`). Note that the read endpoints
(`seatMeta`, `seatStatus`) answer without any of these; the headers matter for
the session-bound ones.

### 1.6 Expiry triggers, renewal, and drop detection

| Token | Lifetime | Renewal | How a drop shows up |
|---|---|---|---|
| `partner_token` | ~3 h **[jar]** | `partner_token_r` (~6 h) refreshes it — mechanism not observed; the SSO bridge (§2.0) re-mints on navigation | `member-info` → HTTP 401; `secure-url` never reached |
| `yanolja_sid` / `nol_session_id` | ~2.5–3 h **[jar]** | re-login through Naver OAuth | NOL header shows 로그인 instead of 로그아웃/마이페이지 — `loginState()` reads the top bar on every 400 ms poll and the panel refuses to arm |
| `interpark/context.sessionId` | for the booking session; wiped when onestop boots a new session | new `/gates/partner` hop | `/onestop/seat` opened cold bounces to the product page; `getInitData()` → null |
| GraphQL preselect hold | server-side, expires on its own (duration **not observed**); counts against the account's ticket allowance until released | `BulkDeselectSeats` | later `seats/select` answers 예매 가능 매수를 초과하였습니다 |
| Captcha challenge (`EncRnd`) | 5 min **[bundle]** | new `POST /captcha/image` | modal copy 「화면의 문자를 입력해주세요」 reappears |
| `signature` (member-info) | stamped with issue time: `<hex>.<unix>` **[measured]**; still accepted by `secure-url` at 121 s, 301 s and 601 s of age **[measured 2026-09-05, `tools/probe_entry_chain.py`]** — the limit is beyond 10 min, so it is pre-minted 10 s before the burst | re-GET `member-info` | `secure-url` rejects (code not recorded) |
| `__cf_bm` | 30 min | Cloudflare re-issues silently | none |
| Gateway abuse block | `retryAfterMs` (165 470 ms observed) | wait; **retrying extends it** | `GATEWAY_ABUSE_BLOCKED` on every call, §3.1 |

Session-drop strings recognised by the app: `HTTP 401`, `로그인`, `logout`,
`Unauthorized`, `자동 로그아웃` (`isAuthError()`), and the ticketfront body
`"오랜 시간 이용하지 않아 자동 로그아웃되었습니다. 다시 로그인 후 이용해주세요."`.

---

## 2. Ticketing flow — endpoints and payloads

Order of operations for a reserved-seat onestop show:

```
[NOL product page]  nol.yanolja.com/ticket/products/{goodsCode}
   │  reads: RSC flight payload (goodsCode, goodsName, placeCode, playStartDate, bookingOpenTime — no round list)
   │  GET /ticket/products/api/schedules            (rounds for one day)
   │  GET /ticket/products/api/remaining-seats      (per-grade remain)
   ▼
[park]  tickets.interpark.com/goods/{goodsCode}     ← the only origin that can mint the credential
   │  GET  /api/ticket/v2/reserve-gate/goods-info   (rounds + ticketOpenDate; public)
   │  GET  /api/ticket/v2/reserve-gate/member-info  (signature + secureData; needs .interpark.com cookie)
   │  POST ent-waiting-api…/waiting/api/secure-url  (→ redirectUrl)
   ▼
[queue] POST ent-waiting-api…/waiting/api/line-up {key}   ← assigns the place in line (userSeq / waitingId)
   │  GET  ent-waiting-api…/waiting/api/rank?waitingId=…   (myRank / totalRank; oneStopUrl when it is your turn, ~1.7 s+)
   │  (the page tickets.interpark.com/waiting?key=… does exactly these two, after its own boot; the app does them itself)
   ▼
[gate]  tickets.interpark.com/gates/partner?bc=61776&gc&pc&ps&cc=Gates   → writes interpark/context
   ▼
/onestop/schedule   (forced when goods.isMultiPlay; sending playSeq does NOT skip it [measured])
   ▼
/onestop/seat       block-data → seatMeta → seatStatus → (circle click → page's own PreselectSeat) → 선택 완료 → POST /seats/select
   ▼
/onestop/seat?step=price   quantity, birth, delivery, payment method, consents
   ▼
POST /onestop/api/payment/order/{goodsCode}   [bundle — never called here]
   ▼
/onestop/payment → /onestop/complete   [bundle]
```

### 2.0 SSO bridge and gate (navigation, not XHR) **[measured]**

The button NOL's own 예매하기 fires:

```
GET https://sso.yanolja.com/sso/v1/bridge/token
      ?source=YANOLJA
      &serviceDomainCode=NOL_TICKET
      &redirectUrl=https%3A%2F%2Ftickets.interpark.com%2Fgates%2Fpartner
                   %3Fbc%3D61776%26gc%3D{goodsCode}%26pc%3D{placeCode}%26ps%3D{playSeq}%26cc%3DGates
```

`bc=61776` is the **NOL partner** biz code, constant. It is not the goods'
`bizCode` (e.g. `29283`); sending the latter to `secure-url` is a 400
**[measured]**. `/gates/partner` builds the booking session and lands on
`/onestop/schedule` or `/onestop/seat`.

NOL's `openPCOnestop()` drives a named popup (`window.open('', 'BookingPop',
'width=900,height=682')`) and submits a form into it, or for a queue does
`window.self.close(); win.location.replace(waitingUrl)`. **[measured, mac/README.md]**

### 2.1 Queue entry — the working route **[measured 2026-09-04]**

#### 2.1.1 `GET /api/ticket/v2/reserve-gate/member-info`

```bash
curl 'https://tickets.interpark.com/api/ticket/v2/reserve-gate/member-info?goodsCode=26011315&channelCode=pm' \
  -H 'Accept: application/json' \
  -H 'Referer: https://tickets.interpark.com/goods/26011315' \
  --cookie 'id_token=…; partner_token=…; interparkSNO=…'
```

| Param | Value |
|---|---|
| `goodsCode` | 8-digit goods code |
| `channelCode` | `pm` (what the gate bundle sends) |

Response (200):

```jsonc
{
  "signature":  "<hex>.<unix seconds>",   // time-stamped; mint at fire time
  "secureData": "<opaque>",
  // …other member fields not recorded
}
```

Errors: 401 when the `.interpark.com` cookie is missing (always the case from a
`nol.yanolja.com` document).

#### 2.1.2 `POST https://ent-waiting-api.interpark.com/waiting/api/secure-url`

```bash
curl -X POST 'https://ent-waiting-api.interpark.com/waiting/api/secure-url' \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -H 'Origin: https://tickets.interpark.com' \
  --data '{
    "signature":  "<from member-info>",
    "secureData": "<from member-info>",
    "lang":       "ko",
    "passCode":   "",
    "from":       "NOL",
    "goodsCode":  "26011315",
    "bizCode":    "61776",
    "playSeq":    "001",
    "preSales":   "N"
  }'
```

No cookies. No `playDate` — `playSeq` alone identifies the round
**[measured, tests/test_entry.py]**. `passCode` is for presale codes (empty
otherwise). `preSales` is `Y` only for an active presale round.

Response (200):

```json
{ "redirectUrl": "https://tickets.interpark.com/waiting?key=…" }
```

The URL may also be a direct gate URL when no queue is running; the app
navigates to whatever it gets and learns the queue host for preconnect on later
runs (`nolsniper_queue_host_v1` in localStorage).

Error body (non-200):

```json
{ "error": "UnableReservationTime" }
{ "error": "AccessDenied_Blacklist" }
```

| `error` | Meaning | App behaviour |
|---|---|---|
| `UnableReservationTime` | 티켓 오픈 전 — the normal answer before open | keep retrying inside the window |
| `AccessDenied_Blacklist` | 비정상 예매 차단 (modern spelling of legacy `BL`) | stop, never retry |
| other / non-JSON | unknown | up to 5 consecutive, then stop |

Latency **[measured]**: ~33 ms warm.

#### 2.1.3 Retry shape around the open **[code]**

`enterViaSecureUrlWithRetries`: window 15 s past target, ≤120 attempts. Target
is `ticketOpenDate − entry_offset_ms`, default lead 150 ms (`core/arm.py`),
cap 600 ms. Cadence: if the early shot says not-open and the open is <2 s
away, spin-wait to exactly the open for the next shot; then **20 ms** while
`|offset| < 1.5 s`, 150 ms to 5 s, 300 ms beyond. The `member-info`
signature is **pre-minted 1.5 s before the burst** (`PREMINT_LEAD_MS`) and
reused for every shot; it is re-minted once if the gate answers anything
auth-shaped, and dropped after any error other than `UnableReservationTime`.
(Until 2026-09-05 a member-info GET preceded every shot, so the 0 ms shot
landed one RTT — ~60 ms — late, and the spacing was 80 ms.)

The older `/waiting` poll shape (kept for the legacy path): 100 ms before
−100 ms, **20 ms across [−100 ms, +600 ms]**, 80 ms after; ~35 requests
bounded.

A closed round still gets a perfectly good `redirectUrl` and then onestop
lands on 일정 선택 instead of the seat map **[measured, 겨울왕국 회차 024]** —
filter rounds by `saleCloseTime` first (§2.2.1).

#### 2.1.5 What the waiting room does with the key — `line-up` and `rank` **[measured 2026-09-05]**

Read out of the waiting room's own bundle
(`tickets.interpark.com/waiting/_next/static/chunks/pages/index-*.js`,
string-table deobfuscated) and then exercised live with
`tools/probe_entry_chain.py` on a quiet open show (26012391). **The place in
line is assigned by `line-up`, not by `secure-url`.** secure-url only mints
the key.

```bash
# 1. the key is the `key` query param of secure-url's redirectUrl (536 chars observed)
curl -X POST 'https://ent-waiting-api.interpark.com/waiting/api/line-up' \
  -H 'Content-Type: application/json' -H 'Origin: https://tickets.interpark.com' \
  --cookie '<.interpark.com jar>' --data '{"key":"<key>"}'
→ { "waitingId": "26012391:A4Iz…:<userSeq>", "userSeq": 3777, "exist": false,
    "userAgent": "…", "clientReq": { "goodsCode", "goodsName", "channelCode": "PM", "bizCode": "61776", "lang", "from" } }

# 2. the rank, polled
curl 'https://ent-waiting-api.interpark.com/waiting/api/rank?waitingId=<waitingId>'
→ { "myRank": 1, "totalRank": 1, "bookingRate": 57, "goodsCode", "goodsName", "redirectChannel": "IOP",
    "lang", "bizCode": "61776", "k": "…", "iopAvailable": false }                       # before the session exists
→ { "myRank": 1, "totalRank": 0, "sessionId": "…", "oneStopUrl": "https://tickets.interpark.com/onestop?key=…&app_tapbar_state=hide&app_header_state=hide", "key": "…", … }   # the turn
```

Timings, warm keep-alive connections from this machine:

| Call | Warm RTT |
|---|---|
| `member-info` | **60 ms** — the slowest call in the chain, which is why it is pre-minted |
| `secure-url` | 9 ms |
| `line-up` | 14 ms |
| `rank` | 12–17 ms |
| key → line-up, all in | ~25 ms (84 ms including member-info) |

Semantics observed:

- After `line-up` the rank answers `myRank 1 / totalRank 1` with **no**
  `sessionId` for ~1.7 s, then `totalRank 0` + `sessionId` + `oneStopUrl` in
  one answer. The booking session is created server-side after the line-up;
  nothing client-side shortens that.
- **`line-up` is not idempotent.** The same key lined up twice answered two
  `userSeq` (3777, 3778). Loading `/waiting?key=` after a line-up made here
  therefore lines up *again*, at a later place.
- `oneStopUrl` carries the round inside `key`; `playSeq` is not visible on it.
- Page behaviour (bundle): line-up has a 3 s abort and 3 retries at 1 s, 2 s,
  3 s; rank is polled every `1000·(random()+2)` ms (2–3 s) with back-off to
  10 s on errors; on `oneStopUrl` the page does `location.replace`; `myRank -1
  && totalRank -1` → `ExpiredSession`; `myRank 0` without `sessionId` →
  `ExpiredExistedSession`; `exist: true` shows the "already waiting on
  another device" screen with a 새로 대기 button (`refresh-url {key}` →
  new `redirectUrl` with `nw=Y`).
- CORS preflight: all four `/waiting/api/*` endpoints answer
  `access-control-allow-origin: https://tickets.interpark.com` with
  `allow-credentials: true`; from `nol.yanolja.com` no ACAO header.

The app (`enterQueueDirect`) now makes both calls from the goods page the
instant secure-url answers, polls rank every 150 ms, and navigates straight
to `oneStopUrl`. The waiting page is loaded only when line-up or rank fails
terminally.

### 2.1.4 Legacy queue route — dead under SSO **[measured]**

```
GET https://api-ticketfront.interpark.com/v1/goods/{goodsCode}/waiting
      ?channelCode=pc|cp|lo&preSales=N|Y&playDate=yyyyMMdd&playSeq=001
```

`channelCode`: `pc` default, `cp` camping, `lo` lottery. Response `data` is a
string: an `https://…` queue URL, `"N"` (no queue → POST BookSession), `"NP"`
(presale auth failed), `"BL"` (blocked); 401 with the 자동 로그아웃 body for
every show under a NOL/Naver SSO login. Captured: `research/api_shapes/waiting_probe.json`.

BookSession (legacy engine, when `/waiting` says `N`):

```
POST https://poticket.interpark.com/Book/BookSession.asp   (form, target=_self)
GroupCode={goodsCode}&Tiki=N&Point=N&PlayDate={yyyyMMdd}&PlaySeq={001}&lottery=
```

### 2.2 Schedule and goods info

#### 2.2.1 `GET /api/ticket/v2/reserve-gate/goods-info` — the authoritative round list **[measured]**

```bash
curl 'https://tickets.interpark.com/api/ticket/v2/reserve-gate/goods-info?bizCode=61776&goodsCode=26011315&lang=ko&placeCode=26000914' \
  -H 'Accept: application/json'
```

`placeCode` is required (400 without). No cookies needed; answers cross-origin.

```jsonc
{
  "goodsCode": "26011315",
  "goodsName": "마룬5 내한공연",
  "placeName": "킨텍스 제1전시장 4,5홀",
  "ticketOpenDate": "20260814120000",        // yyyyMMddHHmmss, KST
  "playSeqList": [
    {
      "playSeq":       "001",                 // 001…NNN; the value secure-url accepts
      "playDate":      "20270127",
      "playTime":      "2000",                // HHmm
      "dayOfWeek":     "수",
      "saleOpenTime":  "20260814120000",
      "saleCloseTime": "20270126170000"       // round is bookable while now < this
    }
  ]
  // per-round sold-out flag: not in this payload; use remaining-seats (§2.2.3) or the bitmap (§2.3.3)
}
```

#### 2.2.2 NOL product-side schedules **[measured]**

```
GET https://nol.yanolja.com/ticket/products/api/schedules
      ?goodsKey={goodsCode}:{placeCode}&playStartDate=yyyy-MM-dd&playEndDate=yyyy-MM-dd
Referer: https://nol.yanolja.com/ticket/products/{goodsCode}
```

Empty dates → HTTP 500; always send a real window. Response
`{ "content": [ { "playSeq" | "playSequence", "playDate", "playTime", … } ] }`.

#### 2.2.3 Remaining seats per grade

NOL side (same-origin from the product page):

```
GET https://nol.yanolja.com/ticket/products/api/remaining-seats?goodsCode={goodsCode}&playSeq={playSeq}
→ { "remainSeat": [ { "playSeq":"001", "seatGrade":"1", "seatGradeName":"VIP석", "remainCnt": 0 } ] }
```

Ticketfront side (public, needs `Referer: https://tickets.interpark.com/`;
no ACAO for NOL pages, so the Python panel calls it):

```
GET https://api-ticketfront.interpark.com/v1/goods/{goodsCode}/playSeq/PlayDate/{yyyyMMdd}/ALL
GET https://api-ticketfront.interpark.com/v1/goods/{goodsCode}/playSeq/PlaySeq/{001}/ALL
```

Envelope (`research/api_shapes/playDate_ALL.json`):

```jsonc
{
  "common": { "messageId": null, "message": "success", "requestUri": "/v1/goods/26011315/playSeq/PlayDate/20270127/ALL",
              "gtid": "", "timestamp": null, "internalHttpStatusCode": 200 },
  "data": {
    "remainSeat": [ { "playSeq": "001", "seatGrade": "1", "seatGradeName": "EARLY ENTRY PACKAGE", "remainCnt": 0 }, … ],
    "casting": [],                               // cast list per round (empty for this show)
    "limitMaxStayDate": 0, "playSeqList": null   // PlaySeq variant only
  }
}
```

`GET /v1/goods/{code}/playSeq` alone answers `"data": "class java.lang.IllegalArgumentException"`
**[measured]** — use the `PlayDate/…/ALL` or `PlaySeq/…/ALL` forms.

Traps **[measured]**: `remainCnt` is 0 for a round not on sale even when the map
holds hundreds of free seats; shows with quality flag `C5021` (HIDE_REMAINSEAT)
publish nothing useful. The bitmap (§2.3.3) is authoritative — one show showed
`remainCnt 0` with 228 seats genuinely free. On a selling round the two agree
exactly or with a standing offset (202/202; 96th round two apart across 11
samples), which is why the count is usable as a *change trigger* (~132 ms per
call) but not as a value.

#### 2.2.4 Goods summary and catalogue **[measured, `research/api_shapes/`]**

```
GET https://api-ticketfront.interpark.com/v1/goods/{goodsCode}/summary
GET https://api-ticketfront.interpark.com/v1/goods/{goodsCode}/prices/group
GET https://api-ticketfront.interpark.com/v1/goods/{goodsCode}/bestprices/group
GET https://api-ticketfront.interpark.com/v1/goods/{goodsCode}/tab/addition      (cancel fees, delivery)
GET https://api-ticketfront.interpark.com/v1/Place/{placeCode}
GET https://api-ticketfront.interpark.com/v1/bizInfo/{bizCode}
GET https://api-ticketfront.interpark.com/v1/ranking/realtime
Headers: User-Agent (Chrome), Accept: application/json, Referer: https://tickets.interpark.com/, Origin: https://tickets.interpark.com
```

Summary fields that decide the engine and the flow (full capture in `summary.json`):

| Field | Meaning |
|---|---|
| `isIngredientOnestop` | false → `legacy-poticket` engine (no onestop seat API) |
| `isReservedSeat` | false → general admission, quantity only |
| `isSportOneStop` | true → `seatType=SPORTS` |
| `isCaptcha` | captcha will appear on the seat step |
| `isPaymentSeparate`, `isOnlyDelivery` | checkout shape |
| `goodsQualityList` | CSV of flags: `C5021` HIDE_REMAINSEAT, `C5025` GLOBAL_BOOKING, `C5027` WAITING (queue), `Q2368` NOL_ONLY |
| `specialSeatingCode` `DM003` | 단독판매 |
| `ticketOpenDate` `yyyyMMddHHmm`, `bookingEndDate`, `limitStartDate/EndDate` | sale window |
| `bizCode` | the goods' own biz code (`29283`) — **not** what `secure-url` wants |
| `placeCode`, `placeName`, `goodsStatus` (`Y`), `soldOut` (null on capture) | |
| `currentTime` | server epoch ms — a second clock source |
| `topingGrades[]` | opaque 15-char grade ids (purpose not observed) |

Engine spread over 94 NOL shows **[measured]**: onestop-reserved 58,
onestop-general-admission 26, legacy-poticket 9, onestop-sports 1.

`prices/group` shape: `{ "<seatGradeName>": { "<priceTypeName>": [ { seatGrade, priceGrade, priceTypeCode, salesPrice, discountRate, startDate, endDate, … } ] } }`.
`seatGrade` is a **per-show ordinal**, not a global scale (`"1"` is VIP석 on a
musical, EARLY ENTRY PACKAGE on Maroon 5, starts at `10` on some KBO games).

### 2.3 Seat map and geometry

All under `https://tickets.interpark.com/onestop/api`. Common query:

| Param | Value |
|---|---|
| `goodsCode` | goods |
| `placeCode` | venue (from summary / initData) |
| `playSeq` | round, `001`… |
| `bizCode` | channel code — `WEBBR` (from initData; sweep of `""`, `MOBILE`, `WEB`, `NOL`, goods bizCode makes no difference to seatMeta **[measured, probe_status_detail.py]**) |
| `blockKeys` | repeated param, `AAA:BBB` |

Adding `playDate` to any of these → HTTP 400 **[measured]**.

#### 2.3.1 `GET /onestop/api/seats/block-data` — section polygons **[measured; needs session]**

```bash
curl 'https://tickets.interpark.com/onestop/api/seats/block-data?goodsCode=26011315&placeCode=26000914&playSeq=001' \
  -H 'X-Onestop-Session: …' -H 'X-Onestop-Channel: ONESTOP' -H 'X-Ticket-BFF-Language: KO' \
  -H 'X-OneStop-Trace-ID: aB3…' -H 'X-Requested-With: XMLHttpRequest' --cookie '…'
```

400 without a booking session. Response is a list (or `{blocks:[…]}` /
`{data:{blocks}}` — the app accepts all three):

```jsonc
[
  {
    "blockKey":        "001:001",     // "<playSeq>:<block>" — the round is embedded
    "blockName":       "스탠딩 A",     // or "selfDefineBlock"
    "selfDefineBlock": "A1",
    "absoluteLeft":    865.912,       // venue-image px
    "absoluteTop":     308.564,
    "absoluteRight":   982.912,
    "absoluteBottom":  380.564
  }
]
```

Block-key space **[measured, probe_blockkeys.py / probe_remains_vs_bitmap.py]**:
the first triple is the round (`001:001` on round 001, `096:001` on round 096
of the same venue); the second is the block index. Sejong Center (세종문화회관,
3 022 seats) encodes the floor in the hundreds digit: `001:1xx` 1층, `001:2xx`
2층, `001:3xx` 3층 **[measured]**. Without a session, block keys can be
recovered by walking `seatMeta` over `NNN:001…NNN:063` until empty.

#### 2.3.2 `GET /onestop/api/seatMeta` — static layout **[measured; no session needed on most shows]**

```bash
curl 'https://tickets.interpark.com/onestop/api/seatMeta?goodsCode=26005128&placeCode=17000549&playSeq=001&bizCode=WEBBR&blockKeys=001%3A001&blockKeys=002%3A002' \
  -H 'Accept: application/json' -H 'Referer: https://tickets.interpark.com/onestop/seat'
```

Response — list, **in requested block order** **[measured, probe_ordering.py]**:

```jsonc
[
  {
    "blockKey": "001:001",
    "seats": [
      {
        "seatInfoId":    "26005128:17000549:001:1",   // "<goods>:<place>:<playSeq>:<seatSerial>"
        "seatGrade":     "1",                          // null when the round is not on sale / not graded yet
        "seatGradeName": "VIP석",
        "floor":         "1층",                        // may be "" (KINTEX, 장충) or "객석1층"
        "rowNo":         "A열",                        // "A구역 입장번호" for standing entry numbers
        "seatNo":        "10",
        "salesPrice":    77000,
        "seatGroupId":   null,                         // set on package/table seats — atomic group
        "posLeft":       98.107,                       // venue coords, same space as block absolute*
        "posTop":        68.81,
        "rowIdx":        0,
        "colIdx":        10,
        "isExposable":   true                          // "part of the sellable map" — NOT "free"
      }
    ]
  }
]
```

Batch limit: ≥12 `blockKeys` per request accepted **[measured, probe_blockkeys.py BATCH=12]**;
`probe_batchlimit.py` sweeps 1…12 and the true ceiling was not pinned higher.
The app fetches with concurrency 6 (`SEAT_META_CONCURRENCY`).

Observed venue sizes (`research/seatmaps/summary.json`):

| Venue | Blocks | Seats | Exposable | Graded |
|---|---|---|---|---|
| 킨텍스 (Maroon 5) | 24 | 21 460 | 960 | 960 |
| 장충체육관 | 34 | 3 699 | 44 | 44 |
| 극장 용 | 3 | 835 | 773 | 773 |
| 화성예술의전당 | 11 | 1 450 | 0 (pre-open skeleton) | 0 |
| 세종문화회관 **[sketch only]** | 001:1xx–3xx | 3 022 | — | — |

Seat sketch cache (`mac/.nolsniper_sketch_{goods}.json`): `{ goods_code, at,
sketch:[{k:"002:003", x, y}], blocks:[{key, name, left, top, right, bottom}] }`
— `posLeft/posTop` and the block `absolute*` box share one coordinate space.

#### 2.3.3 `GET /onestop/api/seatStatus` — live availability bitmap **[measured]**

Same query as seatMeta. **Maximum 2 `blockKeys` per request; 3 → HTTP 400.**

```bash
curl 'https://tickets.interpark.com/onestop/api/seatStatus?goodsCode=26005128&placeCode=17000549&playSeq=001&bizCode=WEBBR&blockKeys=001%3A001&blockKeys=002%3A002'
```

```json
{ "data": [ "F8000000…", "0000…" ] }
```

One hex string per **requested** block, positional (no keys), 4 seats per
character, MSB first, aligned to that block's `seatMeta.seats` order. Bit = 1
means **free right now**. A seat is takeable only when `isExposable && bit`.
A 538-seat block is 135 hex chars ≈ 148 bytes on the wire.

Response headers **[measured, probe_status_cache.py]**:
`Cache-Control: no-cache, no-store, must-revalidate`, CloudFront miss — never
cached. The bundle also mentions a `last-seat-modified` header on this call
**[bundle]**.

Trap **[measured]**: `seatStatus` keys off the round embedded in `blockKey`,
not the `playSeq` parameter — `blockKeys=001:001&playSeq=096` answers round
001's bitmap with no error.

#### 2.3.4 `GET /onestop/api/seats/grades` — remain per grade inside the session

```
GET /onestop/api/seats/grades?goodsCode&placeCode&playSeq&bizCode=WEBBR
→ [ { "seatGrade", "seatGradeName", "remainCount" | "remainSeatCount" | "remainCnt" } ]   (or wrapped in {grades} / {data})
```

Has answered `remainCount 0` for every grade while the map still sold
**[measured]** — not trusted as a sold-out signal.

#### 2.3.5 Other seat-side endpoints **[bundle only — never called here]**

```
GET  /onestop/api/seats/init/{goodsCode}
GET  /onestop/api/seats/clear-select/…
GET  /onestop/api/seats/book-wait/…          (예매대기 twin flow)
POST /onestop/gql  query GetRemainSeats       → grades, isNonReservedSeat, isAutoAssignSeat
```

#### 2.3.6 Official client rendering behaviour **[bundle, re-checked live]**

On `/onestop/seat` load only `block-data` is fetched; no circles. Pan/zoom is
debounced ~100–300 ms, an R-tree picks `blockKey`s intersecting the viewport,
and *only new* blocks trigger `seatMeta` + `seatStatus`. Circle target sizes
`[0, 4, 12, 24, 48]` px; below `seatVisibleLevelScale` (~12 px) a click zooms
toward the point instead of selecting. Opening a 구역 fires **six `seatStatus`
requests inside 13 ms** and the page bursts at roughly **460 requests/s**
**[measured from a recorded session]** — the ceiling the gateway evidently
tolerates from its own client.

### 2.4 Seat selection and hold

#### 2.4.1 `POST /onestop/gql` — `PreselectSeat` (soft hold) **[measured]**

```bash
curl -X POST 'https://tickets.interpark.com/onestop/gql' \
  -H 'Content-Type: application/json' -H 'X-Onestop-Session: …' -H 'X-Onestop-Channel: ONESTOP' \
  -H 'X-Ticket-BFF-Language: KO' -H 'X-OneStop-Trace-ID: …' -H 'X-Requested-With: XMLHttpRequest' --cookie '…' \
  --data '{
    "query": "mutation PreselectSeat($command: PreselectSeatCommand!) { preselectSeat(command: $command) }",
    "variables": { "command": { "playSeq": "001", "blockKey": "001:001", "seatGrade": "1",
                                "seatInfoId": "26005128:17000549:001:1" } }
  }'
→ { "data": { "preselectSeat": true } }
```

Bulk variant:

```graphql
mutation BulkPreselectSeats($command: BulkPreselectSeatsCommand!) { bulkPreselectSeats(command: $command) }
# variables.command = { playSeq, blockKey, seatGrade, seatInfoIds: [ … ] }
```

**For a single seat the bulk mutation answers `P40021 좌석 요청이 잘못 되었습니다`
while the singular one answers `true` for the same seat in the same session
[measured]** — bulk is for seat groups.

Release:

```graphql
mutation BulkDeselectSeats($command: BulkDeselectSeatsCommand!) { bulkDeselectSeats(command: $command) }
# variables.command = { seatInfoIds: [ … ] }
```

(`deselectSeat` singular also exists **[bundle]**.)

Auto-assign (non-map shows / server-side allocator):

```graphql
mutation AutoAssignSeats($command: AutoAssignSeatsCommand!) {
  autoAssignSeats(command: $command) { seatInfoIds success errorCode errorMessage }
}
# variables.command = { playSeq, blockKey|null, seatGrade, seatInfoIds: [] }
```

GraphQL errors arrive as `{ "errors": [ { "message", "extensions": { errorCode, abuseStage, retryAfterMs, classification } } ] }`.

**Critical finding [measured 2026-09-03, `tools/probe_soft_hold.py`]:** a bare
API `preselectSeat` returns `true` and locks the seat server-side, but the
SPA's 선택 좌석 never moves — React only updates from *its own* handler's
response. Pressing 선택 완료 in that state is the empty-cart failure
`P40021 CONFIRM_PRESELECTION_INVALID` / 좌석 선택 도중 오류, with the seat
still held against the account's allowance. Seats are therefore taken by
dispatching a real pointer press on the rendered `circle.js-seat` (whose fiber
props are `{ seat, blockKey, isSelected, isDisabled, onSeatClick }`) and
letting the page make its own PreselectSeat.

#### 2.4.2 Captcha **[bundle + live modal]**

Trigger: `summary.isCaptcha=true`; appears as a modal on `/onestop/seat` (and
over the calendar on `/onestop/schedule`) with copy
「화면의 문자를 입력해주세요」, 6 characters, 5-minute TTL.

Official endpoints as read from the bundle (the autopilot **does not call
them**; the user types the answer):

```
POST /onestop/api/captcha/image
     → { "Img": "<base64>", "EncRnd": "<challenge id>" }
GET  /onestop/api/captcha/verify?p1={answer}&p2={sessionId}&p3={goodsCode}&p4={entMemberCode}&p5={bizKind}&p9={EncRnd}
     → { "result": "Y" }
GET  https://aspseat-ticket.interpark.com/CommonAPI/Captcha/GetCaptchaAudio?v=en&t={EncRnd}   (English audio)
```

Note the inconsistency inside the repo: `mac/README.md` says the captcha path
uses these endpoints with host-side OCR; `docs/interpark_flow.md` and the
autopilot source say the official captcha APIs are **not** used and the macro
never types a captcha. The source is authoritative — treat the endpoint shape
as **[bundle]** and unverified.

#### 2.4.3 Cart commit — `POST /onestop/api/seats/select` **[measured]**

Pressed by the page's 선택 완료; the app only issues it itself on the
auto-assign path.

```bash
curl -X POST 'https://tickets.interpark.com/onestop/api/seats/select' \
  -H 'Content-Type: application/json' -H 'X-Onestop-Session: …' … \
  --data '{
    "goodsCode":  "26005128",
    "placeCode":  "17000549",
    "playSeq":    "001",
    "sessionId":  "<initData.sessionId>",
    "seatType":   "DEFAULT",            // "SPORTS" when isSportOneStop / isSportsGroup / kindOfGoods=01007
    "autoAssign": false,
    "seats": [ { "seatGrade": "1", "seatInfoId": "26005128:17000549:001:1" } ]
  }'
```

`goods.isInterlocking=true` → `/onestop/api/seats/select-external` instead.

Responses:

| Status | Body | Meaning |
|---|---|---|
| 200 | `{ …, "unselectableSeatInfoIds": [] }` | committed; page moves to `?step=price` |
| 200/400 | `unselectableSeatInfoIds: [ids]` (top level on 200, under `data.data` on 400) | those seats refused — drop and retry others |
| 400 | `P40021 좌석 요청이 잘못 되었습니다` / `CONFIRM_PRESELECTION_INVALID` | no live preselect for the seat (page cart empty, hold missing, or after a gateway block) |
| 400 | `예매 가능 매수를 초과하였습니다` | allowance exhausted by stranded holds — release with BulkDeselectSeats |
| 4xx | `로그인` / `세션이 만료` / `인증이 필요` | session gone |
| 403 | `GATEWAY_ABUSE_BLOCKED` | §3.1 |

Page-side race note **[measured]**: 선택 완료 pressed while the page's own
preselect is in flight is refused (`seat_requestPending` / 선택 도중 오류);
after 전체삭제 the page holds an in-flight flag for ~700 ms.

### 2.5 Schedule step (`/onestop/schedule`) **[measured]**

DOM-driven; no API. Calendar grid `[class*='EntCalendar_grid']`, month heading
`[class*='EntCalendar_month']` (`yyyy.MM`, one heading names the *active* swiper
slide; adjacent slides are ±1 month), day buttons
`button[class*='EntCalendar_dateButton']`, time blocks
`button[class*='TimeBlock_timeButton']` (`aria-pressed`), then 다음 / 변경하기.
The 예매 안내 gate (「확인하고 예매하기」) can sit over the calendar. 일정변경 on
the seat map opens the same calendar as a modal without a navigation.
Step timeout 15 s.

### 2.6 Price step (`/onestop/seat?step=price`)

DOM-driven: quantity, birth `YYMMDD`, delivery (배송 / 현장수령), payment
method (무통장입금 default), consent checkboxes, 다음/확인. Entering it with an
empty cart shows 「구매하실 좌석을 선택해주세요」; the app strips `step` and
returns to the map.

### 2.7 Order creation and payment **[bundle — never exercised]**

```
POST /onestop/api/payment/order/{goodsCode}     ← the commit; buttons 결제하기/결제완료/입금하기/구매하기/주문완료/결제진행 are excluded by test
→ /onestop/payment → /onestop/complete
PG hand-off: order-gw.yanolja.com (cookies pay-correlation-id, pay-goods-type, pay-cancel-uri, result-send-way, pay-lang) → nol-payment.yanolja.com
```

Request/response shape, order-token generation, and the PG payload are
**[not observed]** in this repository. The seat is already held once the
page's preselect succeeds, so everything from here is not on the race path.

---

## 3. Rate limits, anti-bot, and fingerprinting

### 3.1 Gateway abuse throttle **[measured]**

Per-account, answers on every `/onestop/*` call while it holds:

```json
{ "errorCode": "GATEWAY_ABUSE_BLOCKED", "abuseStage": "BLOCKED",
  "retryAfterMs": 165470, "classification": "FORBIDDEN" }
```

Delivered as: the GraphQL `errors[].extensions` on `/onestop/gql`; the same
fields at the top level of a REST 4xx body on `seatMeta` / `seatStatus` /
`block-data` / `grades`; a bare 403/429 with or without `Retry-After`
(seconds); and the legacy queue API's one-word `BL`. **Retrying through a block
extends it.** Observed lockout ≈165 s. `preselect` failing under a block is what
makes the following `select` say `P40021`.

The exact request-rate threshold has not been measured. Bounds that are known:

| Rate | Outcome |
|---|---|
| ~460 req/s burst, six `seatStatus` in 13 ms | what the official page itself does when opening a 구역 — tolerated |
| ≤60 req/s sustained (`CATCH_MAX_REQUESTS_PER_SEC`), 6 in flight (`SWEEP_CONCURRENCY`) | the app's ceiling; no block observed at this rate |
| ~8 req/s sustained (100 ms tick × 1 req, or 4 req/tick untriggered) | the default watch |
| 50 req/s for 15 s on the queue endpoint | explicitly avoided — a block at the open is unrecoverable |

There is no separate 429 from `api-ticketfront` on the polls used here
(one `/playSeq/PlayDate/…/ALL` every 2 s). Cloudflare `__cf_bm` / `cf_clearance`
sit in front of both `.yanolja.com` and `.interpark.com`; a lost
`cf_clearance` produces a challenge page, not a 429.

### 3.2 Header and origin verification **[measured]**

| Check | Where | Effect if wrong |
|---|---|---|
| `Referer` / `Origin` = `https://tickets.interpark.com` | `api-ticketfront…/v1/*` | 403 |
| CORS allow-list = `https://tickets.interpark.com` only | `api-ticketfront` `/waiting`, `ent-waiting-api` `secure-url` | browser `TypeError: Load failed`; 215 dead attempts across a 15 s window from a NOL page |
| SameSite cookie scoping | `reserve-gate/member-info` | 401 from any non-`.interpark.com` document |
| `bizCode=61776` (partner) vs goods `bizCode` | `secure-url` | 400 |
| `placeCode` present | `goods-info` | 400 |
| No `playDate` | `block-data` / `seatMeta` / `seatStatus` / `secure-url` | 400 |
| `blockKeys` ≤ 2 | `seatStatus` | 400 |
| `Sec-Fetch-*`, `User-Agent` | **[not observed]** — nothing in the repo shows these being enforced; the Python probes succeed with a plain desktop Chrome UA and no `Sec-Fetch` headers |
| Payload encryption / JS challenge | **[not observed]** on any onestop or ticketfront call. The only encoded material is the `signature.secureData` pair (opaque, server-minted) and Cloudflare's cookies. `goodsNameEucKr` in summary is just a URL-encoded EUC-KR label. |

### 3.3 Browser fingerprint

The Windows host launches real Chrome with
`--disable-blink-features=AutomationControlled` and **without**
`--enable-automation`, so `navigator.webdriver === false` on the live page
**[measured]**. macOS uses WKWebView (Safari UA) with `window.open` shimmed to
`_self`. No UA spoofing is done; the seat circles are pressed with real
pointer events rather than `.click()`, because the page's handler reads
`(!isDisabled || isSelected) && onSeatClick(seat, isSelected, blockKey)` from
`pointerup`.

---

## 4. Bottleneck and friction analysis

### 4.1 Network floor **[measured]**

| Segment | Cost |
|---|---|
| Cold TCP+TLS to a booking host | ~37 ms |
| `secure-url` POST, warm | ~33 ms |
| legacy `/waiting` GET, warm | ~11 ms |
| `seatStatus`, two blocks | 29 ms best; 31–72 ms range, median 50 ms; ~58 ms quoted in `watch_trigger.py` |
| `seatMeta`, one block | comparable; batched ×12 |
| `remaining-seats` (ticketfront) | ~132 ms |
| page's own `PreselectSeat` RTT | "a few hundred ms" — 220 ms used as the bench constant, 250 ms and 389 ms noted live |
| whole-venue sweep, 34 blocks, sequential | 17 requests ≈ 4.4 s; with 6 in flight ≈ 490 ms → ~one RTT |
| 8-block venue (겨울왕국), 4 requests | 50 ms median whole-venue (30–96 over 33 laps) |
| 75-block venue (26006903) at 1 req / 154 ms tick | 5.9 s lap — the reason the untriggered budget is 4 req/tick |

### 4.2 Stage-by-stage: browser DOM path vs. direct API

```
Stage                    Browser/DOM path (measured)                    Direct API path            Winnable?
────────────────────────────────────────────────────────────────────────────────────────────────────────────
Open detect              button enables "some unpredictable moment"     secure-url every 20–80 ms  yes — API
                         after 정각; 15 ms click poll, ≤1.5 s force      33 ms RTT, -150 ms lead    already used
Queue navigation         cold host, ~37 ms handshake                    preconnect learned host    partly
Gate → schedule          /gates/partner boot + /onestop/schedule         cannot be skipped on       no (server
                         (DOM: modal clears, 15 s budget)               isMultiPlay [measured]     forces it)
Seat map load            block-data → lazy seatMeta/seatStatus per       seatMeta ×12 batched,      yes — API
                         zoomed block, 100–300 ms debounce, circles      6 concurrent; no zoom      (pre-open,
                         only above ~12 px                               needed                     no login)
Detect a free seat       page refetches on its own pan/zoom             seatStatus 29–72 ms,       yes — API
                                                                        2 blocks/req, 30 ms focus  (this is the
                                                                        poll, 6 in flight          catch loop)
Take the seat            real pointer press on circle → page's own       API preselectSeat = true   NO — the hold
                         PreselectSeat (220–390 ms) → cart re-render     in ~RTT but invisible to   is a dead end
                         (~1 frame; noticed within 16/24 ms ramp)        React → P40021 on confirm  [measured]
Confirm                  선택 완료 press → POST /seats/select              same POST                  page-gated
Price step               DOM form fill                                  — (no API observed)        n/a
Payment                  manual                                         POST payment/order [bundle] out of scope
```

### 4.3 Where ≥100 ms goes, and what already removed it **[measured]**

| Latency sink | Was | Now | Remaining floor |
|---|---|---|---|
| Open detection blind spot (live `secure-url` route) | 80 ms spacing, plus a ~60 ms member-info GET in front of every shot | pre-minted signature, 20 ms spacing → ≤20 ms, 10 ms avg | one RTT (9 ms warm) |
| Queue position assigned late | key handed to the waiting page, which lines up only after its ~1.3 MB boot, then polls rank every 2–3 s | `line-up` + `rank` from the goods page: key → place in ~25 ms; turn noticed within 150 ms | ~1.7 s server-side session creation (measured, not ours) |
| Entry on the wrong route | 15 s lost to the dead `/waiting` (401) and 215 CORS-dead attempts from NOL origin; 2026-09-04 17:00 the arm fired from nol.yanolja.com and took the 예매하기 → SSO → gates path (seconds) | `secure-url` from a parked `tickets.interpark.com/goods/{code}` page; forced park at arm time, scheduler relocates itself if the window leaves the origin | 0 |
| Cold queue host | ~37 ms handshake on the navigation | `<link rel=preconnect>` to the remembered host | ~0 warm |
| Clock error | single `Date` read: 0–1 s early | boundary-bracketed sync, hosts within 12 ms | ~10 ms |
| `currentOpenBlock()` | 925 ms per candidate (nested venue scan) | <100 ms (indexed) | — |
| `clickSeatOnMap()` | 2.7 ms linear fiber walk | <25 ms ceiling, indexed | — |
| `checkDomAgreement()` | 2.5 ms per seat per tick | <25 ms ceiling | — |
| Cart notice lag | 65.6 ms (16 → 80 ms ramp) | ≤45 ms (16 ms ×8, 24 ms ×36, then 80 ms) | ~1 frame |
| Sweep serialisation | 17 sequential RTTs ≈ 490 ms | 6 in flight ≈ 1 RTT | 1 RTT |
| Focus poll cadence | 200 ms floor, 87 % idle | 30 ms, timer-free (MessageChannel yield), ≤60 req/s | RTT-bound |
| Stale round in `initData` | polled a round nobody was watching → "sold out" | `blockKey` prefix from DOM circles | 0 |
| Schedule step | sending `playSeq` does not skip it | driven by DOM, 15 s budget | server-forced |
| DOM render lag on a busy map (`domAgreedMs`) | ~1 s | unchanged — page-side | page-side |

### 4.4 Theoretical minimum per stage (toward pure RTT, <20 ms)

1. **Open → queue slot.** Floor is one `secure-url` RTT (33 ms warm) plus
   the clock error (~10 ms). Only reducible by (a) a persistent HTTP/2
   connection to `ent-waiting-api` kept warm through the open (already done
   by the pre-open shots), (b) a colocated egress in the same region as the
   CloudFront edge to bring RTT toward ~10 ms, and (c) pre-minting
   `member-info` a few hundred ms before the fire so the two calls are not
   serialised (the signature is time-stamped, so minting it too early risks
   rejection — the safe lead is unmeasured).
2. **Seat discovery.** Layout (`seatMeta`) and a first bitmap need no login,
   so the whole venue can be pre-indexed before the open; at open the cost is
   one `seatStatus` per 2 blocks with 6 in flight ≈ one RTT (29–50 ms). The
   2-block cap is the hard limit; the only lever below it is targeting fewer
   blocks (hyper-focus on one block already does 30 ms polling).
3. **Detect → press.** Already sub-frame: bit index = seat index, indexed
   circle lookup, timer-free loop. Floor is the DOM event dispatch (~1 ms).
4. **Press → hold.** This is the page's own `PreselectSeat` RTT (220–390 ms)
   and it **cannot be bypassed**: an API hold is real on the server but the
   SPA does not know about it, and `select` is gated on the SPA's state
   **[measured]**. The only winnable slice is `click → preselectSent` (how
   long React sits on the press before fetching), which the app now measures
   per catch (`catchTimings.clickToPreselect`). Reaching <20 ms here would
   require making the page's own request faster, i.e. server-side.
5. **Hold → confirm.** Floor is one frame to notice the cart (16 ms) plus a
   quiet gap so 선택 완료 is not pressed while the page's own fetch is still
   in `finally()` — measured refusal otherwise. Then one `seats/select` RTT.
6. **Whole-venue trigger.** 132 ms on ticketfront is slower than one
   `seatStatus` RTT; it only pays when the watched area exceeds ~4 blocks.

Net: everything up to and including detection is at or near RTT already; the
irreducible >100 ms item is the site's own preselect round trip, and the
measured dead end of the API soft hold is the reason it stays on the DOM path.

---

## 5. Error and status code catalogue

| Code / string | Source | Meaning |
|---|---|---|
| `UnableReservationTime` | `secure-url` | not open yet |
| `AccessDenied_Blacklist` | `secure-url` | account blocked — never retry |
| `"N"` / `"NP"` / `"BL"` / `https://…` | legacy `/waiting` `data` | no queue → BookSession / presale auth failed / blocked / queue URL |
| `오랜 시간 이용하지 않아 자동 로그아웃되었습니다…` (HTTP 401) | `api-ticketfront` | realm not populated by SSO login |
| `class java.lang.IllegalArgumentException` (HTTP 200) | `api-ticketfront /playSeq` | wrong route shape |
| `GATEWAY_ABUSE_BLOCKED` / `abuseStage=BLOCKED` / `retryAfterMs` | any `/onestop/*` | throttle, §3.1 |
| `P40021 좌석 요청이 잘못 되었습니다` | `gql` bulk on 1 seat; `seats/select` | invalid request / no live hold |
| `CONFIRM_PRESELECTION_INVALID` | `seats/select` | cart/hold mismatch |
| `unselectableSeatInfoIds` | `seats/select` (200 top-level, 400 under `data.data`) | seats refused |
| `예매 가능 매수를 초과하였습니다` | `seats/select` | allowance used by stranded holds |
| `seat_requestPending` / 선택 도중 오류 | page | confirm pressed mid-flight |
| `구매하실 좌석을 선택해주세요` | price step | empty cart |
| `TypeError: Load failed` | browser | CORS-refused (wrong origin) |

---

## 6. Provenance index

| Fact | Where it is established |
|---|---|
| secure-url route, body, no-cookie, no-date, 33 ms | `core/entry.py`, `browser/nolsniper_autopilot.js:1950–2030`, `tests/test_entry.py` |
| member-info SameSite 401 from NOL | `mac/nolsniper.py:_park_for_entry`, `core/entry.py` |
| CORS allow-list on queue hosts | `browser/nolsniper_autopilot.js:2985–3020` |
| ticketfront `/waiting` 401 under SSO | `research/api_shapes/waiting_probe.json`, `core/entry.py` |
| goods-info shape, placeCode 400, bizCode 61776 | `core/showinfo.py:fetch_goods_info_rounds`, `autopilot.js:2633–2700` |
| summary / prices / remain shapes | `research/api_shapes/*.json`, `inspect_report.json` |
| seatMeta shape and venues | `research/seatmaps/*.json`, `summary.json`, `dump_seatmaps.py` |
| seatStatus bitmap semantics, 2-key cap, never cached, ordering | `probe_bitmap*.py`, `probe_batchlimit.py`, `probe_status_cache.py`, `probe_ordering.py`, `mac/README.md` |
| block key embeds the round | `probe_remains_vs_bitmap.py`, `autopilot.js:936–960` |
| Sejong floor digits | `autopilot.js:3832`, `tests/test_latency_ceilings.py:98` |
| API soft hold dead end | `tools/probe_soft_hold.py`, `autopilot.js:8600–8800`, memory `api-soft-hold-dead-end` |
| bulk vs singular preselect P40021 | `autopilot.js:7009` |
| gateway abuse block shape | `autopilot.js:8040–8110`, `mac/README.md` |
| official page 460 req/s, 6 in 13 ms | `autopilot.js:340–360, 8255` |
| clock offsets and estimator | `probe_clock.py`, `validate_clock_estimator.py`, `core/clock.py` |
| catch-path ceilings | `tests/test_catch_latency_budget.py`, `tests/test_latency_ceilings.py`, `tests/bench_catch_latency.mjs` |
| cookie inventory | `mac/.nolsniper_cookies.json` (names only), `mac/browser_session.py` |
| captcha endpoints | `mac/README.md` (bundle-derived; unverified by code) |
| payment endpoint | `mac/README.md`, `autopilot.js:1840` (never called) |
