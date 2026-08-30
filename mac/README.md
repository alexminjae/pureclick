# NOL 스나이퍼

Korean control panel + embedded browser for NOL / Interpark onestop ticketing.
Everything runs against the booking APIs directly; the DOM is only used where a
form has to be filled.

## Run

```bash
cd ~/Desktop/pureclick/mac && ./run_pureclick.sh
```

A browser window titled **NOL 예매** opens next to this control panel.
They are one app:

1. **예매 창** — log into NOL, click the show, press 예매하기 when it is on sale.
2. **조작판** — fills itself from the page you opened. Buttons enable only when
   that page can actually run them (오픈 대기 on a product page before sale;
   취켓팅 only on the seat map).

You do not type a goods code. Open the show in the 예매 창 and wait a second.

**Log into NOL in the 예매 창 once** — the autopilot runs inside that session.
Credentials never leave the browser.

The login is carried across restarts. WKWebView claims a persistent data store
but a plain Python process does not get its jar back on relaunch — measured: a
cookie written in one run, closed gracefully with 20s to settle, is gone in the
next. Since NOL's Naver button is a full OAuth redirect with
`auth_type=reauthenticate`, starting logged out means a real Naver login by
hand, every launch. So the jar is saved and restored explicitly through
`WKHTTPCookieStore` (`browser_session.py`), which also keeps `cf_clearance` and
avoids a fresh Cloudflare challenge.

`mac/.pureclick_cookies.json` is a live session — anyone holding it is logged in
as you. It is written `0600` and gitignored. **Delete it to sign out.**

## One window, no popups

NOL does not navigate to the booking flow. `openPCOnestop()` opens a named
popup and drives it from the product page:

```js
win = window.open('', 'BookingPop', 'width=900,height=682');
form.target = 'BookingPop'; form.submit();      // seat booking
// or
window.self.close(); win.location.replace(waitingUrl);   // queue entry
```

WKWebView asks its host to build that popup and pywebview declines for anything
that is not a plain link click, so `win` is `null`, the POST is aimed at a
window that does not exist, and **예매하기 does nothing at all** — the seat map
is never reached and no automation downstream of it can run.

Rather than open a second window with no autopilot in it, every popup is folded
back into the one window:

| Site does | Shim does |
|---|---|
| `window.open(url, name)` | navigates this window, returns a proxy |
| `window.open('', name)` then `form.target = name; form.submit()` | rewrites `target` to `_self` — the POST lands here |
| `win.location.replace(url)` / `win.location.href = url` | navigates this window |
| `window.self.close()` | no-op, so the queue path cannot close the app |
| `<a target="_blank">` | retargeted to `_self` |

`form.submit()` never fires a submit event, so `HTMLFormElement.prototype.submit`
is patched rather than a listener added. `OPEN_EXTERNAL_LINKS_IN_BROWSER` is
turned off too, otherwise a `target=_blank` click hands the booking flow to
Safari where none of this exists.

The autopilot is registered as a `WKUserScript` at **document start** on all
frames, not injected on load. The shim has to be in place before NOL's own
bundle wires up 예매하기, and on the seat map the first few hundred milliseconds
decide whether a seat is still there. On-load injection remains as a fallback.

## Reading the page

NOL is a Next.js **App Router** site. There is no `__NEXT_DATA__`; the show data
is an RSC flight payload pushed into `self.__next_f` as JS string literals, so
every quote reads as `\"` in the DOM and a plain `/"goodsName":"…"/` never
matches. The payload is unescaped once per document before fields are read.

A product page carries `goodsCode`, `goodsName`, `placeCode`, `playStartDate`
and `bookingOpenTime` — but **no round list**. `playSeq` and the open time come
from the ticketfront API instead, which the panel fetches from the goods code.
That is why the panel only needs you to open the show.

## The two functions

### ① 오픈 대기 — be first in the queue
Set the open time (auto-filled when you load a show) and press 오픈 대기 시작.

At the moment of open it calls the queue API directly:

```
GET /v1/goods/{goodsCode}/waiting?channelCode=pc&preSales=N&playDate=&playSeq=
```

The 예매하기 button is only a client-side gate, so nothing waits for it to render.

Three things make the timing reliable:

- **The clock is synced against the host we fire at.** It used to sync on
  `poticket.interpark.com` while the queue lives on `api-ticketfront`. Measured
  by boundary-bracketing their `Date` headers: poticket +18ms, ticketfront -8ms,
  `tickets.interpark.com` +4ms — the two hosts that matter for booking agree
  within 12ms, and poticket was the 26ms outlier.
- **Burst entry, shaped.** The queue endpoint answers in 11ms warm, so a flat
  80ms poll left ~69ms of every cycle idle — the show could open and go
  unnoticed for up to 80ms. It now polls at 100ms before the open (where the
  answer cannot be yes, so the requests only keep the connection warm), **20ms
  across [-100ms, +600ms]**, and 80ms after. Average blind spot 40ms → 10ms,
  burst bounded to ~35 requests. `NP` (presale auth) and `BL` (blocked) stop
  immediately.
- **Every attempt is recorded** with its offset from the open and shown in the
  panel, centred on the flip. What `/waiting` returns *before* a show opens has
  never been observed, and the two possibilities imply opposite strategies: if
  it stays unusable until the flip, polling the boundary is right; if it hands
  out a queue URL early, arriving at the open is already too late. One recorded
  open settles it.

The queue host is learned from the first entry that returns one and
preconnected on later runs — the `/waiting` request is warm by the open, but the
navigation that follows goes to a different host, cold, at the exact moment it
is claiming your place.

**Clicking 예매하기 is not faster.** NOL's button calls `openPCOnestop()`, which
hits the same `/waiting` endpoint and then opens a named popup to drive — a
popup WKWebView will not create, which is why this file carries a shim to fold
it back into one window. Calling the endpoint directly skips the handler, the
popup and the shim.

### ② 취켓팅 — catch cancellations

Draw a 감시 구역 on the seat map and press 감시 시작. Anything that frees inside
it is taken regardless of grade — a cancellation is gone in seconds, and
refusing one because the grade was not on a list is how you watch an empty seat
go to somebody else.

Two channels feed it:

- **The whole-venue trigger.** One request answers "did anything free anywhere?"
  in ~132ms. Sweeping the bitmap to answer the same question costs one request
  per two blocks — `seatStatus` caps at two blockKeys, measured, `n>=3` is HTTP
  400 — so a 34-block venue needs 17 requests and about 4.4s per lap. The
  trigger only ever *adds* a sweep: when it cannot see (the show hides its
  remaining counts, the round is not on sale, the feed is unreachable) the
  rolling sweep carries on unchanged.
- **The page's own traffic.** The 예매 창 fetches availability for its own
  drawing; those responses feed the same diff, cost no request, and so are not
  paced by the gateway budget.

When a bit flips 0→1 that index *is* the freed seat, so it is clicked with no
re-fetch. Most of the remaining latency is travel: reaching a seat outside the
open 구역 means leaving it, opening another and fitting it to the viewport. The
panel reports what each of those actually costs.

There is no separate "take a seat now" button. Taking a seat is what both
functions end in, and 들어가면 곧바로 좌석까지 잡기 makes 오픈 대기 continue
into it.

## How a seat map is understood

Nothing about any venue is hardcoded. Two endpoints describe any show:

| Endpoint | Meaning | Notes |
|---|---|---|
| `/onestop/api/seatMeta` | Static layout | Every seat: grade, floor, row, seat no, price, `seatInfoId`, x/y position |
| `/onestop/api/seatStatus` | Live availability | Hex bitmap, **one bit per seat**, 4 per character, MSB first, same order as seatMeta |

A seat is takeable only when `isExposable` **and** its status bit is set.
`isExposable` alone means "part of the sellable map" — a sold-out show still
reports it for every seat. Maroon 5 reports 960 exposable seats with a single
bit set.

Measured properties:

- `seatStatus` is **148 bytes** for a 538-seat block
- `Cache-Control: no-cache, no-store, must-revalidate`, CloudFront miss — never cached
- 31–72ms round trip, median 50ms
- **Maximum 2 blockKeys per request** — 3 returns HTTP 400

Grades are matched **by name**, never by code: `seatGrade` `"1"` is `VIP석` on a
musical, `EARLY ENTRY PACKAGE` on Maroon 5, and starts at `10` on some KBO games.
Leaving the grade list unselected means "best available in any grade".

Package and table seats share a `seatGroupId` and can only be taken as a whole
set, so they are offered as one atomic unit and used only when the group size
matches your 매수.

### Reading without a login

`seatMeta` and `seatStatus` answer without a session on most shows — 43 of 63
seat-pickable shows return full graded seats and live availability, 16 return an
ungraded skeleton (typically shows that have not opened yet), 4 return nothing.

Login is required to **act**: queue entry (401 otherwise), preselect, and
checkout. It is not required to **read**, so targeting can be prepared before
open and the session only has to execute.

The bitmap is also more accurate than the official remaining-seats API — one
show reported `remainCnt` 0 while 228 seats were genuinely free.

## Booking engines

Measured over the whole NOL catalogue (94 shows across 7 genre pages):

| Engine | Share | Seat sniping |
|---|---|---|
| `onestop-reserved` | 58/94 | Full |
| `onestop-general-admission` | 26/94 | No seat map; quantity only |
| `legacy-poticket` | 9/94 | **Not supported** — separate engine |
| `onestop-sports` | 1/94 | Full, `seatType=SPORTS` |

The show panel names the engine and warns when sniping does not apply.

## Captcha

Uses the official endpoints rather than screen-scraping:

```
POST /onestop/api/captcha/image          -> { Img, EncRnd }
GET  /onestop/api/captcha/verify?p1=answer&p2=sessionId&p3=goodsCode
                                 &p4=memberCode&p5=bizKind&p9=EncRnd
                                         -> { result: "Y" }
```

Answers are always 6 characters and a challenge expires after 5 minutes. The
image is OCR'd by the host process; if the API path is unavailable it falls back
to reading the rendered modal.

There is also an English audio captcha at
`aspseat-ticket.interpark.com/CommonAPI/Captcha/GetCaptchaAudio?v=en&t={EncRnd}`,
which is usually easier to solve than the distorted image.

## Checkout — it never pays

The commit endpoint is `POST /onestop/api/payment/order/{goodsCode}`. The
autopilot never reaches it.

After the seat is locked it fills quantity, birth date, delivery and payment
method, ticks required consent boxes, advances through intermediate 다음/확인
steps, then **stops at the payment button**. Buttons matching 결제하기 / 결제완료 /
입금하기 / 구매하기 / 주문완료 / 결제진행 are explicitly excluded from being
clicked, and that exclusion is asserted in the test suite.

This costs nothing: the seat is already held once preselect succeeds, so only
the path *up to* the lock is a race.

## Files

| File | Role |
|---|---|
| `pureclick.py` | Control panel |
| `browser_host.py` | pywebview + document-start script injection |
| `browser_bridge.py` | Panel ↔ browser state, over a flock-guarded JSON file |
| `browser_session.py` | Cookie jar save/restore, so login survives a restart |
| `pureclick_mac_core.py` | CoreGraphics helpers and the accessibility check |
| `../browser/pureclick_autopilot.js` | Everything that happens inside the page |
| `../core/clock.py` | Server-time sync and the open-time parser |
| `../core/seat.py` | Grade ranking, bitmap decoding, seat grouping, panel text |
| `../core/showinfo.py` | Show lookup, remaining counts, engine classification |
| `../core/zone_map.py` | Projecting the venue for the 범위 정하기 picker |
| `../core/arm.py` | The arm payload shared with the page |
| `../core/watch_trigger.py` | The whole-venue "did anything free?" trigger |

Nothing under `core/` touches tkinter, pywebview or the filesystem. That is the
split: those are the parts testable without launching an app or opening a
booking page, and all of them are tested.

## Research scripts

Each reproduces one measurement quoted above. Run any of them from the repo
root; none needs a login.

| Script | Answers |
|---|---|
| `research/probes/demo_no_login.py` | Real seats and availability with no cookies |
| `research/probes/dump_seatmaps.py` | Layout variety across venue types |
| `research/probes/extract_rsc.py` | Pulls initData out of a captured RSC payload |
| `research/probes/mine_nol_api.py` | Downloads the current bundles and mines their API surface |
| `research/probes/probe_batchlimit.py` | The 2-blockKey request limit |
| `research/probes/probe_bitmap.py` | That seatStatus is a per-seat bitmap |
| `research/probes/probe_bitmap_semantics.py` | That a set bit means "free now" |
| `research/probes/probe_blockkeys.py` | That block keys embed the round |
| `research/probes/probe_clock.py` | Date-header resolution and round-trip spread |
| `research/probes/probe_ordering.py` | That posLeft/posTop track the printed row order |
| `research/probes/probe_remains_vs_bitmap.py` | That the whole-venue count agrees with the bitmap |
| `research/probes/probe_seatmap.py` | seatMeta shape and which fields are populated |
| `research/probes/probe_seatstatus.py` | seatStatus shape and round-trip cost |
| `research/probes/probe_status_cache.py` | That seatStatus is never cached |
| `research/probes/probe_status_detail.py` | What a status response carries beyond the mask |
| `research/probes/survey_shows.py` | Engine and layout spread across many shows |
| `research/probes/validate_clock_estimator.py` | Clock estimator accuracy |

## Tests

```bash
cd ~/Desktop/pureclick
python3 -m pytest tests/ -q               # 103 tests
node tests/test_autopilot_picker.mjs      # 79 tests, drives the real autopilot
```

Every claim above that carries a number has a test or a probe behind it. Where
one does not, it says so.

## Legal

Korea's 공연법 (amended, effective March 2024) makes macro ticket purchasing an
offence when combined with resale — up to 1 year imprisonment or a ₩10M fine.
Automated booking also breaches the site's terms of use.
