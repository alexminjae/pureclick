# Interpark ticketing flow & API recon

Findings from live probing (Aug 2026) plus endpoints reverse-engineered from
the earlier browser autopilot. PureClick's shipped Phase 2 (color-change watch)
does **not** call these APIs — it works purely from the screen — but they are
documented here because they are the "optimal path" if we ever want a
headless/API version, and because the clock-sync endpoint choice depends on
this.

## 1. Clock sync — why `poticket`

| Endpoint | `Date` header | `x-cache` | Notes |
|---|---|---|---|
| `poticket.interpark.com/Book/BookMain.asp` | rolls every second | **Miss from cloudfront** | `cache-control: no-store,private`, tiny body. Best sync target. |
| `tickets.interpark.com/...` | often frozen | Hit/Error from cloudfront | CDN-cached, stale `Date`. Avoid. |
| `api-ticketfront.interpark.com` | live | — | JSON API host, usable but heavier. |

Confirmed live: `poticket` returns `200` with an uncached, per-second `Date`.
This is what `ServerClock.sync_tick` targets, now over a single keep-alive
connection (`KeepAliveProbe`) so poll spacing approaches bare RTT (~40 ms
observed), which tightens the second-boundary bracket.

## 2. Booking flow (reserved-seat "onestop" shows)

1. **Goods page** `tickets.interpark.com/goods/{goodsCode}` — the 예매하기
   button. Phase 1 clicks this at the synced target time.
2. **Waiting queue** `GET api-ticketfront.interpark.com/v1/goods/{goodsCode}/waiting`
   — assigns queue position, returns a `waitingUrl`. Confirmed live: returns
   `{common:{...}, data:"..."}`; missing params yield
   `data: "Required request parameter is missing"`.
3. **Seat map** `tickets.interpark.com/onestop/seat?...` — the interactive map.
4. **Seat data APIs** (all under `tickets.interpark.com/onestop/api/`, require
   the logged-in session cookie, `credentials: include`):
   - `GET /seats/block-data?goodsCode&placeCode&playSeq` → block keys. Empty →
     `{"statusCode":400}`.
   - `GET /seatMeta?goodsCode&placeCode&playSeq&bizCode&blockKeys=...` → per-seat
     availability. Empty → `{"error":{"code":"P00002","message":"goodsCode 값이 잘못되었습니다."}}`.
   - `POST /seats/select` with `{goodsCode, placeCode, playSeq, sessionId,
     seatType, autoAssign:false, seats:[{seatGrade, seatInfoId}]}` → locks the
     seat, then the page advances to `?step=price`.
5. **Payment** `?step=price` — done manually.

## 3. Cancellation catching ("취켓팅")

When someone drops a reservation, a seat re-appears on an already-loaded seat
map (or after a refresh). Two ways to catch it:

- **API polling** — loop `seatMeta` for the target block and `POST select` the
  instant a seat flips to available. Fastest, but fragile: params, session
  handling, anti-bot, and payload shape all change without notice, and it
  needs the live session cookies.
- **Screen color-change watch (shipped Phase 2)** — watch the user-framed seat
  area for a colored bubble appearing and click it. Slower by one render frame,
  but robust to every API/markup change and needs zero reverse engineering. The
  user chose this approach.

`select` returned `404` on a bare GET/POST during recon, so the exact current
contract should be re-captured from DevTools on a real drop before trusting the
API path.
