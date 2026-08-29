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
   미리보기 / 좌석 잡기 / 취켓팅 only on the seat map).

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

## The three functions

### ① 오픈 대기 — be first in the queue
Set the open time (auto-filled when you load a show) and press 오픈 대기 시작.

At the moment of open it calls the queue API directly:

```
GET /v1/goods/{goodsCode}/waiting?channelCode=pc&preSales=N&playDate=&playSeq=
```

The 예매하기 button is only a client-side gate, so nothing waits for it to render.

Two things make the timing reliable:

- **Clock accuracy ~19ms.** The `Date` header has one-second resolution, so a
  single reading is randomly 0–1000ms wrong. Taking the *maximum* across ~40
  samples recovers the true offset, because every sample is short by the
  fractional second and the largest was taken nearest a real boundary.
  Measured against boundary-bracketing: max-of-40 = 19ms error, single reading =
  50–454ms error, median = 385ms (biased).
- **Burst entry.** It starts 400ms early and retries every ~80ms until the
  server accepts, so a slightly wrong clock or one dropped packet does not cost
  the slot. `NP` (presale auth) and `BL` (blocked) stop immediately.

### ② 좌석 잡기 — take a seat
On the seat map, 좌석 잡기 reads the layout, ranks seats against your conditions,
and calls `bulkPreselectSeats` then `POST /onestop/api/seats/select`.

**미리보기** does everything except send — it prints the exact POST body it would
have sent, so the path can be verified without taking a seat.

### ③ 취켓팅 — catch cancellations
Polls the availability bitmap and diffs it. When a bit flips 0→1 that index *is*
the freed seat, so it preselects immediately with no re-fetch.

Detection lands in roughly 50–250ms.

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
| `browser_host.py` | pywebview + script injection |
| `browser_bridge.py` | Panel ↔ browser state |
| `browser_session.py` | Cookie jar save/restore, so login survives a restart |
| `../pureclick_showinfo.py` | Show lookup + engine classification |
| `../pureclick_catalog.py` | Genre browsing + upcoming-opens scan |
| `../pureclick_seat_core.py` | Grade ranking, bitmap decoding, seat grouping |
| `../browser/pureclick_autopilot.js` | In-page automation |

## Research scripts

Each reproduces one measurement quoted above.

| Script | Answers |
|---|---|
| `research/demo_no_login.py` | Real seats + availability with no cookies |
| `research/coverage_no_login.py` | How many shows expose data without login |
| `research/probe_bitmap.py` | That seatStatus is a per-seat bitmap |
| `research/probe_bitmap_semantics.py` | That a set bit means "free now" |
| `research/probe_batchlimit.py` | The 2-blockKey request limit |
| `research/probe_status_cache.py` | That seatStatus is never cached |
| `research/validate_clock_estimator.py` | Clock estimator accuracy |
| `research/dump_seatmaps.py` | Layout variety across venue types |

## Tests

```bash
cd ~/Desktop/pureclick
python3 -m unittest discover -s tests    # 62 tests
node tests/test_autopilot_picker.mjs     # 17 tests, drives the real autopilot
```

## Legal

Korea's 공연법 (amended, effective March 2024) makes macro ticket purchasing an
offence when combined with resale — up to 1 year imprisonment or a ₩10M fine.
Automated booking also breaches the site's terms of use.
