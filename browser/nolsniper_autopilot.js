(() => {
  "use strict";

  // Bumped whenever seat-grab behaviour changes. Shown in the overlay on load /
  // 좌석 잡기 so you can tell the 예매 창 actually got the new script.
  const AUTOPILOT_BUILD = "trigger-v68";

  // ---------------------------------------------------------------------------
  // Popup shim — runs before anything else, on every frame.
  //
  // NOL's openPCOnestop() (src/util/module/util.js) does not navigate; it opens
  // a named popup and drives it:
  //
  //     win = window.open('', 'BookingPop', 'width=900,...')
  //     form.target = 'BookingPop'; form.submit()          // seat booking
  //   or
  //     window.self.close(); win.location.replace(waitingUrl)   // queue entry
  //
  // WKWebView asks its host to build the popup window and pywebview declines
  // for anything that is not a plain link click, so `win` is null, the POST is
  // aimed at a window that does not exist, and the click does nothing at all.
  // The 예매하기 button looks broken and the seat map is never reached.
  //
  // Rather than open a second window we have no automation in, every popup is
  // folded back into this one: window.open returns a proxy whose navigation
  // moves *this* window, form targets are rewritten to _self, and close() is
  // neutered so the queue path cannot take the app down with it.
  // ---------------------------------------------------------------------------
  const POPUP_SELF_TARGETS = new Set(["", "_self", "_top", "_parent"]);

  // about:blank and data: documents have an opaque origin — no storage, no site
  // to automate. The window starts blank so the saved session can be restored
  // before the first request, so this is hit on every launch.
  if (location.protocol === "about:" || location.protocol === "data:") return;

  function installPopupShim() {
    if (window.__nolsniperPopupShim) return;
    window.__nolsniperPopupShim = true;
    window.__nolsniperPopups = [];

    const record = (entry) => {
      window.__nolsniperPopups.push({ at: Date.now(), ...entry });
      if (window.__nolsniperPopups.length > 20) window.__nolsniperPopups.shift();
      console.log("[NOL Sniper] popup", entry);
    };

    // Hosts the booking flow itself steers to. Anything else that calls
    // window.open is third-party — an ad or analytics pixel — and honouring it
    // navigates the whole 예매 창 off the booking page.
    //
    // Measured live: NOL's page calls window.open("https://www.facebook.com/tr/…")
    // for the Meta pixel, this shim ran location.assign() on it, and the booking
    // page was replaced by a tracking response with an empty body. That is the
    // "it shows at first and then goes blank" report — the window was never
    // broken, it had been navigated away. Confirmed under CDP with
    // `window.top === window.self` true and href facebook.com/tr.
    const BOOKING_HOST = /(^|\.)(yanolja\.com|interpark\.com|naver\.com)$/i;

    const isBookingUrl = (absolute) => {
      let target;
      try {
        target = new URL(absolute);
      } catch (error) {
        return false;
      }
      if (target.origin === location.origin) return true;
      if (BOOKING_HOST.test(target.hostname)) return true;
      // The queue host is learned at run time, not knowable here — see
      // rememberQueueHost. try/catch also covers reading it before its own
      // declaration has run, which returns false rather than throwing.
      try {
        const queue = localStorage.getItem(QUEUE_HOST_KEY) || "";
        if (queue && target.origin === new URL(queue).origin) return true;
      } catch (error) {
        /* opaque origin, blocked storage, or not yet initialised */
      }
      return false;
    };

    const go = (url, { replace = false } = {}) => {
      if (!url) return;
      const absolute = new URL(String(url), location.href).href;
      if (!isBookingUrl(absolute)) {
        // Swallow it. The caller still gets the inert popupProxy back, so a
        // tracker that expects a window object carries on none the wiser.
        record({ blockedThirdParty: absolute });
        return;
      }
      record({ navigate: absolute, replace });
      if (replace) location.replace(absolute);
      else location.assign(absolute);
    };

    function popupProxy(name) {
      const proxy = {
        name: String(name || ""),
        closed: false,
        opener: window,
        focus() {},
        blur() {},
        close() {},
        print() {},
        postMessage() {},
        // Written-in HTML is only ever used to post a form; run it here instead.
        document: {
          write(html) {
            record({ write: String(html).slice(0, 200) });
            submitWrittenForm(String(html));
          },
          writeln(html) {
            this.write(html);
          },
          close() {},
        },
      };
      // `win.location.replace(url)` and `win.location.href = url` are the two
      // ways NOL steers the popup, and both have to land on this window.
      Object.defineProperty(proxy, "location", {
        get() {
          return {
            get href() {
              return location.href;
            },
            set href(value) {
              go(value);
            },
            assign: (value) => go(value),
            replace: (value) => go(value, { replace: true }),
            reload: () => location.reload(),
            toString: () => location.href,
          };
        },
        set(value) {
          go(value);
        },
      });
      return proxy;
    }

    function submitWrittenForm(html) {
      if (typeof DOMParser === "undefined" || !nativeSubmit) return;
      try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const form = doc.querySelector("form");
        if (!form) return;
        const clone = document.importNode(form, true);
        // Same gate as the other three paths: a written-in form is only worth
        // running here if it belongs to the booking flow.
        if (!isBookingUrl(clone.action)) {
          record({ blockedWrittenForm: String(clone.action).slice(0, 200) });
          return;
        }
        clone.target = "_self";
        document.body.appendChild(clone);
        nativeSubmit.call(clone);
      } catch (error) {
        console.log("[NOL Sniper] written form failed", error);
      }
    }

    let nativeSubmit = null;

    if (typeof window.open === "function") {
      window.open = function nolsniperOpen(url, name, features) {
        record({ open: String(url || ""), name: String(name || ""), features: String(features || "") });
        if (url) go(url);
        return popupProxy(name);
      };
    }

    // form.submit() bypasses submit listeners entirely, and that is exactly the
    // call NOL uses for the seat-booking POST, so the prototype has to be patched.
    if (typeof HTMLFormElement !== "undefined") {
      nativeSubmit = HTMLFormElement.prototype.submit;
      HTMLFormElement.prototype.submit = function nolsniperSubmit() {
        // Only retarget the booking flow's own forms. A third-party form aimed
        // at a hidden iframe — which is how analytics pixels post — becomes a
        // full top-frame navigation if it is forced to _self, and the booking
        // page is gone. This is the second half of the blank-page bug: gating
        // go() alone still left the page being navigated away by a pixel form.
        if (!POPUP_SELF_TARGETS.has(this.target || "") && isBookingUrl(this.action)) {
          record({ formTarget: this.target, action: this.action });
          this.target = "_self";
        }
        return nativeSubmit.apply(this, arguments);
      };
    }

    // User-triggered submits and target=_blank links, for completeness. Same
    // scoping as above — retarget the booking flow, leave third parties alone.
    document.addEventListener(
      "submit",
      (event) => {
        const form = event.target;
        if (form && form.target && !POPUP_SELF_TARGETS.has(form.target)
            && isBookingUrl(form.action)) {
          form.target = "_self";
        }
      },
      true,
    );
    document.addEventListener(
      "click",
      (event) => {
        const anchor = event.target?.closest?.("a[target]");
        if (anchor && !POPUP_SELF_TARGETS.has(anchor.target) && isBookingUrl(anchor.href)) {
          anchor.target = "_self";
        }
      },
      true,
    );

    // openPCOnestop's queue path calls window.self.close() *before* steering the
    // popup. Honouring it would close the only window we have.
    try {
      window.close = function nolsniperClose() {
        record({ close: location.href });
      };
    } catch {
      /* frame may be cross-origin */
    }
  }

  installPopupShim();

  // The shim is registered on every frame, because an iframe can open a popup
  // just as well as the page. The autopilot itself is a single-instance thing —
  // one overlay, one seat loop — so it stops here in subframes.
  const isTopFrame = (() => {
    try {
      return window.top === window.self;
    } catch {
      return false; // cross-origin parent: treat as a subframe
    }
  })();
  if (!isTopFrame) return;

  const alreadyLoaded = Boolean(window.NOLSniper);
  // Abort any in-flight run from a previous script copy. Old async loops keep
  // their old selectSeats (with the API fallback) unless we invalidate them.
  window.__nolsniperRunGen = (window.__nolsniperRunGen || 0) + 1;
  if (alreadyLoaded) {
    try {
      window.NOLSniper.stopAll();
    } catch {
      /* ignore */
    }
  }
  if (window.__nolsniperWatchId) {
    clearInterval(window.__nolsniperWatchId);
    window.__nolsniperWatchId = 0;
  }

  function runWasSuperseded(runGen) {
    return runGen !== window.__nolsniperRunGen;
  }

  const SEAT_STORAGE_KEY = "nolsniper_seat_v1";
  const ARM_STORAGE_KEY = "nolsniper_arm_v1";
  const SYNC_URL = "https://poticket.interpark.com/Book/BookMain.asp";
  const TICKETFRONT = "https://api-ticketfront.interpark.com";
  const NOL_ORIGIN = "https://nol.yanolja.com";
  const SSO_ORIGIN = "https://sso.yanolja.com";
  const GATE_ORIGIN = "https://tickets.interpark.com";
  // The queue host the live gate actually posts to. Measured 2026-09-04:
  // api-ticketfront's /v1/goods/{code}/waiting answers 401 "자동 로그아웃되었습니다"
  // for every show under an SSO login, so it is not a route that can succeed.
  const ENT_WAITING_ORIGIN = "https://ent-waiting-api.interpark.com";
  const MEMBER_INFO_PATH = "/api/ticket/v2/reserve-gate/member-info";
  const SECURE_URL_PATH = "/waiting/api/secure-url";
  // What the waiting room itself does with the key from secure-url, read out
  // of its bundle (tickets.interpark.com/waiting, 2026-09-05): POST line-up
  // {key} assigns the place in line (userSeq / waitingId), GET rank?waitingId=
  // answers {myRank, totalRank, oneStopUrl,…} and the page navigates to
  // oneStopUrl the moment it appears. The page polls rank every 2–3s and only
  // starts after its own ~1.3MB of JS has booted — dead time that decides the
  // position, so the sniper makes both calls itself from the goods page (CORS
  // allows tickets.interpark.com with credentials for all four endpoints).
  const LINE_UP_PATH = "/waiting/api/line-up";
  const RANK_PATH = "/waiting/api/rank";
  // Measured 2026-09-05 (tools/probe_entry_chain.py, warm connections):
  // member-info 60ms, secure-url 9ms, line-up 14ms, rank 12–17ms. On an empty
  // queue rank reports myRank 1 / totalRank 1 with no session for ~1.7s after
  // line-up, then totalRank 0 + sessionId + oneStopUrl in one answer — the
  // booking session is created server-side after the line-up, and nothing on
  // this side shortens that. The page polls every 2–3s; 150ms notices the URL
  // within one RTT of it appearing.
  const RANK_POLL_MS = 150;
  const RANK_POLL_WINDOW_MS = 15 * 60 * 1000;
  // myRank 0 with no sessionId is the page's "expired existing session" — but
  // the session takes ~1.7s to exist at all, so that reading is only trusted
  // once the line-up is older than this.
  const RANK_SESSION_GRACE_MS = 4000;
  // line-up is NOT idempotent (measured: the same key lined up twice took two
  // userSeq). A fallback to the waiting page after a successful line-up
  // therefore costs the place in line — it is the last resort, never a retry.
  // The signature member-info mints is stamped with its issue time and is
  // minted once, before the open, so the burst spends one round trip per shot
  // instead of two (member-info is the slowest call in the chain at ~60ms).
  // Re-minted when older than this or refused. Measured 2026-09-05: the same
  // signature was still accepted by secure-url at 121 s, 301 s and 601 s of
  // age, so ten seconds before the burst is nowhere near its limit and keeps
  // a slow or failed member-info (60 ms warm, retried) off the decisive path.
  const SIGNATURE_MAX_AGE_MS = 300000;
  const PREMINT_LEAD_MS = 10000;
  // Burst cadence around the open. secure-url answers in ~9ms warm and no
  // throttle has ever been observed on ent-waiting-api, so 20ms: at most
  // ~75 shots across the 1.5s decisive window, under the attempt cap.
  const SECURE_URL_BURST_MS = 20;
  const NOT_OPEN_ERROR = "UnableReservationTime";
  const BLOCKED_ERROR = "AccessDenied_Blacklist";
  const DEFAULT_SEAT_CONFIG = {
    enabled: true,
    // Empty = follow the show's own grade order. See rankGrade().
    grade_order: [],
    grade_strict: false,
    allow_group_seats: true,
    auto_assign: false,
    block_keys: [],
    block_names: [],
    // Venue coords from the zone picker drag. null = whole map.
    watch_rect: null,
    max_attempts: 80,
    retry_ms: 20,
    poll_ms: 40,
    speed_ms: 100,
    quantity: 1,
    birth_yymmdd: "",
    delivery: "배송",
    payment: "무통장입금",
    discord_webhook: "",
    // Which seat to aim for within a grade tier. See SEAT_STRATEGIES.
    seat_strategy: "center",
    // 취켓팅 takes one seat and stops, so an unwanted grade ends the watch.
    catch_grade_strict: true,
    reentry: false,
    adjacent: true,
    auto_seats_after_entry: false,
    // Only carried so the panel's 진입 보정 survives a restart. The fire reads
    // the correction off the arm payload, never from here.
    entry_offset_ms: 0,
  };

  const SEAT_META_CONCURRENCY = 6;
  // One seatStatus call covers two blocks. Holding requests-per-second steady
  // is what keeps the gateway quiet, so this and the interval below are the
  // budget; how long a sweep takes follows from how many blocks are watched.
  // How far ahead of the open to start asking for a queue slot. The request
  // loop is built to retry across the boundary, so being early is the point:
  // the first request the server is willing to accept is then ours.
  const ENTRY_LEAD_MS = 400;

  /**
   * Pressing the page's own 예매하기, which is a different race from the API.
   *
   * NOL renders that button disabled until the show opens and enables it from
   * its own client-side code, some unpredictable moment after the published
   * time — the server does not flip at exactly 정각, and the page needs a beat
   * after that to notice. This used to be a single click at exactly T against a
   * finder that skipped disabled nodes, so the usual outcome was finding
   * nothing at all and reporting 예매하기 버튼을 찾지 못했습니다.
   *
   * So: start watching early, look often, and click the instant it goes live.
   */
  const ENTRY_CLICK_LEAD_MS = 400;
  const ENTRY_CLICK_POLL_MS = 15;
  // How long past the target to keep watching. Long enough for a slow open,
  // short enough that a genuinely broken run says so rather than hanging.
  const ENTRY_CLICK_WINDOW_MS = 8000;
  // If it is still disabled this long after the open, stop believing the
  // attribute and strip it. A last resort, and recorded as one: the button
  // being enabled is NOL's own signal that the booking route is wired up, so
  // forcing past it can click something that does nothing.
  const ENTRY_FORCE_AFTER_MS = 1500;
  // The modal that follows the first click. A fixed sleep here was a guess in
  // both directions — too long on a fast page, too short on a slow one.
  const ENTRY_MODAL_WAIT_MS = 1200;

  // Measured: seatStatus answers in 29ms for two blocks, so a 200ms floor left
  // 87% of every tick asleep. The site's own page bursts at roughly 460
  // requests a second when it opens a 구역; this sustains about eight.
  const CATCH_MIN_POLL_MS = 100;
  const CATCH_MAX_REQUESTS_PER_TICK = 1;
  // What to spend when there is no burst to save for.
  //
  // The one-request budget is an *average*: it stays cheap while the venue is
  // quiet so a trigger can spend the whole sweep at once the moment something
  // frees. That bargain needs a trigger. A show that does not publish its
  // remaining-seat count has none — watch_trigger reports usable:false and
  // triggerBursts stays 0 forever — so the saving is never spent and the watch
  // just runs at the slowest rate it has, permanently.
  //
  // Measured on 26006903: 75 blocks, 2 per request, 1 request per 154ms tick =
  // a 5.9 second lap. A seat freeing behind the cursor is three seconds old
  // before we look, which is the whole race.
  //
  // So when there is nothing to save for, spend. This is still an order of
  // magnitude under the 460 requests a second the site's own page does when it
  // opens a 구역, and the gateway backoff below is unchanged.
  const CATCH_UNTRIGGERED_REQUESTS_PER_TICK = 4;
  // Stop spending once a lap is this quick — past here the page's own render
  // lag (domAgreedMs, ~1s on a busy map) dominates and more requests buy
  // nothing but exposure.
  const CATCH_TARGET_LAP_MS = 1200;
  // The floor is a rate ceiling, not a fact about the network.
  //
  // CATCH_MIN_POLL_MS holds requests-per-second down, and on a venue big enough
  // that one tick reads only part of it that is exactly what it does: the sweep
  // spans several ticks and every one of them is spent working.
  //
  // On a small venue it does something else. Measured live on 26007416
  // (겨울왕국, 8 blocks = 4 requests): the whole venue comes back in 50ms median
  // (30-96 over 33 laps) and the untriggered budget covers all four requests in
  // one tick — so half of every tick is spent asleep in front of a map we have
  // already finished reading, and a seat freeing just after a sweep is a full
  // 100ms stale before anything looks at it again. That staleness is
  // self-inflicted: it buys no request budget, because the sweep is already
  // whole.
  //
  // So when a tick covers the entire watched venue, shorten the wait toward
  // what the sweep actually costs. Requests *per sweep* are unchanged — what
  // goes away is the dead air — but requests per second do rise, so this is
  // bounded twice: never below CATCH_FAST_POLL_MS, and never fast enough for
  // the measured sweep to exceed CATCH_MAX_REQUESTS_PER_SEC. On the venue
  // above that lands at 67ms rather than 100ms, at 60 requests a second
  // against the ~460 the site's own page does opening a 구역.
  //
  // 20, not 30: the request-rate ceiling below is what actually bounds the
  // gateway exposure, and a 30ms floor sat on top of a ~20-29ms seatStatus
  // round trip, so a seat freeing just after a reading was up to 30ms stale
  // before the next one. The floor now matches the RTT; the 60/s cap and the
  // MessageChannel yield (never a timer) are unchanged.
  const CATCH_FAST_POLL_MS = 20;
  // Hyper-focus: once a block is open on screen, 취켓팅 reads only that block,
  // this often, and never leaves it. Measured 17:00: sequential sweeps across
  // 10+ blocks (~1s a lap) lost every cancellation to bots parked on one block.
  const CATCH_FOCUS_POLL_MS = 20;
  const CATCH_MAX_REQUESTS_PER_SEC = 60;
  const CATCH_LIVE_TRIES = 8;
  // 좌석 잡기 gives up after this many refusals in a row. 취켓팅 keeps watching,
  // because there the whole point is to wait for the map to change.
  const REJECT_STREAK_LIMIT = 12;
  const BFF_LANGUAGE = { ko: "KO", en: "EN", ja: "JA", zh: "ZH" };

  const seatState = {
    running: false,
    locked: false,
    // Prevents concurrent advanceAfterSeatLock (bootRoute + success path)
    // from both clicking 선택 완료 after the intentional post-hold delay.
    confirmStarted: false,
    attempts: 0,
    lastError: "",
    lastSeat: "",
    // Set by 정지, cleared only by a button press in the panel.
    haltedByUser: false,
    // Aiming strategy support: seed is per run, centre is per venue.
    shuffleSeed: 0,
    mapCenterX: null,
    mapStage: null,
    lastOrder: [],
    lastStagePoint: null,
    // Seats preselected but not yet committed. Anything left here is counting
    // against the account's ticket allowance and has to be handed back.
    heldSeatIds: new Set(),
    // seatInfoId -> monotonic ms (nowMs) when it may be tried again. A seat
    // someone else just took is not gone for good: holds expire and carts are
    // abandoned, so it rejoins the pool rather than being blacklisted.
    takenUntil: new Map(),
    unreachableUntil: new Map(),
    // Monotonic ms until a gateway block lifts, and which call earned it. Declared
    // rather than sprung into being, because every loop now reads them.
    blockedUntil: 0,
    blockedEndpoint: "",
    // Seats the page's own traffic showed opening, waiting for the loop.
    pageFreed: [],
    // What travelling to a seat actually costs, by kind of move.
    mapMoves: {},
    // detect -> click -> cart -> 선택 완료, in ms, for the last few catches.
    // Survives a run: two 취켓팅 sittings on the same show are the comparison
    // that matters, and clearing it per run would throw that away.
    catchTiming: null,
    catchTimings: [],
    // The 구역 the watch is standing in, and when that was last verified.
    parkedBlock: "",
    parkedCheckedAt: 0,
    parkFailures: 0,
    reparks: 0,
    // The panel's whole-venue "did anything free?" verdict.
    watchTrigger: null,
    triggerActedAt: 0,
    syncedSummary: null,
    lastProbe: null,
    lastBlocks: null,
    // Whether lastBlocks covers the whole venue, or is whatever an early-
    // stopped 좌석 잡기 happened to fetch. The watch reads it as the venue.
    lastBlocksComplete: true,
    batchFailures: 0,
    awaitingPayment: false,
    stopRequested: false,
    lastStatusStamp: "",
    showCatalog: null,
    message: "",
    catchCursor: 0,
    // Blocks this run has already read once — see applyBlockMask.
    runBaseline: null,
    discoveredBlocks: null,
    domCircleCount: 0,
    // sessionId/channel/lang derived from readInterparkContext() — a real
    // localStorage/DOM scan, not a free lookup. These do not change within a
    // round, so onestopHeaders() memoizes them here instead of re-scanning on
    // every request; adoptBlocksKey() clears this alongside the other per-
    // round caches when the round actually changes.
    headerContextCache: null,
  };

  // Survives reload_autopilot the same way the trace does. 감시 시작 used to
  // re-inject the script, which emptied the grape-map sketch the panel copies.
  // Bump when the shape or the contents of a parked sketch change. The cache
  // outlives a reload_autopilot — it hangs off window, which is the point — so
  // without a version a fixed builder keeps serving the old build's output and
  // the fix looks like it did nothing. That cost a full round of debugging.
  const SKETCH_CACHE_VERSION = 5;
  const parkedSketch = (window.__nolsniperZoneSketch = window.__nolsniperZoneSketch || {
    points: [],
    key: null,
    v: SKETCH_CACHE_VERSION,
  });
  if (parkedSketch.v !== SKETCH_CACHE_VERSION) {
    parkedSketch.points = [];
    parkedSketch.key = null;
    parkedSketch.v = SKETCH_CACHE_VERSION;
  }

  const armState = {
    running: false,
    fired: false,
    lastError: "",
    waitingUrl: "",
    // Every /waiting attempt with its offset from the target — see acquireWaitingUrl.
    waitingLog: [],
    queueHost: "",
    reentryTries: 0,
    // On armState rather than in the closure so a stuck re-entry is visible in
    // the status, and so the latch can be tested apart from the spacing floor —
    // an attempt can easily outlast the floor, and then the latch is the only
    // thing preventing overlap.
    reentryInFlight: false,
    reentryAt: 0,
    // What the last entry actually did, so a rehearsal has something to show.
    // fireEntry already measured the lateness and then discarded it.
    latenessMs: null,
    firedAtServer: 0,
    syncMs: 0,
    enterMs: 0,
    clockQuality: "",
    clockOffsetMs: 0,
    // Non-zero when the device clock moved out from under a waiting arm. The
    // target does not move with it any more — see clockState — but the panel
    // still has to be able to say so, because "the countdown looks wrong" is
    // otherwise indistinguishable from a bad sync.
    clockJumpMs: 0,
    enteredVia: "",
    // Which of the four routes the entry actually took, unabbreviated.
    // `enteredVia` collapsed a BookSession POST and a DOM click into one
    // value ("book"), which is exactly the distinction you need when asking
    // "what did it actually press?".
    route: "",
    // Every look at the 예매하기 button, by state, offset from the target.
    clickLog: [],
    clickTries: 0,
    clickLatenessMs: null,
    // The ms correction that was in force, echoed back so the panel can show
    // the fire it asked for beside the fire it got.
    entryOffsetMs: 0,
    goodsCode: "",
    playSeq: "",
  };

  /**
   * The moment this arm actually fires: 티켓 오픈 plus the user's ms correction.
   *
   * `target_server_unix` stays 티켓 오픈 itself so the panel can show both, and
   * every route reads the fire moment through here instead — otherwise a
   * correction applied in one place and not another is a bug that only shows up
   * on the one day it cannot be fixed.
   */
  function armTargetUnix(arm) {
    const target = Number(arm?.target_server_unix);
    if (!Number.isFinite(target)) return NaN;
    const offset = Number(arm?.entry_offset_ms);
    return target + (Number.isFinite(offset) ? offset : 0) / 1000;
  }

  const clockState = {
    offsetSeconds: 0,
    syncedAt: 0,
    quality: "none",
    samples: 0,
    spreadSeconds: 0,
    syncMs: 0,
    note: "",
    // A monotonic anchor, so the fire cannot be moved by the device clock.
    //
    // This used to be `Date.now() + offset` and nothing else, which meant the
    // moment we fire at was pinned to the wall clock. Anyone who shifts the
    // system clock forward to make a not-yet-open show's 예매하기 button
    // appear — the standard folk remedy — moved the target with it, silently,
    // while the panel beside it kept counting to the real one. performance.now()
    // is monotonic and unaffected by a clock change, an NTP step or a DST
    // transition, so the target stays where it was measured.
    anchorPerf: 0,
    anchorServer: 0,
    // The wall clock at the same instant as anchorPerf. Kept only so a jump can
    // be *detected* and reported; it is never used to compute the time.
    anchorWall: 0,
  };

  function log(...args) {
    console.log("[NOL Sniper]", ...args);
  }

  // The monotonic millisecond clock. Every "not before this moment" deadline in
  // this file is measured against it rather than Date.now(), so moving the
  // device clock cannot leave a cooldown stranded hours in the future — which
  // is how one shifted clock used to freeze 취켓팅 with 접속 차단 중.
  function nowMs() {
    return performance.now();
  }

  // How far the wall clock has run away from the monotonic one since the last
  // sync. Sleep and a manual clock change both show up here; an NTP slew is
  // well under a second, so the threshold separates them cleanly.
  const CLOCK_JUMP_TOLERANCE_S = 2.0;

  function clockJumpSeconds() {
    if (!clockState.anchorPerf || !clockState.anchorWall) return 0;
    const monotonicElapsed = (performance.now() - clockState.anchorPerf) / 1000;
    const wallElapsed = Date.now() / 1000 - clockState.anchorWall;
    return wallElapsed - monotonicElapsed;
  }

  function clockJumped() {
    return Math.abs(clockJumpSeconds()) > CLOCK_JUMP_TOLERANCE_S;
  }

  // Re-anchor after a sync. Called from exactly one place on purpose: three
  // separate branches in syncServerClock set the offset, and an anchor that
  // only some of them refreshed would be worse than none.
  function anchorClock(offsetSeconds) {
    clockState.offsetSeconds = offsetSeconds;
    clockState.anchorPerf = performance.now();
    clockState.anchorWall = Date.now() / 1000;
    clockState.anchorServer = clockState.anchorWall + offsetSeconds;
    clockState.syncedAt = Date.now();
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function loadSeatConfig() {
    try {
      const raw = localStorage.getItem(SEAT_STORAGE_KEY);
      if (!raw) return { ...DEFAULT_SEAT_CONFIG };
      return { ...DEFAULT_SEAT_CONFIG, ...JSON.parse(raw) };
    } catch {
      return { ...DEFAULT_SEAT_CONFIG };
    }
  }

  function saveSeatConfig(config) {
    // A sandboxed iframe has an opaque origin and throws on localStorage.
    try {
      localStorage.setItem(SEAT_STORAGE_KEY, JSON.stringify(config, null, 2));
    } catch {
      /* nothing to persist to here */
    }
  }

  function loadArmConfig() {
    try {
      const raw = localStorage.getItem(ARM_STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function saveArmConfig(config) {
    try {
      localStorage.setItem(ARM_STORAGE_KEY, JSON.stringify(config, null, 2));
    } catch {
      /* nothing to persist to here */
    }
  }

  function parseHttpDate(value) {
    return new Date(value).getTime() / 1000;
  }

  function serverTimeUnix() {
    // Before the first sync there is no anchor, and the wall clock is all we
    // have. Afterwards the anchor is the only reading — see clockState.
    if (!clockState.anchorPerf) return Date.now() / 1000 + clockState.offsetSeconds;
    return clockState.anchorServer + (performance.now() - clockState.anchorPerf) / 1000;
  }

  async function sampleServerOffset() {
    const startUnix = Date.now() / 1000;
    const startPerf = performance.now();
    const response = await fetch(SYNC_URL, { method: "HEAD", cache: "no-store" });
    const rawDate = response.headers.get("Date");
    if (!rawDate) throw new Error("missing Date header");
    const rtt = (performance.now() - startPerf) / 1000;
    // The Date header is truncated to whole seconds, so a single reading is
    // uniformly 0..1s early: measured = trueOffset - frac(localMidnightPhase).
    return parseHttpDate(rawDate) - (startUnix + rtt / 2);
  }

  // Taking the maximum over many samples recovers the true offset: every
  // measurement understates it by the fractional part of the current second, so
  // the largest sample is the one taken closest to a server second boundary.
  // ~40 samples spread across a second land within roughly 25ms, versus the
  // 0-1000ms error of a single reading.
  async function syncServerClock(fallbackOffset = 0, { samples = 40, budgetMs = 2500 } = {}) {
    const startedPerf = performance.now();
    const deadline = startedPerf + budgetMs;
    const observed = [];
    let consecutiveFailures = 0;
    while (observed.length < samples && performance.now() < deadline) {
      try {
        observed.push(await sampleServerOffset());
        consecutiveFailures = 0;
      } catch (error) {
        consecutiveFailures += 1;
        // Give up early rather than burning the whole budget on requests that
        // cannot succeed.
        //
        // The Date header is not on the CORS safelist and none of these hosts
        // send Access-Control-Expose-Headers, so an in-page read of it always
        // fails — measured: quality "none", 0 samples, every time. This loop
        // used to keep retrying until the 2500ms deadline, so every single arm
        // paid 2.5 seconds of dead time before it could even start waiting.
        // The host's own sync has no such restriction and is what we fall back
        // to, so there is nothing to gain by insisting.
        if (consecutiveFailures >= 3) {
          anchorClock(fallbackOffset);
          clockState.quality = "host";
          clockState.samples = 0;
          clockState.spreadSeconds = 0;
          clockState.syncMs = performance.now() - startedPerf;
          clockState.note = "브라우저에서 Date 헤더를 읽을 수 없어 조작판 보정을 사용합니다";
          log("clock: in-page sync unavailable, using host offset", fallbackOffset, error);
          return fallbackOffset;
        }
      }
    }
    if (!observed.length) {
      anchorClock(fallbackOffset);
      clockState.quality = "fallback";
      return fallbackOffset;
    }
    const best = Math.max(...observed);
    const spread = best - Math.min(...observed);
    // A spread well under a second means the samples never straddled a boundary,
    // so `best` may still understate. The host's own sync is better in that case.
    if (spread < 0.6 && Number.isFinite(fallbackOffset) && fallbackOffset !== 0) {
      anchorClock(fallbackOffset);
      clockState.quality = "host";
    } else {
      anchorClock(best);
      clockState.quality = "boundary";
    }
    clockState.samples = observed.length;
    clockState.spreadSeconds = spread;
    log(`clock ${clockState.quality} offset=${clockState.offsetSeconds.toFixed(3)}s n=${observed.length} spread=${spread.toFixed(3)}s`);
    return clockState.offsetSeconds;
  }

  // Timer-free yield: WKWebView clamps setTimeout hard in a non-frontmost
  // window; a MessageChannel hop is not clamped, so short waits stay short.
  const fastChannel = typeof MessageChannel === "function" ? new MessageChannel() : null;
  function yieldFast() {
    if (!fastChannel) return Promise.resolve();
    return new Promise((resolve) => { fastChannel.port1.onmessage = () => resolve(); fastChannel.port2.postMessage(0); });
  }
  // Resolve the moment the page's own seat request answers — from inside the
  // network callback, not from a poll. `since` guards against an older answer.
  const seatNetWaiters = [];
  function waitForSeatNet(kind, since, timeoutMs = 2000) {
    return new Promise((resolve) => {
      const check = () => {
        const net = window.__nolsniperLastSeatNet || {};
        const at = kind === "preselect" ? net.preselectAt : net.selectAt;
        if ((at || 0) >= since) return { ok: kind === "preselect" ? net.preselectOk !== false : net.selectOk !== false, at, net };
        return null;
      };
      const now = check(); if (now) return resolve(now);
      const waiter = { check, resolve, deadline: Date.now() + timeoutMs };
      seatNetWaiters.push(waiter);
      // Fallback so a request that never answers cannot hang the sequence.
      sleep(timeoutMs).then(() => { const i = seatNetWaiters.indexOf(waiter); if (i >= 0) { seatNetWaiters.splice(i, 1); resolve({ ok: null, timeout: true }); } });
    });
  }
  function resolveSeatNetWaiters() {
    for (let i = seatNetWaiters.length - 1; i >= 0; i -= 1) {
      const w = seatNetWaiters[i]; const hit = w.check();
      if (hit) { seatNetWaiters.splice(i, 1); w.resolve(hit); }
    }
  }
  // A stop must not wait out a 2.5s network timeout: wake every waiter now,
  // marked, so the press sequence holding it can exit on the spot.
  function abortSeatNetWaiters() {
    const waiting = seatNetWaiters.splice(0);
    for (const w of waiting) w.resolve({ ok: null, aborted: true });
    return waiting.length;
  }
  async function waitUntilServerUnix(targetUnix, { cancelled = null } = {}) {
    let lastCheck = 0;
    while (serverTimeUnix() < targetUnix) {
      const remainingMs = (targetUnix - serverTimeUnix()) * 1000;
      if (remainingMs <= 4) {
        while (serverTimeUnix() < targetUnix) {
          /* spin */
        }
        return;
      }
      // 대기 중지 removes the arm from storage; the wait has to notice, or the
      // panel says 취소됨 while this still fires — measured: armed stayed
      // true 10s after the stop.
      if (cancelled && remainingMs > 250 && Date.now() - lastCheck > 150) {
        lastCheck = Date.now();
        if (cancelled()) {
          const error = new Error("대기 취소됨");
          error.cancelled = true;
          throw error;
        }
      }
      await sleep(Math.min(20, remainingMs - 4));
    }
  }

  // The same state machine as core/mode.py, in the same order. The overlay
  // header, the panel banner and the live band all read this, so they agree.
  const MODE_LABELS = {
    held: "좌석 잡음", grabbing: "좌석 잡는 중", watching: "취켓팅 중", armed: "오픈 대기 중",
    entering: "진입 중", on_schedule: "회차 맞추는 중", halted: "중지됨", error: "문제 발생",
    on_seat: "좌석맵", ready: "준비됨", no_show: "공연 없음",
  };
  function currentMode() {
    try {
      if (seatState.locked) {
        const held = typeof seatState.pageSelected === "number" ? seatState.pageSelected : -1;
        if (held !== 0) return "held";
      }
      if (seatState.running) return seatState.runMode === "catch" ? "watching" : "grabbing";
      if (armState.running && !armState.fired) return "armed";
      if (isWaitingPage() || isGatesPage() || (armState.fired && (isNolProductPage() || isGoodsPage()))) return "entering";
      if (/\/onestop\/schedule/.test(location.pathname)) return "on_schedule";
      if (/\/onestop/.test(location.pathname) && !isSeatPage()) return "entering";
      if (seatState.haltedByUser) return "halted";
      if (String(seatState.lastError || "").trim() || String(armState.lastError || "").trim()) return "error";
      if (isSeatPage()) return "on_seat";
      if (isNolProductPage() || isGoodsPage()) return "ready";
    } catch (error) { /* a mode must never throw */ }
    return "no_show";
  }
  let lastOverlayMode = "";
  let lastOverlayMessage = "";
  function overlayHeader() {
    const mode = currentMode();
    lastOverlayMode = mode;
    return `스나이퍼 · ${MODE_LABELS[mode] || mode}`;
  }
  // Keep the header truthful between messages: the mode can change (a stop,
  // a lock) without anyone calling updateOverlay.
  setInterval(() => {
    const root = document.getElementById("nolsniper-overlay");
    if (!root || currentMode() === lastOverlayMode) return;
    const head = root.querySelector("strong");
    if (head) head.textContent = overlayHeader();
  }, 500);

  // Skip a repaint only when the overlay is *showing* this line already. The
  // key is recorded by updateOverlay itself — every write, not only the
  // de-duplicated ones — so a stop message in between ("정지했습니다.") never
  // leaves a stale key that swallows the next run's first identical line.
  function updateOverlayIfChanged(message, tone = "info") {
    if (seatState.lastOverlayKey === `${tone}|${message}`) return;
    updateOverlay(message, tone);
  }
  function updateOverlay(message, tone = "info") {
    seatState.lastOverlayKey = `${tone}|${message}`;
    seatState.message = String(message || "").replace(/<br\s*\/?>/gi, " · ");
    lastOverlayMessage = message;
    let root = document.getElementById("nolsniper-overlay");
    if (!root) {
      root = document.createElement("div");
      root.id = "nolsniper-overlay";
      root.style.cssText = [
        "position:fixed",
        "right:16px",
        "bottom:16px",
        "z-index:2147483647",
        "background:#0b1220",
        "color:#fff",
        "padding:12px 14px",
        "border-radius:12px",
        "font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
        "box-shadow:0 10px 28px rgba(0,0,0,.35)",
        "border:1px solid #243044",
        "max-width:380px",
      ].join(";");
      document.body.appendChild(root);
    }
    const colors = { info: "#7dd3fc", ok: "#86efac", warn: "#fbbf24", error: "#fca5a5" };
    root.innerHTML = `<strong style="color:${colors[tone] || colors.info}">${overlayHeader()}</strong><br>${message}`;
  }

  // The booking session lives in sessionStorage under "interpark/context" — but
  // only when the SPA has written it under that exact name. Measured on a live
  // seat map with the countdown still running: __NEXT_DATA__ carried no initData
  // and this key was absent, so getInitData() returned null and the whole run
  // aborted silently. Rather than depend on one key, take any stored object that
  // looks like a booking session.
  const CONTEXT_KEYS = ["interpark/context", "onestop/context", "interpark_context"];

  function readInterparkContext() {
    for (const key of CONTEXT_KEYS) {
      const parsed = parseStored(key);
      if (looksLikeBookingContext(parsed)) return parsed;
    }
    return scanForBookingContext();
  }

  function parseStored(key) {
    for (const store of [sessionStorage, localStorage]) {
      try {
        const raw = store.getItem(key);
        if (raw) return JSON.parse(raw);
      } catch {
        /* absent, unreadable, or not JSON */
      }
    }
    return null;
  }

  function looksLikeBookingContext(value) {
    return Boolean(value && typeof value === "object" && value.sessionId && value.goods);
  }

  function scanForBookingContext() {
    for (const store of [sessionStorage, localStorage]) {
      let length = 0;
      try {
        length = store.length;
      } catch {
        continue;
      }
      for (let index = 0; index < length; index += 1) {
        try {
          const key = store.key(index);
          const raw = store.getItem(key);
          if (!raw || raw.length > 200000 || !raw.includes("sessionId")) continue;
          const parsed = JSON.parse(raw);
          if (looksLikeBookingContext(parsed)) return parsed;
          // Some builds nest it one level down.
          for (const nested of Object.values(parsed || {})) {
            if (looksLikeBookingContext(nested)) return nested;
          }
        } catch {
          /* not JSON, or not ours */
        }
      }
    }
    return null;
  }

  // The 회차 the page is actually displaying.
  //
  // initData is captured when the page loads and is never refreshed, so
  // changing 회차 in-place leaves __NEXT_DATA__ holding the old round. Every
  // API call then asks about a round nobody is looking at. Measured live:
  // initData said playSeq 017 and we polled blocks 017:001/017:002 while the
  // seats on screen carried blockKey 022:001 with 40 of them free — the server
  // answered all-zero masks for round 017 and the macro called a selling show
  // sold out.
  //
  // Block keys are `${playSeq}:${block}`, so the seats themselves say which
  // round is on screen, and they cannot be stale.
  //
  // Bounded and cached. This used to walk EVERY circle through React fiber on
  // every call — and it is called from every seatStatus/seatMeta request and
  // twice per 400ms host snapshot. On a 3,022-seat map that is thousands of
  // 16-hop fiber walks several times a second on the thread the page paints
  // from: the host's evaluate_js queued behind it and the panel read
  // "예매 창 응답 없음". Three agreeing circles are all the evidence needed, so
  // stop there, never look at more than a dozen, and remember the answer until
  // the map re-mounts (the seat-index MutationObserver invalidates) or the
  // circle count changes.
  const LIVE_PLAY_SEQ_SAMPLE = 12;
  const LIVE_PLAY_SEQ_TTL_MS = 500;
  const livePlaySeq = { value: null, at: 0, circles: -1 };
  function invalidateLivePlaySeq() {
    livePlaySeq.at = 0;
  }
  function currentPlaySeqFromDom() {
    let nodes = document.querySelectorAll("circle.js-seat");
    const count = nodes.length;
    const now = performance.now();
    if (livePlaySeq.at && livePlaySeq.circles === count && now - livePlaySeq.at < LIVE_PLAY_SEQ_TTL_MS) {
      return livePlaySeq.value;
    }
    if (count < 3) nodes = collectSeatCircles();
    const tally = new Map();
    let best = null;
    let bestCount = 0;
    let inspected = 0;
    for (const node of nodes) {
      if (inspected >= LIVE_PLAY_SEQ_SAMPLE || bestCount >= 3) break;
      inspected += 1;
      const key = seatFromFiber(node)?.blockKey;
      if (!key) continue;
      const seq = String(key).split(":")[0];
      if (!seq) continue;
      const n = (tally.get(seq) || 0) + 1;
      tally.set(seq, n);
      if (n > bestCount) {
        best = seq;
        bestCount = n;
      }
    }
    // A handful of circles is not evidence; a drawn 구역 is.
    livePlaySeq.value = bestCount >= 3 ? best : null;
    livePlaySeq.circles = count;
    livePlaySeq.at = now;
    return livePlaySeq.value;
  }

  function withLivePlaySeq(initData) {
    if (!initData) return initData;
    const fromDom = currentPlaySeqFromDom();
    if (!fromDom) return initData;
    const stated = String(initData.playSeq?.playSeq || initData.playSeq || "");
    if (stated === fromDom) return initData;
    traceCall("playSeqCorrected", null, { stated, fromDom });
    return {
      ...initData,
      playSeq: typeof initData.playSeq === "object" && initData.playSeq
        ? { ...initData.playSeq, playSeq: fromDom }
        : fromDom,
    };
  }

  function getInitData() {
    const page = window.__NEXT_DATA__?.props?.pageProps?.initData || null;
    const ctx = readInterparkContext();
    if (!page && !ctx) return null;
    if (!ctx) return page;
    if (!page) {
      return {
        sessionId: ctx.sessionId,
        bizCode: ctx.bizCode,
        channelType: ctx.channelType,
        goods: ctx.goods,
        playSeq: ctx.playSeq,
        entMemberCode: ctx.entMemberCode,
      };
    }
    return {
      ...page,
      sessionId: page.sessionId || ctx.sessionId,
      bizCode: page.bizCode || ctx.bizCode,
      channelType: page.channelType || ctx.channelType,
      goods: page.goods || ctx.goods,
      playSeq: page.playSeq || ctx.playSeq,
      entMemberCode: page.entMemberCode || ctx.entMemberCode,
    };
  }

  function randomTraceId(length = 16) {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let value = "";
    for (let index = 0; index < length; index += 1) {
      value += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
    }
    return value;
  }

  function onestopHeaders(initData = null) {
    const data = initData || getInitData();
    // readInterparkContext() is a real localStorage/DOM scan, and this runs on
    // every seatStatus/seatMeta/gql call the catch loop makes — several times
    // a second while a watch is running. None of what it derives here changes
    // within a round, so a genuine hit is memoized on seatState and
    // invalidated by adoptBlocksKey() exactly when discoveredBlocks/showCatalog
    // already are. A miss (null — the context has not landed in storage yet)
    // is deliberately NOT cached: caching {} here would have been sticky, so
    // one early call before the context exists would have kept every later
    // call reading an empty object for the rest of the run, silently dropping
    // X-Onestop-Session/Channel from every request after it.
    // (The scan's own cost is small next to the network round trip it feeds
    // into — this is worth doing because it is free once warm, not because it
    // moves the numbers.)
    const found = seatState.headerContextCache || readInterparkContext();
    if (found) seatState.headerContextCache = found;
    const ctx = found || {};
    const headers = { Accept: "application/json" };
    const sessionId = data?.sessionId || ctx.sessionId;
    const channel = data?.channelType || ctx.channelType || "ONESTOP";
    const lang = BFF_LANGUAGE[ctx.lang || data?.lang || "ko"] || "KO";
    if (sessionId) headers["X-Onestop-Session"] = sessionId;
    if (channel) headers["X-Onestop-Channel"] = channel;
    headers["X-Ticket-BFF-Language"] = lang;
    headers["X-OneStop-Trace-ID"] = randomTraceId();
    headers["X-Requested-With"] = "XMLHttpRequest";
    const preOpt = data?.goods?.preOpt || ctx.goods?.preOpt;
    if (preOpt) headers.Authorization = `Bearer ${preOpt}`;
    return headers;
  }

  function isNolProductPage() {
    return /nol\.yanolja\.com$/i.test(location.hostname) && /\/ticket\/products\/\d+/.test(location.pathname);
  }

  function isGoodsPage() {
    return /tickets\.interpark\.com$/i.test(location.hostname) && location.pathname.includes("/goods/");
  }

  function isSeatPage() {
    return /tickets\.interpark\.com$/i.test(location.hostname) && location.pathname.startsWith("/onestop/seat");
  }

  // Anywhere along one show's path: product, goods, queue, schedule, seat, pay.
  function onShowPage() {
    return isNolProductPage() || isGoodsPage() || isSeatPage() || isGatesPage()
      || isWaitingPage() || /\/onestop\//.test(location.pathname);
  }

  function isGatesPage() {
    return /tickets\.interpark\.com$/i.test(location.hostname) && location.pathname.startsWith("/gates/");
  }

  function isWaitingPage() {
    const host = location.hostname;
    const path = location.pathname + location.search;
    return /waiting|queue|대기/i.test(host + path);
  }

  const CAPTCHA_LENGTH = 6;
  const CAPTCHA_TTL_MS = 5 * 60 * 1000;
  const CAPTCHA_JUNK = /^(NOL|LOGO|TICKET|INTERPARK|YANOLJA|CAPTCHA|IMAGE|NOLSNIPER)$/;

  // Only the Interpark modal title. Never "보안문자" — that string is in our
  // own toast, and matching it made the sniper wait forever after a manual solve.
  function isCaptchaPageCopy(text) {
    return /화면의\s*문자를\s*입력해주세요|문자를\s*입력해주세요/.test(String(text || ""));
  }

  function isSniperOverlay(node) {
    return Boolean(node && (node.id === "nolsniper-overlay" || node.closest?.("#nolsniper-overlay")));
  }

  function isVisible(el) {
    if (!el) return false;
    const style = typeof window.getComputedStyle === "function" ? window.getComputedStyle(el) : null;
    if (style && (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0)) {
      return false;
    }
    const rect = el.getBoundingClientRect?.();
    if (rect && (rect.width < 2 || rect.height < 2)) return false;
    return true;
  }

  function captchaPresent() {
    const modal = findCaptchaModal();
    if (modal && isVisible(modal)) return true;
    const input = findCaptchaInput();
    return Boolean(input && isVisible(input));
  }

  function findCaptchaModal() {
    // innerText matches bubble up, so the page shell also contains the modal
    // copy. The smallest matching node is the dialog itself — the first
    // matching node is often a wrapper that still includes the NOL logo.
    const nodes = [...document.querySelectorAll("div,section,article,aside,[role=dialog]")].filter(
      (node) =>
        !isSniperOverlay(node) &&
        isCaptchaPageCopy(node.innerText || "") &&
        node.querySelector("img, canvas, input"),
    );
    if (!nodes.length) return null;
    nodes.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
    return nodes[0];
  }

  function findCaptchaInput() {
    const modal = findCaptchaModal();
    const scoped = modal ? [...modal.querySelectorAll("input[type=text], input:not([type]), input[maxlength]")] : [];
    return (
      scoped.find((el) => el.offsetParent !== null && !el.disabled) ||
      [...document.querySelectorAll("input")].find(
        (el) =>
          el.offsetParent !== null &&
          /captcha|보안|문자/i.test(`${el.name}${el.id}${el.placeholder}`),
      ) ||
      null
    );
  }

  // Interpark words this modal several ways, and only one of them was matched.
  // Reported live: "취소/환불 기간이 지난 예매를 선택했습니다. 이 일정은 예매 후
  // 취소/환불이 불가능합니다." — that is not 취소/환불 *안내*, so it was never
  // recognised and never dismissed. It is modal, so it blocked the seat map and
  // every action after it failed.
  // Deliberately narrow: this runs against whole-page text, and the 취소/환불
  // wording also appears as static 예매 안내 copy on the seat page. Matching it
  // here would make bookingNoticeVisible() permanently true and no run could
  // ever start. The broader wording is handled at the dialog level below, where
  // the node is scoped and its visibility checked.
  function isBookingNoticeCopy(text) {
    return /취소\/환불\s*안내|확인하고\s*예매하기|동의하고\s*예매하기/.test(String(text || ""));
  }

  // A *modal* saying any of these, with a confirm button, is the post-selection
  // notice. Reported live and previously unmatched: "취소/환불 기간이 지난 예매를
  // 선택했습니다. 이 일정은 예매 후 취소/환불이 불가능합니다." Because it went
  // unrecognised it was never dismissed, and being modal it blocked the seat map
  // so every later action failed with 좌석 요청이 잘못되었습니다.
  const BOOKING_MODAL_COPY =
    /취소\/환불\s*안내|취소\/환불\s*기간|취소\/환불이?\s*불가능|예매\s*안내|확인하고\s*예매하기|동의하고\s*예매하기/;

  // Our own toast also says "취소/환불 안내" / "확인하고 예매하기". Using
  // document.body.innerText made the sniper think the Interpark modal was still
  // open forever after the first warn overlay.
  // Read the *rendered* text, then subtract our own toast from it.
  //
  // This used to clone the body and read the clone. `innerText` on a detached
  // node is not rendered text — the spec falls back to `textContent` — so the
  // clone reported hidden, collapsed and off-screen nodes as if they were
  // visible. Every judgement the autopilot makes about page state reads through
  // here (booking notices, refund notices, seatSelectionEmpty, selectedSeatCount),
  // and the degraded read only started once a toast existed, i.e. only while the
  // macro was running and never when clicking by hand.
  function pageTextWithoutOverlay() {
    const body = document.body;
    if (!body) return "";
    const text = body.innerText || "";
    const overlay = document.getElementById("nolsniper-overlay");
    const own = overlay ? overlay.innerText || "" : "";
    // Subtracting the string beats hiding the node: no reflow, and this runs on
    // every loop iteration over maps with thousands of seats.
    return own ? text.split(own).join(" ") : text;
  }

  function bookingNoticeVisible() {
    return isBookingNoticeCopy(pageTextWithoutOverlay());
  }

  function refundNoticeVisible() {
    return /취소\/환불\s*안내/.test(pageTextWithoutOverlay());
  }

  function forceClick(el) {
    if (!el) return false;
    const target = el.closest?.("button, a, [role=button]") || el;
    try {
      target.focus?.();
    } catch {
      /* ignore */
    }
    const opts = { bubbles: true, cancelable: true, view: window };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      target.dispatchEvent(new MouseEvent(type, opts));
    }
    if (typeof target.click === "function") target.click();
    return true;
  }

  function findBookingNoticeConfirmButton() {
    const dialogs = [...document.querySelectorAll("div,section,article,aside,[role=dialog]")].filter(
      (node) =>
        !isSniperOverlay(node) &&
        BOOKING_MODAL_COPY.test(node.innerText || "") &&
        isVisible(node),
    );
    dialogs.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
    const scopes = dialogs.length ? [dialogs[0], document] : [document];
    for (const scope of scopes) {
      const nodes = [
        ...scope.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit]"),
      ];
      const hits = nodes.filter((el) => {
        if (isSniperOverlay(el)) return false;
        const label = el.value || el.textContent || "";
        if (!isBookingNoticeConfirm(label)) return false;
        return isVisible(el) && !el.disabled;
      });
      if (hits.length) {
        hits.sort((a, b) => (a.textContent || "").length - (b.textContent || "").length);
        return hits[0];
      }
    }
    // Custom primary buttons sometimes put the label only on a child span.
    const labeled = [...document.querySelectorAll("button, a, [role=button], div, span")].filter((el) => {
      if (isSniperOverlay(el)) return false;
      const own = (el.childElementCount ? [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("") : el.textContent) || "";
      return isBookingNoticeConfirm(own) && isVisible(el);
    });
    if (!labeled.length) return null;
    labeled.sort((a, b) => (a.textContent || "").length - (b.textContent || "").length);
    return labeled[0].closest("button, a, [role=button]") || labeled[0];
  }

  function dismissBookingNotices() {
    const hit = findBookingNoticeConfirmButton();
    if (!hit) return false;
    return forceClick(hit);
  }

  // The seat map's own error / busy alert. It is modal: while it is up nothing
  // on the map can be clicked. Official onestop also shows seat_requestPending
  // when 선택 완료 is pressed while a preselect/select is still in flight —
  // that is the "guardrail" users hit, not an iframe/anti-macro myth.
  const SEAT_ERROR_DIALOG =
    /좌석\s*선택\s*도중\s*오류|좌석\s*요청이\s*잘못|선택\s*가능한\s*매수를\s*초과|요청\s*처리\s*중|잠시만\s*기다려|seat_requestPending/;

  // Deliberately narrow. The first version selected every div on the page and
  // clicked the first 확인 inside anything whose text contained the phrase —
  // which on a seat map is most of the document once the error has appeared, so
  // it could press an unrelated 확인 while a seat was being picked by hand.
  //
  // A real alert here is a small, visible, self-contained box. Anything larger
  // is a container that merely happens to include the text, and is left alone.
  const SEAT_ERROR_MAX_TEXT = 200;

  /**
   * Visible *and* big enough to be a dialog.
   *
   * This was also called `isVisible`, declared in the same IIFE scope as the
   * general one above. Function declarations hoist, so the later one won for the
   * whole file — including the call sites written above it — and the general
   * test was unreachable dead code. Every button and input in the app was being
   * judged against a 40x20 minimum meant for alert boxes, with no opacity test:
   * a small label-carrying span read as invisible, and a modal fading out at
   * opacity 0 read as still blocking.
   *
   * Only the two dialog scans want this. Everything else wants isVisible.
   */
  function isVisibleDialog(node) {
    if (!node?.getBoundingClientRect) return false;
    const box = node.getBoundingClientRect();
    if (box.width < 40 || box.height < 20) return false;
    const style = window.getComputedStyle?.(node);
    return !style || (style.visibility !== "hidden" && style.display !== "none");
  }

  // A seat lost to another buyer, which is a different thing entirely from the
  // errors above. Those mean "something went wrong, back off"; this means "that
  // one seat is gone, take the next one now" — the ordinary outcome of a race
  // against other people's macros, where the right response is speed.
  //
  // It had no pattern at all, so seatErrorDialogVisible() answered false, the
  // select wait ran to its timeout, and the modal — which is modal — sat there
  // blocking every later click. The run did not move on; it ground to a halt.
  const SEAT_TAKEN_DIALOG =
    /이미\s*선점|이미\s*선택된\s*좌석|이미\s*판매|다른\s*고객(님)?\s*이/;

  function confirmButtonIn(node) {
    return [...node.querySelectorAll("button,a,[role=button]")].find((el) =>
      /^(확인|닫기|OK)$/.test((el.textContent || "").replace(/\s+/g, "")),
    );
  }

  function dialogNodesMatching(pattern) {
    // Not just [role=dialog]. NOL's 이미 선점된 좌석입니다 box carries no role at
    // all, so a role-only query never saw it: the run stopped with the modal
    // still on screen, which is exactly the freeze this was meant to prevent.
    //
    // The breadth is safe because of what is still required: the text must
    // match, the box must be small (an early version clicked 확인 inside any
    // container whose text merely contained the phrase — on a seat map that is
    // most of the document), it must be visible, and it must actually own a
    // 확인 button. Innermost first, so we act on the alert and not its wrapper.
    const hits = [];
    for (const node of document.querySelectorAll(
      "[role=dialog],[role=alertdialog],div,section,aside,article",
    )) {
      const text = (node.innerText || "").trim();
      if (!text || text.length > SEAT_ERROR_MAX_TEXT) continue;
      if (!pattern.test(text)) continue;
      if (!isVisibleDialog(node)) continue;
      if (!confirmButtonIn(node)) continue;
      hits.push({ node, len: text.length });
    }
    hits.sort((a, b) => a.len - b.len);
    return hits.map((hit) => hit.node);
  }

  function dismissDialogNodes(nodes, counterKey) {
    for (const node of nodes) {
      const confirm = confirmButtonIn(node);
      if (!confirm) continue;
      confirm.click();
      if (counterKey) seatState[counterKey] = (seatState[counterKey] || 0) + 1;
      return true;
    }
    return false;
  }

  function seatErrorDialogNodes() {
    return dialogNodesMatching(SEAT_ERROR_DIALOG);
  }

  function seatErrorDialogVisible() {
    return seatErrorDialogNodes().length > 0;
  }

  function dismissSeatErrorDialog() {
    return dismissDialogNodes(seatErrorDialogNodes(), "seatErrorDialogs");
  }

  function seatTakenDialogNodes() {
    return dialogNodesMatching(SEAT_TAKEN_DIALOG);
  }

  function seatTakenDialogVisible() {
    return seatTakenDialogNodes().length > 0;
  }

  function dismissSeatTakenDialog() {
    return dismissDialogNodes(seatTakenDialogNodes(), "takenConflicts");
  }

  // Anything small, visible and modal that neither pattern claims. Recording it
  // is the whole reason 이미 선점 took a screenshot round-trip to find: an
  // unmatched modal is invisible from outside the browser, and it blocks the
  // map just as effectively as one we know.
  function unknownBlockingDialogText() {
    for (const node of document.querySelectorAll("[role=dialog],[role=alertdialog]")) {
      const text = (node.innerText || "").trim();
      if (!text || text.length > SEAT_ERROR_MAX_TEXT) continue;
      if (SEAT_ERROR_DIALOG.test(text) || SEAT_TAKEN_DIALOG.test(text)) continue;
      if (!isVisibleDialog(node)) continue;
      return text.slice(0, 160);
    }
    return null;
  }

  // Any modal, whatever it says.
  //
  // Every computed block click was landing on DIV.nds-e-dialog__overlay — a
  // full-screen 1320x956 backdrop — so the clicks were hitting a dialog, not
  // the venue. The phrase-matched detectors could not see it because its text
  // is not one of the phrases we know, and an unknown modal blocks the map just
  // as completely as a known one. Structure is the reliable signal here: a
  // backdrop with a dialog that owns a dismiss button.
  function blockingOverlayNodes() {
    const out = [];
    for (const node of document.querySelectorAll('[class*="dialog"],[class*="Dialog"],[class*="modal"],[class*="Modal"]')) {
      const rect = node.getBoundingClientRect();
      if (rect.width < 120 || rect.height < 60) continue;
      const style = window.getComputedStyle?.(node);
      if (style && (style.visibility === "hidden" || style.display === "none")) continue;
      out.push(node);
      if (out.length >= 12) break;
    }
    return out;
  }

  /**
   * A modal that is actually answering us, not merely present.
   *
   * blockingOverlayNodes() is deliberately broad — it feeds a dismisser that
   * simply finds nothing to press on a false positive. Here a false positive
   * would report a good selection as declined, so the dismiss button is
   * required: that is what distinguishes an alert from a layout wrapper that
   * happens to carry "dialog" in its class.
   */
  function blockingOverlayAnswered() {
    return blockingOverlayNodes().some((node) => confirmButtonIn(node));
  }

  function describeBlockingOverlay() {
    for (const node of blockingOverlayNodes()) {
      const text = (node.innerText || "").trim();
      if (!text) continue;
      return {
        cls: String(node.getAttribute("class") || "").slice(0, 60),
        text: text.slice(0, 160),
        hasConfirm: Boolean(confirmButtonIn(node)),
      };
    }
    return null;
  }

  // Dismiss whatever is covering the page. Only ever presses a button that says
  // 확인/닫기/OK, and prefers the smallest box that owns one, so it cannot go
  // pressing 예매 or 결제 on a page that merely happens to be modal.
  function dismissAnyBlockingOverlay() {
    const owners = [];
    for (const node of blockingOverlayNodes()) {
      const button = confirmButtonIn(node);
      if (!button) continue;
      const text = (node.innerText || "").trim();
      // Never the 보안문자 box. Its submit button reads 확인, which is exactly
      // what confirmButtonIn matches and what NEVER_CLICK does not exclude — and
      // waitForCaptchaClear called this every 400ms for up to two minutes while
      // the user was typing. That submits a half-typed captcha, repeatedly, and
      // there was nothing anywhere in this path that knew what a captcha was.
      if (isCaptchaPageCopy(text)) continue;
      owners.push({ node, button, len: text.length });
    }
    if (!owners.length) return false;
    owners.sort((a, b) => a.len - b.len);
    const target = owners[0];
    // Belt and braces: the button text is already constrained to 확인/닫기/OK,
    // but never press one that also reads like leaving the page.
    const label = `${target.button.getAttribute?.("aria-label") || ""} ${target.button.textContent || ""}`;
    if (NEVER_CLICK.test(label) || target.button.closest?.("a,[href]")) return false;
    seatState.unknownDialog = (target.node.innerText || "").trim().slice(0, 160);
    traceCall("dismissOverlay", null, {
      cls: String(target.node.getAttribute("class") || "").slice(0, 60),
      text: seatState.unknownDialog,
    });
    target.button.click();
    seatState.overlaysDismissed = (seatState.overlaysDismissed || 0) + 1;
    return true;
  }

  function dismissBlockingDialogs() {
    // Taken-seat first: it is the most common one during an open, and nothing
    // on the map is clickable while it is up.
    if (dismissSeatTakenDialog()) return true;
    if (dismissSeatErrorDialog()) return true;
    // Last: anything else modal. A dialog we cannot name still stops the map.
    if (dismissAnyBlockingOverlay()) return true;
    if (dismissBookingNotices()) return true;
    const nodes = [...document.querySelectorAll("div,section,article,aside,[role=dialog]")].filter((node) =>
      /예매를 잃게|예매확인\/취소로 이동/.test(node.innerText || ""),
    );
    if (!nodes.length) return false;
    nodes.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
    const cancel = [...nodes[0].querySelectorAll("button, a")].find(
      (el) => (el.textContent || "").replace(/\s+/g, "") === "취소",
    );
    if (!cancel) return false;
    cancel.click();
    return true;
  }

  async function waitForSeatMapReady({ allowRefundConfirm = true } = {}) {
    for (let attempt = 0; attempt < 16; attempt += 1) {
      if (refundNoticeVisible() && !allowRefundConfirm) return true;
      if (dismissBookingNotices() || bookingNoticeVisible()) {
        updateOverlay(refundNoticeVisible() ? "환불 안내 확인 중…" : "예매 안내 확인 중…", "info");
        await sleep(280);
        continue;
      }
      return true;
    }
    return true;
  }

  async function waitForPageSelectOutcome({ since, timeoutMs = 6000 } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (refundNoticeVisible() || bookingNoticeVisible() || location.search.includes("step=price")) {
        return { ok: true, via: "ui" };
      }
      // Checked before the generic error: a seat lost to someone else is not a
      // fault and must not be treated as one.
      if (seatTakenDialogVisible()) {
        return { ok: false, via: "taken" };
      }
      if (seatErrorDialogVisible()) {
        return { ok: false, via: "dialog" };
      }
      const net = window.__nolsniperLastSeatNet || {};
      if ((net.selectAt || 0) >= since) {
        return { ok: Boolean(net.selectOk), via: "net", status: net.selectStatus };
      }
      await sleep(100);
    }
    if (refundNoticeVisible() || bookingNoticeVisible() || location.search.includes("step=price")) {
      return { ok: true, via: "ui-late" };
    }
    if (seatTakenDialogVisible()) return { ok: false, via: "taken" };
    if (seatErrorDialogVisible()) return { ok: false, via: "dialog-late" };
    // Timed out with a modal up that neither pattern claims. Recording it is
    // what turns the next unknown copy into one `diagnose` instead of a
    // screenshot round-trip — which is exactly what 이미 선점 cost.
    const unknown = unknownBlockingDialogText();
    if (unknown) {
      seatState.unknownDialog = unknown;
      traceCall("unknownDialog", null, { text: unknown });
    }
    const net = window.__nolsniperLastSeatNet || {};
    if ((net.selectAt || 0) >= since) {
      return { ok: Boolean(net.selectOk), via: "net-late", status: net.selectStatus };
    }
    return { ok: null, via: "timeout" };
  }

  // Official seat bundle refuses 선택 완료 while preselect/select is in flight
  // (b.current / j.current) and shows seat_requestPending. Wait until the page
  // PreselectSeat response is done, then a quiet gap so finally() can clear.
  async function waitForSoftHoldIdle({ since, quietMs = 600, timeoutMs = 4000 } = {}) {
    const start = Date.now();
    let sawPreselect = false;
    while (Date.now() - start < timeoutMs) {
      if (seatErrorDialogVisible()) return false;
      const net = window.__nolsniperLastSeatNet || {};
      if ((net.preselectAt || 0) >= since) {
        if (net.preselectOk === false) return false;
        sawPreselect = true;
        // The gap is measured from the page's answer, not from the moment we
        // noticed it. Nothing gets here until selectSeats has already watched
        // 선택 좌석 rise, and the cart only rises *after* that answer — so
        // sleeping a further full quietMs held 선택 완료 back for time that had
        // already elapsed. Measured against a 220ms preselect that was 250ms
        // held for a page that had been idle for ~60 of them.
        //
        // The invariant is unchanged and is the load-bearing part: 선택 완료 is
        // never pressed less than quietMs after the preselect response, because
        // NOL's own bundle refuses it while its in-flight flag is still set and
        // answers seat_requestPending.
        seatState.lastSoftHoldWaitMs = Math.max(0, quietMs - (Date.now() - net.preselectAt));
        await sleep(seatState.lastSoftHoldWaitMs);
        if (!pageHasSelectedSeats()) return false;
        return true;
      }
      // Sidebar already moved and no net signal yet — still give the page time.
      if (pageHasSelectedSeats() && Date.now() - since > 1200) {
        await sleep(quietMs);
        return pageHasSelectedSeats();
      }
      await sleep(100);
    }
    return pageHasSelectedSeats();
  }

  async function recoverFailedConfirm(label = "", reason = "") {
    // Someone else confirmed this seat between our click and our 선택 완료.
    // That is a lost race, not a broken session, and the difference matters:
    // the session path tells the user to close the window and start over,
    // which during an open throws away a working queue position for something
    // that only needed the next seat. Observed live as 이미 선점된 좌석입니다
    // with the run reporting 중단됨.
    // The rejection can reach us as a network answer before the page has drawn
    // its modal — the live failure reported reason "net", with 이미 선점된
    // 좌석입니다 appearing a moment later. Deciding immediately would call that
    // a broken session. Give the modal a beat to show itself.
    let takenDialog = seatTakenDialogVisible();
    for (let wait = 0; !takenDialog && wait < 6; wait += 1) {
      await sleep(100);
      takenDialog = seatTakenDialogVisible();
    }
    if (takenDialog) {
      dismissSeatTakenDialog();
      if (pageHasSelectedSeats()) clearSelectedSeats();
      // Handing this seat back is bookkeeping for something already lost —
      // the next candidate is a different seatInfoId with no dependency on
      // this release completing. Clear it out of heldSeatIds immediately, so
      // the loop's own quantity/hold checks downstream are correct without
      // waiting, and let the release itself settle in the background instead
      // of blocking the next attempt behind a full BulkDeselectSeats round
      // trip. Same reasoning the map-click rejection path already uses — "No
      // sleep: the next seat is being raced for right now too" — brought to
      // the one place that hadn't caught up to it.
      //
      // releasePreselected never rejects: its own try/catch already counts a
      // failure into releaseFailures/lastReleaseError and logs it, so nothing
      // further needs to be awaited or chained here. One real behavior
      // change: a failed release no longer sets this function's own
      // lastError message, because by the time that background call resolves
      // the run has moved on to a different seat — a delayed write here would
      // either never be seen (still running; the band shows 감시 중, not
      // lastError, while running) or could overwrite whatever lastError the
      // run set when it actually stopped, for an unrelated reason.
      // releaseFailures/lastReleaseError still capture the failure either way.
      const held = [...seatState.heldSeatIds];
      held.forEach((id) => seatState.heldSeatIds.delete(id));
      // .catch, not bare — releasePreselected's own try/catch means this never
      // actually rejects today, but a dropped promise with no handler is a
      // silent-failure hazard if that ever changes (flagged by tools/audit_js.mjs,
      // the same D5 class as an un-awaited runArmScheduler() elsewhere in this
      // file). The catch itself is a no-op: releaseFailures/lastReleaseError are
      // already set inside releasePreselected before it would ever get here.
      if (held.length) releasePreselected(held).catch(() => {});
      seatState.locked = false;
      seatState.awaitingPayment = false;
      seatState.lastExit = "takenByAnother";
      updateOverlay(`${label} 이미 선점됨 — 다음 자리로`, "warn");
      return { awaitingPayment: false, takenConflict: true };
    }

    dismissSeatTakenDialog();
    dismissSeatErrorDialog();
    if (pageHasSelectedSeats()) clearSelectedSeats();
    // Left as-is (not decoupled): this branch is the genuinely-broken-session
    // case, not a lost race, and nothing here established this 500ms is safe
    // to shorten or run concurrently with — unlike the taken-dialog branch
    // above, there is no sibling path in this file to compare it against.
    await sleep(500);
    const held = [...seatState.heldSeatIds];
    held.forEach((id) => seatState.heldSeatIds.delete(id));
    if (held.length) releasePreselected(held).catch(() => {});
    seatState.locked = false;
    seatState.awaitingPayment = false;
    seatState.lastExit = "confirmPreselectionInvalid";
    seatState.lastError =
      `${label ? label + " · " : ""}가선점 확정 실패` +
      `${reason ? ` (${reason})` : ""}. ` +
      `선택 완료를 요청 처리 중에 눌렀거나 세션/잔여가 어긋난 경우입니다. ` +
      `예매 창을 닫고 예매하기부터 다시 들어오세요.`;
    updateOverlay(
      `선택 완료 거절 · ${AUTOPILOT_BUILD}<br>${reason || "예매하기부터 다시"}`,
      "error",
    );
    return { awaitingPayment: false, confirmFailed: true };
  }

  function findConfirmSelectButton() {
    const nodes = [...document.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit]")];
    return (
      nodes.find((el) => {
        const text = (el.value || el.textContent || "").replace(/\s+/g, "");
        if (!/^선택완료$/.test(text)) return false;
        if (!isVisible(el)) return false;
        if (el.disabled) return false;
        if (el.getAttribute("aria-disabled") === "true") return false;
        return true;
      }) || null
    );
  }

  // Exactly one click. forceClick fires pointer+mouse+click+.click() and that
  // can look like a double press to NOL's 선택 완료 handler.
  function clickConfirmSelect() {
    const hit = findConfirmSelectButton();
    if (!hit) {
      traceCall("clickConfirmSelect", null, { ok: false, reason: "no-enabled-button" });
      return false;
    }
    hit.click();
    noteCatchStage("confirm");
    traceCall("clickConfirmSelect", null, { ok: true, reason: "once" });
    return true;
  }

  async function advanceAfterSeatLock(config) {
    // Job: soft-hold is already on the page → click 선택 완료 once → stop.
    //
    // The "delay then error" symptom: we sleep after lock, and during that
    // sleep bootRoute (auto_seats_after_entry) re-enters runSeatAutopilot →
    // locked branch → second advanceAfterSeatLock → two 선택 완료 presses
    // (or the second hits NOL's in-flight guard). Latch blocks that.
    if (seatState.confirmStarted) {
      traceCall("advanceAfterSeatLock", null, { skipped: "confirmStarted" });
      return { awaitingPayment: false, reserved: true, userContinues: true, skipped: true };
    }

    // Deliberately NOT checking the sidebar yet. The page updates 선택 좌석 only
    // after its own PreselectSeat call resolves, and we are called immediately
    // after the map click — so an instantaneous check reads "empty", unlocks a
    // hold that is genuinely live, and the end-of-run cleanup then releases it.
    // That is the observed failure: PreselectSeat true, no page:select, then
    // BulkDeselectSeats. waitForSoftHoldIdle below performs the same check
    // after waiting for the signal that makes it meaningful.
    traceCall("advanceAfterSeatLock", null, {
      entered: true,
      selectedNow: selectedSeatCount(),
      locked: seatState.locked,
    });

    if (refundNoticeVisible() || bookingNoticeVisible() || location.search.includes("step=price")) {
      seatState.confirmStarted = true;
      seatState.lastExit = "reservedUserContinues";
      updateOverlay(
        `예약됨 ${seatState.lastSeat || ""}<br>나머지는 직접 진행 · ${AUTOPILOT_BUILD}`,
        "ok",
      );
      return { awaitingPayment: false, reserved: true, userContinues: true };
    }

    seatState.confirmStarted = true;
    // Wait until page PreselectSeat finished, then a short quiet — not a long
    // fixed sleep that leaves a window for bootRoute to start a second advance.
    if (seatState.markStartup) seatState.markStartup("advance");
    const holdSince = Date.now() - 5000;
    updateOverlay(`가선점 확인 후 선택 완료…<br>${AUTOPILOT_BUILD}`, "info");
    const idle = await waitForSoftHoldIdle({ since: holdSince, quietMs: 250, timeoutMs: 2500 });
    if (!idle || !pageHasSelectedSeats()) {
      seatState.confirmStarted = false;
      seatState.locked = false;
      seatState.lastExit = "advanceWithNoSeat";
      finishCatchTiming("noSoftHold");
      updateOverlay("가선점 미확인 — [선택 완료]를 누르지 않습니다", "warn");
      return { awaitingPayment: false, noSeat: true };
    }

    if (refundNoticeVisible() || bookingNoticeVisible() || location.search.includes("step=price")) {
      seatState.lastExit = "reservedUserContinues";
      updateOverlay(
        `예약됨 ${seatState.lastSeat || ""}<br>나머지는 직접 진행 · ${AUTOPILOT_BUILD}`,
        "ok",
      );
      return { awaitingPayment: false, reserved: true, userContinues: true };
    }

    updateOverlay(`선택 완료 1회 · ${seatState.lastSeat || ""}<br>${AUTOPILOT_BUILD}`, "info");
    const since = Date.now();
    if (!clickConfirmSelect()) {
      seatState.confirmStarted = false;
      seatState.lastExit = "awaitingManualConfirm";
      finishCatchTiming("noConfirmButton");
      updateOverlay("선택 완료를 직접 눌러 주세요", "warn");
      return { awaitingPayment: false, awaitingManualConfirm: true };
    }

    const outcome = await waitForPageSelectOutcome({ since, timeoutMs: 5000 });
    noteCatchStage("outcome");
    traceCall("confirmSelectOutcome", null, outcome);
    if (outcome.ok === false) {
      seatState.confirmStarted = false;
      finishCatchTiming(`rejected:${outcome.via}`);
      return recoverFailedConfirm(seatState.lastSeat || "", outcome.via);
    }
    finishCatchTiming(outcome.ok ? "reserved" : "unconfirmed");
    if (seatState.markStartup) seatState.markStartup("reserved");

    seatState.lastExit = "reservedUserContinues";
    const spent = catchTimingLine();
    updateOverlay(
      `예약 요청 ${seatState.lastSeat || ""}<br>환불 안내부터는 직접 · ${AUTOPILOT_BUILD}` +
        (spent ? `<br>${spent}` : ""),
      outcome.ok ? "ok" : "warn",
    );
    return { awaitingPayment: false, reserved: true, userContinues: true };
  }

  // The macro never types a captcha.
  //
  // The old solver reported success as soon as it had typed six plausible
  // characters — it never checked the modal closed, so a misread was submitted
  // and the run carried on believing it had passed. That silent failure is worse
  // than no automation at all, and a human reading six characters beats three
  // OCR round trips. Detection stays; solving is gone.
  const captchaReport = { state: "idle", detail: "" };

  function setCaptchaReport(state, detail = "") {
    captchaReport.state = state;
    captchaReport.detail = detail;
  }


  // Wait for the human, then continue instantly.
  //
  // This is the only place a person is in the loop, so the handover has to be
  // obvious and the resume immediate: the 400ms poll means the armed run
  // proceeds within a tick of the modal closing, with no further button press.
  async function waitForCaptchaClear(timeoutMs = 120000) {
    if (!captchaPresent()) {
      // Reset on the way past. The report used to stick on its last state for
      // the rest of the session, and because the panel ranks captcha above every
      // other hint, it masked seat errors and rejections underneath.
      if (captchaReport.state !== "idle") setCaptchaReport("idle");
      return true;
    }

    const deadline = Date.now() + timeoutMs;
    // Once, for anything already stacked on top of the captcha. Not on every
    // pass: the point of this wait is to leave the user alone while they type,
    // and a dismisser running ten times a second is the opposite of that.
    dismissBlockingDialogs();
    setCaptchaReport("waiting", "예매 창에서 6자리를 입력하세요");
    updateOverlay("보안문자 — 예매 창에서 직접 입력하세요<br>통과하면 바로 이어서 진행합니다", "warn");

    while (Date.now() < deadline) {
      if (!captchaPresent()) {
        setCaptchaReport("idle");
        updateOverlay("보안문자 통과 — 이어서 진행합니다", "ok");
        await sleep(120);
        return true;
      }
      await sleep(400);
    }

    setCaptchaReport("timeout", "인증 화면이 닫히지 않았습니다");
    updateOverlay("보안문자가 아직 열려 있습니다", "error");
    return false;
  }

  function clickFirstMatching(pattern) {
    const nodes = [...document.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit]")];
    const hit = nodes.find((el) => {
      const text = (el.value || el.textContent || "").trim();
      return pattern.test(text) && isVisible(el) && !el.disabled;
    });
    if (!hit) return false;
    hit.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    return true;
  }

  // NOL/onestop "예매 안내" overlay. Exact labels only — never bare "확인",
  // which would hit 예매확인/취소 and dump the booking session.
  function isBookingNoticeConfirm(text) {
    const compact = String(text || "").replace(/\s+/g, "");
    return compact === "확인하고예매하기" || compact === "동의하고예매하기";
  }

  // Anything that commits money. POST /payment/order/{goodsCode} is the actual
  // purchase, and these controls trigger it, so the autopilot never clicks them.
  const COMMIT_BUTTON = /결제\s*하기|결제\s*완료|입금\s*하기|구매\s*하기|주문\s*완료|결제\s*진행/;
  const ADVANCE_BUTTON = /^\s*(다음|다음\s*단계|확인)\s*$/;

  function seatSelectionEmpty() {
    // count === 0 covers "선택한 좌석이 없습니다". Do not treat an unreadable
    // sidebar (count < 0) as empty here — that would bounce valid pages. The
    // 선택 완료 gate uses pageHasSelectedSeats() instead, which requires proof.
    if (selectedSeatCount() === 0) return true;
    const text = pageTextWithoutOverlay();
    if (/구매하실\s*좌석을\s*선택해주세요/.test(text)) return true;
    return false;
  }

  // Positive proof the SPA cart holds at least one seat. Never press 선택 완료
  // without this — that is exactly the empty-cart error modal.
  function pageHasSelectedSeats() {
    return selectedSeatCount() >= 1;
  }

  function emptyPriceStepVisible() {
    if (!location.search.includes("step=price")) return false;
    if (seatSelectionEmpty()) return true;
    const text = pageTextWithoutOverlay();
    return /가격\s*선택/.test(text) && /0\s*원/.test(text);
  }

  function dismissSeatRequiredAlert() {
    const nodes = [...document.querySelectorAll("button, a, [role=button]")].filter((el) => {
      if (!isVisible(el) || el.disabled) return false;
      if ((el.textContent || "").replace(/\s+/g, "") !== "확인") return false;
      const root = el.closest("[role=dialog], aside, section, article, div") || el.parentElement;
      return /구매하실\s*좌석을\s*선택해주세요/.test(root?.innerText || "");
    });
    if (!nodes.length) return false;
    forceClick(nodes[0]);
    return true;
  }

  function recoverEmptyPriceStep() {
    dismissSeatRequiredAlert();
    seatState.locked = false;
    seatState.awaitingPayment = false;
    seatState.lastSeat = "";
    updateOverlay("좌석 없이 가격 단계에 들어감 — 좌석맵으로 복귀", "warn");
    if (location.pathname.includes("/onestop/seat") && location.search.includes("step=price")) {
      const next = new URL(location.href);
      next.searchParams.delete("step");
      const query = next.searchParams.toString();
      location.href = next.pathname + (query ? `?${query}` : "");
      return { awaitingPayment: false, recovered: true };
    }
    return { awaitingPayment: false, recovered: false };
  }

  function notifyDiscord(message) {
    const url = loadSeatConfig().discord_webhook;
    if (!url || !/^https:\/\/discord\.com\/api\/webhooks\//.test(url)) return;
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: message }),
    }).catch(() => {});
  }

  function buildSsoUrl(arm) {
    const gate = new URL("/gates/partner", GATE_ORIGIN);
    gate.searchParams.set("bc", "61776");
    gate.searchParams.set("gc", arm.goods_code);
    if (arm.place_code) gate.searchParams.set("pc", arm.place_code);
    if (arm.play_seq) gate.searchParams.set("ps", arm.play_seq);
    gate.searchParams.set("cc", "Gates");
    const sso = new URL("/sso/v1/bridge/token", SSO_ORIGIN);
    sso.searchParams.set("source", "YANOLJA");
    sso.searchParams.set("serviceDomainCode", "NOL_TICKET");
    sso.searchParams.set("redirectUrl", gate.toString());
    return sso.toString();
  }

  const WAITING_ENDPOINT = "/v1/goods/{code}/waiting";

  async function fetchWaitingUrl(arm) {
    const params = new URLSearchParams({
      channelCode: arm.channel_code || "pc",
      preSales: arm.pre_sales || "N",
      playDate: arm.play_date,
      playSeq: arm.play_seq,
    });
    const url = `${TICKETFRONT}/v1/goods/${arm.goods_code}/waiting?${params}`;
    const response = await fetch(url, { credentials: "include" });
    const payload = await response.json().catch(() => ({}));
    const data = payload?.data ?? payload?.waitingUrl ?? null;
    if (typeof data === "string" && /로그인|logout|Unauthorized/i.test(data)) {
      throw new Error(data);
    }
    // BL is 비정상 예매로 차단 — a block, not an error to retry past. It used to
    // be thrown bare, so nothing recorded a cooldown and every other path in
    // the app carried on as if the account were fine.
    const blockedMs = readGatewayBlock(data ?? payload, {
      status: response.status === 401 ? 0 : response.status,
      headers: response.headers,
    });
    if (blockedMs >= 0) throw noteGatewayBlock(blockedMs, WAITING_ENDPOINT);
    if (!response.ok && response.status !== 401) {
      throw new Error(`waiting HTTP ${response.status}`);
    }
    return data;
  }

  /**
   * Can the credential be minted from here?
   *
   * member-info needs the .interpark.com session cookie, and the browser sends
   * none on a cross-site request — measured: 401 from nol.yanolja.com while the
   * request itself completes, so this is SameSite, not CORS, and no amount of
   * retrying from the product page will fix it.
   */
  function secureUrlUsableHere() {
    return location.origin === GATE_ORIGIN;
  }

  /**
   * The signature/secureData pair the queue call is authenticated with.
   *
   * The signature carries its own issue time (`<hex>.<unix>`), so it is minted
   * next to the fire rather than at arm time.
   */
  async function fetchMemberInfo(arm) {
    const url =
      `${MEMBER_INFO_PATH}?goodsCode=${encodeURIComponent(arm.goods_code)}&channelCode=pm`;
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) throw new Error(`member-info HTTP ${response.status}`);
    const data = await response.json();
    if (!data || !data.signature || !data.secureData) {
      throw new Error("예매 자격 정보를 받지 못했습니다. 로그인을 확인하세요.");
    }
    return data;
  }

  /**
   * One POST, and the answer is the queue URL. ~33ms measured, warm.
   *
   * Note what is *not* sent: no cookies (the pair in the body is the whole
   * credential) and no playDate — playSeq alone identifies the round.
   */
  async function fetchSecureUrl(arm, memberInfo) {
    const body = {
      signature: memberInfo.signature,
      secureData: memberInfo.secureData,
      lang: "ko",
      passCode: "",
      from: "NOL",
      goodsCode: String(arm.goods_code || ""),
      bizCode: String(arm.biz_code || "61776"),
      playSeq: String(arm.play_seq || ""),
      preSales: arm.pre_sales || "N",
    };
    // credentials: the gate's own bundle sends them, and the line-up that
    // follows is made with them too — a cookie this answer sets must not be
    // dropped on the floor by the one request in the chain that omits them.
    const response = await fetch(`${ENT_WAITING_ORIGIN}${SECURE_URL_PATH}`, {
      method: "POST",
      credentials: "include",
      body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", Accept: "application/json" },
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = JSON.parse(text);
    } catch {
      /* a non-JSON body is reported by status alone */
    }
    if (!response.ok) {
      const code = (payload && payload.error) || "";
      if (code === BLOCKED_ERROR) {
        throw new Error("비정상 예매로 차단되었습니다 — 재시도하지 마세요");
      }
      // Not an error to give up on: it is what every show says before its open.
      const error = new Error(
        code === NOT_OPEN_ERROR
          ? "아직 오픈 전입니다 (UnableReservationTime)"
          : `대기열 거절 · ${code || `HTTP ${response.status}`}`
      );
      error.notOpenYet = code === NOT_OPEN_ERROR;
      error.blocked = code === BLOCKED_ERROR;
      throw error;
    }
    const url = payload && payload.redirectUrl;
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
      throw new Error("대기열 URL을 받지 못했습니다");
    }
    return url;
  }

  const SCHEDULE_STEP_TIMEOUT_MS = 15000;

  function onSchedulePage() {
    return /\/onestop\/schedule/.test(location.pathname);
  }

  /**
   * Is the 일정 선택 step in front of the user right now?
   *
   * Not the same question as the URL. 일정변경 on the seat map opens the very
   * same calendar as a modal without navigating anywhere — measured, attempt 6:
   * the click landed, the calendar appeared, and a URL check sat waiting for a
   * page that never came. The calendar's own grid is the reliable signal.
   */
  function scheduleStepVisible() {
    return !!document.querySelector("[class*='EntCalendar_grid']");
  }

  /** Every spelling a time block has been seen to use, spaces removed. */
  function clockVariants(hhmm) {
    const digits = String(hhmm || "").replace(/\D/g, "");
    if (digits.length < 3) return [];
    const hour = Number(digits.slice(0, digits.length - 2));
    const minute = digits.slice(-2);
    const h12 = hour % 12 === 0 ? 12 : hour % 12;
    const ampm = hour < 12 ? "AM" : "PM";
    const korean = hour < 12 ? "오전" : "오후";
    return [
      `${h12}:${minute}${ampm}`,
      `${String(h12).padStart(2, "0")}:${minute}${ampm}`,
      `${String(hour).padStart(2, "0")}:${minute}`,
      `${korean}${h12}:${minute}`,
      `${korean}${h12}시${minute === "00" ? "" : minute + "분"}`,
      `${hour}시${minute === "00" ? "" : minute + "분"}`,
    ];
  }

  /** HHmm → the "7:30 PM" the 일정 선택 step prints. */
  function clockLabel(hhmm) {
    const digits = String(hhmm || "").replace(/\D/g, "");
    if (digits.length < 3) return "";
    const hour = Number(digits.slice(0, digits.length - 2));
    const minute = digits.slice(-2);
    const suffix = hour < 12 ? "AM" : "PM";
    const shown = hour % 12 === 0 ? 12 : hour % 12;
    return `${shown}:${minute} ${suffix}`;
  }

  /**
   * Every date cell on the 일정 선택 calendar, keyed by its real yyyyMMdd.
   *
   * The calendar renders several months at once as swiper slides and each cell
   * is only a day number, so the day alone is ambiguous — three months in the
   * DOM means three cells reading "4". The month heading inside each slide is
   * what disambiguates them.
   */
  // React's calendar handlers listen for the pointer sequence on the button,
  // not a bare .click() on the wrapper cell — .click() on the <td> switched
  // nothing (측정: 10/24 picked, calendar stayed on 10/17, "회차 선택 중…").
  function pressable(el) {
    if (!el) return null;
    if (el.matches && el.matches("button,[role=button],a")) return el;
    return (el.querySelector && el.querySelector("button,[role=button],a")) || el;
  }
  function nativePress(el) {
    const node = pressable(el);
    if (!node) return false;
    const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : { left: 0, top: 0, width: 0, height: 0 };
    const shared = { bubbles: true, cancelable: true, composed: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2, pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0 };
    try {
      if (typeof PointerEvent === "function") node.dispatchEvent(new PointerEvent("pointerdown", { ...shared, buttons: 1 }));
      node.dispatchEvent(new MouseEvent("mousedown", { ...shared, buttons: 1 }));
      if (typeof PointerEvent === "function") node.dispatchEvent(new PointerEvent("pointerup", { ...shared, buttons: 0 }));
      node.dispatchEvent(new MouseEvent("mouseup", { ...shared, buttons: 0 }));
      node.dispatchEvent(new MouseEvent("click", { ...shared, buttons: 0 }));
    } catch (error) {
      try { node.click(); } catch (inner) { return false; }
    }
    return true;
  }
  // The pressed/selected calendar cell, if it is the wanted yyyyMMdd.
  function activeDateCellFor(wantedDate) {
    try {
      const heading = document.querySelector("[class*='EntCalendar_month']");
      const ym = /^(\d{4})\.(\d{2})$/.exec(((heading && heading.textContent) || "").trim());
      if (!ym || `${ym[1]}${ym[2]}` !== wantedDate.slice(0, 6)) return null;
      const day = String(Number(wantedDate.slice(6, 8)));
      const active = [...document.querySelectorAll(
        "[class*='EntCalendar_grid'] [aria-pressed='true'], [class*='EntCalendar_grid'] [aria-selected='true'], [class*='EntCalendar_grid'] [class*='selected'], [class*='EntCalendar_grid'] [class*='active']"
      )];
      return active.find((el) => (el.textContent || "").replace(/\D/g, "") === day) || null;
    } catch { return null; }
  }
  // Press the calendar's next/previous-month control toward yyyyMMdd. True if
  // a control was pressed. Selectors are deliberately loose: the month strip
  // is an EntCalendar heading with a button either side.
  function pageCalendarToward(wantedDate) {
    try {
      const heading = document.querySelector("[class*='EntCalendar_month']");
      const ym = /^(\d{4})\.(\d{2})$/.exec(((heading && heading.textContent) || "").trim());
      if (!ym) return false;
      const shown = Number(ym[1]) * 12 + Number(ym[2]);
      const want = Number(wantedDate.slice(0, 4)) * 12 + Number(wantedDate.slice(4, 6));
      if (shown === want) return false;
      const forward = want > shown;
      const buttons = [...document.querySelectorAll("[class*='EntCalendar'] button, [class*='Calendar'] button")];
      const pick = buttons.find((b) => {
        const label = `${b.getAttribute("aria-label") || ""} ${b.textContent || ""} ${b.className || ""}`;
        return forward ? /다음|next|›|>|right/i.test(label) : /이전|prev|‹|<|left/i.test(label);
      });
      if (!pick || pick.disabled) return false;
      pick.click();
      return true;
    } catch { return false; }
  }
  // The day and time the page currently has pressed on /onestop/schedule.
  function adoptActiveSelection() {
    try {
      const heading = document.querySelector("[class*='EntCalendar_month']");
      const ym = /^(\d{4})\.(\d{2})$/.exec(((heading && heading.textContent) || "").trim());
      const cell = document.querySelector(
        "[class*='EntCalendar_grid'] [aria-pressed='true'], [class*='EntCalendar_grid'] [aria-selected='true'], [class*='EntCalendar_grid'] [class*='selected']"
      );
      const day = cell ? (cell.textContent || "").replace(/\D/g, "") : "";
      if (!ym || !cell || !day) return null;
      const play_date = `${ym[1]}${ym[2]}${day.padStart(2, "0")}`;
      const pressed = [...document.querySelectorAll("button[class*='TimeBlock_timeButton']")]
        .find((b) => b.getAttribute("aria-pressed") === "true" && !b.disabled);
      const clock = pressed ? (pressed.innerText || pressed.textContent || "").replace(/\s+/g, " ").trim() : "";
      const m = /(\d{1,2}):(\d{2})\s*(AM|PM)?/i.exec(clock);
      let play_time = "";
      if (m) {
        let h = Number(m[1]) % 12; if (/PM/i.test(m[3] || "")) h += 12;
        if (!m[3] && Number(m[1]) >= 13) h = Number(m[1]);
        play_time = `${String(h).padStart(2, "0")}${m[2]}`;
      }
      return { cell, play_date, play_time, clock };
    } catch { return null; }
  }
  function scheduleDateCells() {
    const cells = new Map();
    // The month is NOT inside each slide: there is exactly one heading in the
    // whole calendar and it names the *active* slide's month — measured,
    // attempt 7, where all three slides read 2026.09 and October's days were
    // filed under September. Slides run one month apart, so the active one's
    // heading plus each slide's offset from it gives the real month.
    const grids = [...document.querySelectorAll("[class*='EntCalendar_grid']")].map((grid) => {
      let slide = grid;
      while (slide && !/swiper-slide/.test(String(slide.className || ""))) {
        slide = slide.parentElement;
      }
      return { grid, slide };
    });
    if (!grids.length) return cells;
    const heading = document.querySelector("[class*='EntCalendar_month']");
    const matched = /^(\d{4})\.(\d{2})$/.exec(((heading && heading.textContent) || "").trim());
    if (!matched) return cells;
    let active = grids.findIndex(
      (x) => /swiper-slide-active/.test(String((x.slide || {}).className || ""))
    );
    if (active < 0) active = 0;

    grids.forEach(({ grid }, index) => {
      const months = Number(matched[1]) * 12 + (Number(matched[2]) - 1) + (index - active);
      const year = Math.floor(months / 12);
      const month = String((months % 12) + 1).padStart(2, "0");
      for (const button of grid.querySelectorAll("button[class*='EntCalendar_dateButton']")) {
        const day = ((button.querySelector("[class*='EntCalendar_number']") || button).textContent || "")
          .trim().replace(/\D/g, "");
        if (!day) continue;
        const key = `${year}${month}${day.padStart(2, "0")}`;
        if (!cells.has(key)) cells.set(key, button);
      }
    });
    return cells;
  }

  /**
   * Choose the armed round on /onestop/schedule and go through to the seat map.
   *
   * Measured, attempts 2 and 3: a multi-round show's booking session carries no
   * playSeq — `interpark/context` has only `goods`, with `isMultiPlay: true` —
   * so secure-url cannot pin the round and onestop asks for it here. Sending a
   * correct playSeq does not skip this step; nothing does. Driving it with the
   * round the user already picked is the fix, and it is a different thing from
   * dismissing a notice: this is the choice they made, being entered for them.
   */
  // The 예매 안내 gate also comes up *over the calendar* on /onestop/schedule —
  // measured 2026-09-04 12:1x: 지금 진입 landed there with 「예매 안내 /
  // 확인하고 예매하기」 on top, the overlay said 회차 선택 중… and every click
  // below was swallowed. Clear it before each step, and keep clearing while
  // waiting, because a new round can raise it again.
  function clearScheduleNotice() {
    let cleared = false;
    try { cleared = !!dismissEntryNotice() || cleared; } catch (error) { /* keep going */ }
    try { cleared = dismissBookingNotices() || cleared; } catch (error) { /* keep going */ }
    return cleared;
  }

  // Every date button on the calendar that shows this day-of-month, best
  // first: the one under a heading that names the wanted month, then the
  // active swiper slide, then anything else. No dependence on the heading
  // format or on slide-index month arithmetic — that is what left 10/25
  // unfound while the screen sat on 10/17 (measured).
  function findDayButtons(wantedDate) {
    const day = String(Number(wantedDate.slice(6, 8)));
    const ym = wantedDate.slice(0, 6);
    const buttons = [...document.querySelectorAll(
      "[class*='EntCalendar'] button, [class*='Calendar'] button, [class*='swiper'] button, [role='gridcell'] button, td button"
    )].filter((btn) => {
      const num = ((btn.querySelector("[class*='number'], [class*='Number']") || btn).textContent || "").trim().replace(/\D/g, "");
      return num === day && !btn.disabled && !isSniperOverlay(btn);
    });
    const monthOf = (btn) => {
      const scope = btn.closest("[class*='swiper-slide'], [class*='EntCalendar'], [class*='Calendar']") || document;
      const heading = scope.querySelector("[class*='month'], [class*='Month']") || document.querySelector("[class*='EntCalendar_month']");
      const m = /(\d{4})\D+(\d{1,2})/.exec((heading && heading.textContent) || "");
      return m ? `${m[1]}${String(Number(m[2])).padStart(2, "0")}` : "";
    };
    const score = (btn) => {
      const month = monthOf(btn);
      const inActive = !!btn.closest(".swiper-slide-active, [class*='slide-active']");
      return (month === ym ? 4 : month ? 0 : 2) + (inActive ? 1 : 0);
    };
    return buttons.sort((x, y) => score(y) - score(x));
  }
  function isPressed(el) {
    return !!el && (el.getAttribute("aria-pressed") === "true" || el.getAttribute("aria-selected") === "true"
      || /selected|active|on\b/.test(String(el.className || "")));
  }
  // Clear anything modal that can sit over the schedule: 예매 안내, 예매대기,
  // sold-out notices, generic 확인/닫기 dialogs. Returns true if it pressed one.
  function clearScheduleModals() {
    let cleared = false;
    try { cleared = clearScheduleNotice() || cleared; } catch (error) { /* keep going */ }
    try { cleared = dismissBlockingDialogs() || cleared; } catch (error) { /* keep going */ }
    try {
      const dialogs = [...document.querySelectorAll("[role='dialog'], [class*='modal'], [class*='Modal'], [class*='popup'], [class*='Popup'], [class*='layer']")]
        .filter((d) => !isSniperOverlay(d) && d.offsetParent !== null);
      for (const dialog of dialogs) {
        const btn = [...dialog.querySelectorAll("button, a, [role='button']")]
          .find((el) => /^(확인|닫기|확인하고 예매하기|동의하고 예매하기|예매하기|계속)$/.test((el.textContent || "").replace(/\s+/g, "")));
        if (btn) { nativePress(btn); cleared = true; }
      }
    } catch (error) { /* a modal we cannot name still stops nothing below */ }
    return cleared;
  }
  function findNextButton() {
    return [...document.querySelectorAll("button, a, [role='button']")]
      .find((b) => /^(다음|변경하기)$/.test((b.textContent || "").replace(/\s+/g, "")) && !b.disabled && !isSniperOverlay(b)) || null;
  }
  async function chooseRoundOnSchedule(arm) {
    let wantedDate = String(arm.play_date || "").replace(/\D/g, "");
    const traceStart = Date.now();
    const st = (seatState.scheduleTrace = { wanted: wantedDate, clock: String(arm.play_time || ""), startedAt: traceStart });
    // Persisted: the seat page is a new document and would otherwise publish
    // no trace at all, hiding what the schedule step just did.
    const persist = () => { try { sessionStorage.setItem("nolsniper_schedule_trace", JSON.stringify(st)); } catch (error) { /* optional */ } };
    const mark = (k) => { st[k] = Date.now() - traceStart; persist(); };
    const adoptPage = (why) => {
      const active = adoptActiveSelection();
      if (active && active.play_date && active.play_date !== wantedDate) {
        st.result = why;
        updateOverlay(`${why === "date-switch-fallback" ? "날짜가 바뀌지 않아" : "고른 날짜가 달력에 없어"} 화면의 선택(${active.play_date})으로 진행합니다`, "warn");
        wantedDate = active.play_date;
        arm = { ...arm, play_date: active.play_date, play_time: active.play_time || arm.play_time };
        try { const saved = loadArmConfig(); if (saved) saveArmConfig({ ...saved, play_date: arm.play_date, play_time: arm.play_time }); rememberPendingRound(arm); } catch (error) { /* still proceed */ }
      }
    };
    clearScheduleModals();
    // 1. The date: by day number across every grid, at once. If the wanted
    //    month is not the one on screen, page toward it (bounded) first.
    let cell = null;
    if (wantedDate) {
      cell = scheduleDateCells().get(wantedDate) || findDayButtons(wantedDate)[0] || null;
      for (let hop = 0; !cell && hop < 4; hop += 1) {
        if (!pageCalendarToward(wantedDate)) break;
        await sleep(250);
        cell = scheduleDateCells().get(wantedDate) || findDayButtons(wantedDate)[0] || null;
      }
    }
    if (cell) {
      mark("dateFoundMs");
      if (!isPressed(cell)) {
        nativePress(cell); mark("dateClickedMs");
        // Verify briefly (React re-renders the time blocks on a real switch);
        // never idle on it — the page's selection is the fallback.
        const switchedBy = Date.now() + 1500;
        while (Date.now() < switchedBy && !isPressed(cell)) {
          await yieldFast(); await sleep(50);
          cell = scheduleDateCells().get(wantedDate) || findDayButtons(wantedDate)[0] || cell;
        }
        if (!isPressed(cell)) adoptPage("date-switch-fallback");
      }
    } else {
      adoptPage("date-not-on-calendar");
      if (st.result !== "date-not-on-calendar") {
        // Nothing selected on screen either: press 다음 anyway so the user
        // reaches the seat map rather than a frozen picker.
        st.result = "date-missing-advance";
      }
    }
    // 2. The time block — immediately.
    const wantedClock = clockLabel(arm.play_time);
    const wantedVariants = clockVariants(arm.play_time);
    const matchesClock = (b) => wantedVariants.some((v) => (b.innerText || b.textContent || "").replace(/\s+/g, "").includes(v));
    let picked = null;
    const timeBy = Date.now() + 1200;
    while (Date.now() < timeBy) {
      const blocks = [...document.querySelectorAll("button[class*='TimeBlock_timeButton'], button[class*='timeButton'], button[class*='TimeBlock']")].filter((b) => !b.disabled);
      if (blocks.length) {
        picked = (wantedClock && blocks.find(matchesClock)) || blocks.find(isPressed) || blocks[0];
        break;
      }
      clearScheduleModals(); await yieldFast(); await sleep(50);
    }
    if (picked) { if (!isPressed(picked)) nativePress(picked); mark("timePickedMs"); }
    else { st.result = st.result || "no-time-blocks"; }
    // 3. 다음 — immediately, then make sure we actually left. Up to three
    //    presses ~500ms apart with modals cleared in between; ~4.5s ceiling.
    for (let round = 0; round < 3; round += 1) {
      const next = findNextButton();
      if (next) { nativePress(next); if (st.nextClickedMs == null) mark("nextClickedMs"); }
      const leaveBy = Date.now() + 1500;
      while (Date.now() < leaveBy) {
        if (!scheduleStepVisible() && !onSchedulePage()) {
          mark("leftMs"); st.wantedFinal = wantedDate; st.result = st.result && st.result !== "date-missing-advance" ? st.result : "chose"; persist();
          return { chose: true, clock: wantedClock, date: wantedDate, adopted: st.result !== "chose" };
        }
        clearScheduleModals(); await yieldFast(); await sleep(60);
      }
    }
    st.result = "stuck-after-next";
    st.wantedFinal = wantedDate; persist();
    return { chose: false, reason: "일정 선택에서 넘어가지 못했습니다 — 예매 창에서 [다음]을 확인하세요" };
  }
  /**
   * The round the seat page is currently showing, as yyyyMMdd + HHmm.
   *
   * The header prints it as "2026.09.04(금) 7:30 PM", which is the only place
   * the chosen round appears once the schedule step is behind you.
   */
  function shownRoundOnSeatPage() {
    const text = (document.body && document.body.innerText) || "";
    const found = /(\d{4})\.(\d{2})\.(\d{2})\s*\([^)]*\)\s*(\d{1,2}):(\d{2})\s*(AM|PM)/i.exec(text);
    if (!found) return null;
    let hour = Number(found[4]) % 12;
    if (/PM/i.test(found[6])) hour += 12;
    return {
      play_date: `${found[1]}${found[2]}${found[3]}`,
      play_time: `${String(hour).padStart(2, "0")}${found[5]}`,
    };
  }

  /**
   * Make the seat map show the round that was armed, changing it if it does not.
   *
   * A booking session remembers the round it was last used for, so re-entering
   * lands straight on the seat map of the *previous* choice and never offers
   * 일정 선택 at all — measured, attempt 5: a future round was armed and the map
   * still showed the earlier one. 일정변경 is the only way back to the picker.
   */
  /**
   * Clear the 예매 안내 gate that some shows raise on every entry.
   *
   * This is the "last resort" the brief allows, and it is needed for a reason
   * that is not cosmetic: the notice is a modal *over* the seat map, so while it
   * is up 일정변경 cannot be clicked and the round can never be corrected —
   * measured, attempt 10. Pressing it opens the seat map; it buys nothing, and
   * payment stays manual. What it acknowledges is recorded so the panel can say
   * so rather than silently agreeing on the user's behalf.
   */
  function dismissEntryNotice() {
    const button = [...document.querySelectorAll("button")].find(
      (b) => /^(확인하고 예매하기|동의하고 예매하기)$/.test((b.textContent || "").trim())
             && !b.disabled && !isSniperOverlay(b)
    );
    if (!button) return null;
    const body = (document.body && document.body.innerText) || "";
    const nonRefundable = /취소\/환불이?\s*불가능|취소\/환불\s*기간이\s*지난/.test(body);
    button.click();
    armState.noticeAcknowledged = {
      at: Date.now(),
      nonRefundable,
      // Enough for the panel to repeat back what was on screen.
      text: (body.match(/취소\/환불\s*안내[\s\S]{0,160}|예매\s*안내[\s\S]{0,160}/) || [""])[0]
        .replace(/\s+/g, " ").trim().slice(0, 180),
    };
    return armState.noticeAcknowledged;
  }

  // The control that reopens 일정 선택 from the seat map. The label has been
  // seen as "일정변경", "일정 변경" and with an icon or newline inside it, on a
  // <button>, an <a> and a bare <span>/<div> with a click handler. Match the
  // normalised text, innermost element first, and never our own overlay.
  const SCHEDULE_CHANGE_LABEL = /^(일정\s*변경|일정\s*선택|날짜\s*변경|회차\s*변경|다른\s*(회차|일정|날짜)(\s*선택)?|일정\s*다시\s*선택)$/;
  function findScheduleChangeControl() {
    const candidates = [...document.querySelectorAll("button,[role=button],a,span,div,li,p,label")]
      .filter((el) => !isSniperOverlay(el))
      .filter((el) => {
        const text = (el.textContent || "").replace(/\s+/g, " ").trim();
        return text.length > 0 && text.length <= 12 && SCHEDULE_CHANGE_LABEL.test(text);
      });
    if (!candidates.length) return null;
    // Innermost: a candidate that contains no other candidate.
    const inner = candidates.filter((el) => !candidates.some((other) => other !== el && el.contains(other)));
    const pick = inner[0] || candidates[0];
    // Click the nearest clickable ancestor if the text sits in a bare span.
    return pick.closest("button,[role=button],a") || pick;
  }

  async function ensureArmedRound(pending) {
    const wantDate = String(pending.play_date || "").replace(/\D/g, "");
    const wantTime = String(pending.play_time || "").replace(/\D/g, "");
    if (!wantDate) return { ok: false, reason: "회차가 선택되지 않았습니다" };
    // Another show's arm must not try to re-date this one's map — measured
    // 2026-09-04 12:16: the arm named 디어 에반 핸슨 while the user had walked
    // into 대니 구's single-round map, and the check demanded 일정변경 there.
    let here = "";
    try { here = String((getInitData()?.goods?.goodsCode) || ""); } catch (error) { here = ""; }
    if (here && pending.goods_code && here !== String(pending.goods_code)) {
      return { ok: true, unchecked: true, otherShow: here };
    }

    const deadline = Date.now() + SCHEDULE_STEP_TIMEOUT_MS;
    // Before anything else: the notice is a modal over the map, and nothing
    // underneath it is clickable while it stands.
    dismissEntryNotice();
    await sleep(400);
    let shown = null;
    while (Date.now() < deadline) {
      shown = shownRoundOnSeatPage();
      if (shown) break;
      await sleep(200);
    }
    // Nothing to compare against: leave the page alone rather than clicking
    // 일정변경 on a map that may already be the right one.
    if (!shown) return { ok: true, unchecked: true };
    const sameDate = shown.play_date === wantDate;
    const sameTime = !wantTime || shown.play_time === wantTime;
    if (sameDate && sameTime) return { ok: true, matched: true, shown };

    // A pending captcha is a modal over the map: 일정변경 is visible but not
    // reachable, so a click here is swallowed and the round silently stays
    // wrong. Measured. Say so instead of trying.
    if (captchaPresent() || /화면의 문자를 입력|보안문자/.test((document.body && document.body.innerText) || "")) {
      // The user types the captcha; we wait, then continue with the change.
      updateOverlay(`보안문자 입력 후 회차를 ${wantDate} 로 바꿉니다 — 지금은 ${shown.play_date} 회차입니다`, "warn");
      const cleared = await waitForCaptchaClear();
      if (!cleared) {
        return {
          ok: false, shown,
          reason: `보안문자 입력 후 회차를 바꿀 수 있습니다 — 지금은 ${shown.play_date} 회차입니다`,
          captcha: true,
        };
      }
      await sleep(300);
    }
    const change = findScheduleChangeControl();
    if (!change) {
      return {
        ok: false, shown,
        reason: `일정변경 버튼을 찾지 못했습니다 — 화면은 ${shown.play_date} ${shown.play_time || ""}, 고른 회차는 ${wantDate} ${wantTime || ""} 입니다. 예매 창에서 직접 바꿔주세요.`.replace(/\s+,/g, ","),
      };
    }
    updateOverlay("회차가 달라 일정을 바꿉니다…", "warn");
    change.click();

    while (Date.now() < deadline) {
      if (scheduleStepVisible() || onSchedulePage()) {
        const result = await chooseRoundOnSchedule(pending);
        if (!result.chose) return { ok: false, reason: result.reason };
        // The new round can raise its own notice; the map stays covered until
        // that one is cleared too.
        await sleep(1200);
        dismissEntryNotice();
        // Verify, do not assume. An earlier build printed 회차 변경 완료 over a
        // header that had not moved, which is the worst possible outcome: the
        // user picks seats for the wrong night believing the app agreed.
        await sleep(1200);
        const after = shownRoundOnSeatPage();
        const okDate = after && after.play_date === wantDate;
        const okTime = after && (!wantTime || after.play_time === wantTime);
        if (okDate && okTime) return { ok: true, changed: true, shown: after };
        return {
          ok: false, shown: after,
          reason: `회차를 바꾸지 못했습니다 — 화면은 ${after ? after.play_date : "?"}, `
                  + `고른 회차는 ${wantDate} 입니다. 예매 창에서 [일정변경]으로 직접 골라주세요.`,
        };
      }
      await sleep(200);
    }
    return { ok: false, reason: "일정 선택으로 돌아가지 못했습니다", shown };
  }

  /**
   * Mint, spend, go. The whole entry, from the origin it works on.
   */
  // Secure-url, retried through the open window with a reason for each stop.
  //
  //   pre-open  (UnableReservationTime) → keep asking, fast near the open
  //   auth      (401 / 로그인)            → stop: the session is gone
  //   block     (AccessDenied_Blacklist)  → stop: never retry a block
  //   other                               → a few more tries, then stop
  //
  // Returns the entry result, or null when the window closed without a queue
  // URL so the caller may fall back to the page's own 예매하기.
  const SECURE_URL_WINDOW_MS = 15000;
  const SECURE_URL_MAX_ATTEMPTS = 120;
  const SECURE_URL_OTHER_ERROR_LIMIT = 5;
  function isAuthError(error) {
    return /HTTP 401|로그인|logout|Unauthorized|자동 로그아웃/i.test(String(error && error.message ? error.message : error));
  }
  // The credential, minted once and reused. Each shot of the burst used to
  // start with its own member-info GET, so every attempt cost two round trips
  // and the shot meant for 0ms landed one RTT late. The signature carries its
  // issue time; it is re-minted when it ages past SIGNATURE_MAX_AGE_MS, when
  // the gate refuses it, or when the caller asks (maxAgeMs: 0).
  async function mintMemberInfo(arm, { maxAgeMs = SIGNATURE_MAX_AGE_MS } = {}) {
    const have = armState.memberInfo;
    const goods = String(arm.goods_code || "");
    if (have && have.goods === goods && Date.now() - have.at < maxAgeMs) return have.data;
    const startedPerf = performance.now();
    const data = await fetchMemberInfo(arm);
    armState.memberInfo = { goods, at: Date.now(), data, ms: Math.round(performance.now() - startedPerf) };
    armState.premintMs = armState.memberInfo.ms;
    return data;
  }
  async function enterViaSecureUrlWithRetries(arm, { windowMs = SECURE_URL_WINDOW_MS } = {}) {
    const target = armTargetUnix(arm) || serverTimeUnix();
    const giveUpAt = Math.max(target, serverTimeUnix()) + windowMs / 1000;
    let attempts = 0;
    let notOpen = 0;
    let others = 0;
    let lastError = null;
    let remintedForAuth = false;
    armState.waitingLog = [];
    while (attempts < SECURE_URL_MAX_ATTEMPTS && serverTimeUnix() < giveUpAt) {
      attempts += 1;
      const offsetMs = Math.round((serverTimeUnix() - target) * 1000);
      const startedPerf = performance.now();
      try {
        // Pre-minted by the scheduler; a cache miss here still mints once.
        const memberInfo = await mintMemberInfo(arm);
        const result = await enterViaSecureUrl(arm, memberInfo);
        noteWaitingAttempt(offsetMs, "대기열 URL", performance.now() - startedPerf);
        armState.waitingAttempts = attempts;
        return result;
      } catch (error) {
        lastError = error;
        const reason = String(error && error.message ? error.message : error);
        noteWaitingAttempt(offsetMs, `오류 ${reason.slice(0, 40)}`, performance.now() - startedPerf);
        armState.lastError = reason;
        if (error && error.blocked) throw error;
        if (isAuthError(error)) {
          // Once: a signature the gate no longer accepts reads like an auth
          // failure, and a fresh one settles which of the two it was.
          if (!remintedForAuth) {
            remintedForAuth = true;
            armState.memberInfo = null;
            continue;
          }
          const message = "로그인이 풀렸습니다 — 예매 창에서 다시 로그인한 뒤 눌러주세요 (" + reason.slice(0, 60) + ")";
          updateOverlay(message, "warn");
          armState.lastError = message;
          throw new Error(message);
        }
        if (error && error.notOpenYet) {
          notOpen += 1;
          others = 0;
          if (notOpen % 5 === 1) {
            updateOverlay(`아직 오픈 전 — 대기열 요청 ${attempts}회 (${offsetMs >= 0 ? "+" : ""}${offsetMs}ms)`, "info");
          }
        } else {
          // Not "not open yet": whatever the gate disliked, a fresh credential
          // is the cheapest thing to change before the next shot.
          armState.memberInfo = null;
          others += 1;
          if (others >= SECURE_URL_OTHER_ERROR_LIMIT) {
            const message = `대기열 요청이 ${others}회 연속 실패했습니다 — ${reason.slice(0, 80)}`;
            updateOverlay(message, "warn");
            armState.lastError = message;
            throw new Error(message);
          }
        }
      }
      // Burst: the target already sits entry_offset_ms *before* the open. If
      // the early shot answered "not open yet" and the published open is still
      // ahead, the next shot goes out exactly at the open (spin-tight), so
      // there is always one at -lead and one at 0ms. After that, ease off.
      const openUnix = target - (Number(arm.entry_offset_ms) || 0) / 1000;
      const untilOpenMs = (openUnix - serverTimeUnix()) * 1000;
      if (lastError && lastError.notOpenYet && untilOpenMs > 0 && untilOpenMs < 2000) {
        await waitUntilServerUnix(openUnix);
        continue;
      }
      // Tight while the open is within reach, easing off after it.
      const interval = offsetMs < 1500 ? SECURE_URL_BURST_MS : offsetMs < 5000 ? 150 : 300;
      await sleep(Math.max(0, interval - (performance.now() - startedPerf)));
    }
    armState.waitingAttempts = attempts;
    const message = lastError && lastError.notOpenYet
      ? `오픈 후 ${Math.round(windowMs / 1000)}초 동안 대기열을 열어주지 않았습니다 (${attempts}회) — 예매 창에서 [예매하기]를 직접 눌러주세요`
      : `대기열 URL을 받지 못했습니다 (${attempts}회) — ${String(lastError && lastError.message || lastError || "").slice(0, 80)}`;
    updateOverlay(message, "warn");
    armState.lastError = message;
    return null;
  }

  async function enterViaSecureUrl(arm, memberInfo = null) {
    const info = memberInfo || (await mintMemberInfo(arm));
    const waitingUrl = await fetchSecureUrl(arm, info);
    armState.enteredVia = "secure-url";
    armState.route = "secure-url";
    armState.waitingUrl = waitingUrl;
    rememberQueueHost(waitingUrl);
    // Before navigating, not after: this document is about to be replaced, and
    // the round the user chose has to survive that to be usable on 일정 선택.
    rememberPendingRound(arm);
    // Line up and read the rank from here, now — not after handing the key to
    // the waiting room and paying its boot before it does exactly the same.
    const direct = await enterQueueDirect(waitingUrl);
    if (direct.navigated) return { waitingUrl, secureUrl: true, ...direct };
    // The waiting page lines the key up again (line-up is not idempotent), so
    // after a successful line-up this hands back the place we held. Only
    // reached when rank itself gave up; say so where the panel can read it.
    updateOverlay(`대기열 진입${direct.outcome ? ` (${direct.outcome})` : ""}`, direct.userSeq !== null ? "warn" : "ok");
    location.href = waitingUrl;
    return { waitingUrl, secureUrl: true, ...direct };
  }

  // ── The waiting room's own two calls, made from the goods page ────────────
  function queueKeyFrom(waitingUrl) {
    try { return new URL(String(waitingUrl)).searchParams.get("key") || ""; } catch (error) { return ""; }
  }
  async function fetchQueueJson(path, { method = "GET", body = null, timeoutMs = 3000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${ENT_WAITING_ORIGIN}${path}`, {
        method, credentials: "include", signal: controller.signal,
        headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      const data = await response.json().catch(() => ({}));
      return { status: response.status, data: data && typeof data === "object" ? data : {} };
    } finally {
      clearTimeout(timer);
    }
  }
  const postLineUp = (key) => fetchQueueJson(LINE_UP_PATH, { method: "POST", body: { key } });
  const fetchRank = (waitingId) => fetchQueueJson(`${RANK_PATH}?waitingId=${encodeURIComponent(waitingId)}`, { timeoutMs: 5000 });
  // Mirrors core/entry.py decide_line_up / decide_rank; the Python tests are
  // the reference for these shapes.
  function decideLineUp(data, status = 200) {
    const d = data && typeof data === "object" ? data : {};
    const error = String(d.error || "").trim();
    const waitingId = String(d.waitingId || "").trim();
    if (status !== 200 || error || !waitingId) {
      return { action: "fallback", reason: error || (status !== 200 ? `HTTP ${status}` : "no waitingId"), waitingId: "", userSeq: null, exist: Boolean(d.exist) };
    }
    let seq = d.userSeq;
    if ((seq === null || seq === undefined) && waitingId.split(":").length >= 3) {
      const n = Number(waitingId.split(":")[2]);
      seq = Number.isFinite(n) ? n : null;
    }
    return { action: "poll", reason: "", waitingId, userSeq: seq ?? null, exist: Boolean(d.exist) };
  }
  function decideRank(data, status = 200) {
    const d = data && typeof data === "object" ? data : {};
    const error = String(d.error || "").trim();
    const out = { action: "poll", reason: "", url: "", myRank: d.myRank ?? null, totalRank: d.totalRank ?? null };
    if (status !== 200 || error) return { ...out, action: "fallback", reason: error || `HTTP ${status}` };
    const url = String(d.oneStopUrl || "").trim();
    if (/^https?:\/\//i.test(url)) return { ...out, action: "go", url };
    if (d.myRank === -1 && d.totalRank === -1) return { ...out, action: "fallback", reason: "ExpiredSession" };
    if (d.myRank === 0 && !String(d.sessionId || "").trim()) return { ...out, action: "fallback", reason: "ExpiredExistedSession" };
    return out;
  }
  // The place in line is assigned by line-up, and the turn is read from rank:
  // both are one small request, and the waiting page makes them only after it
  // has booted (~1.3MB of script) and then every 2–3s. Done here instead, the
  // key goes from secure-url to a queue position in one more round trip, and
  // the turn is noticed within RANK_POLL_MS. Anything unexpected falls back
  // to navigating to the waiting page exactly as before.
  async function enterQueueDirect(waitingUrl) {
    const gen = armState.entryGen || 0;
    const startedPerf = performance.now();
    const report = { key: false, lineUpMs: null, rankMs: null, totalMs: null, userSeq: null, exist: false,
                     myRank: null, totalRank: null, polls: 0, outcome: "" };
    armState.lineUp = report;
    const key = queueKeyFrom(waitingUrl);
    report.key = Boolean(key);
    if (!key) { report.outcome = "no-key"; return { navigated: false, ...report }; }
    let lu;
    try {
      lu = await postLineUp(key);
    } catch (error) {
      report.outcome = `line-up: ${String(error).slice(0, 80)}`;
      return { navigated: false, ...report };
    }
    report.lineUpMs = Math.round(performance.now() - startedPerf);
    const lined = decideLineUp(lu.data, lu.status);
    report.userSeq = lined.userSeq;
    report.exist = lined.exist;
    if (lined.action !== "poll") { report.outcome = `line-up: ${lined.reason}`; return { navigated: false, ...report }; }
    armState.route = "secure-url+line-up";
    // In line is entered: the panel must read 진입 중, not 오픈 대기 중, for
    // however long the rank poll runs before the turn.
    armState.fired = true;
    updateOverlay(`줄서기 완료 · 순번 ${report.userSeq ?? "?"} · ${report.lineUpMs}ms`, "ok");
    const deadline = performance.now() + RANK_POLL_WINDOW_MS;
    let failures = 0;
    while (performance.now() < deadline) {
      if ((armState.entryGen || 0) !== gen) { report.outcome = "stopped"; return { navigated: false, stopped: true, ...report }; }
      const tickPerf = performance.now();
      let rk;
      try {
        rk = await fetchRank(lined.waitingId);
        failures = 0;
      } catch (error) {
        failures += 1;
        if (failures >= 5) { report.outcome = `rank: ${String(error).slice(0, 80)}`; return { navigated: false, ...report }; }
        await sleep(RANK_POLL_MS);
        continue;
      }
      report.polls += 1;
      if (report.rankMs === null) report.rankMs = Math.round(performance.now() - startedPerf);
      let next = decideRank(rk.data, rk.status);
      // The session that rank reports is created ~1.7s after line-up; an
      // "expired existing session" reading inside the grace window is that
      // creation still in progress, not an expiry.
      if (next.action === "fallback" && next.reason === "ExpiredExistedSession"
          && performance.now() - startedPerf < RANK_SESSION_GRACE_MS) {
        next = { ...next, action: "poll", reason: "" };
      }
      report.myRank = next.myRank;
      report.totalRank = next.totalRank;
      if (next.action === "go") {
        report.totalMs = Math.round(performance.now() - startedPerf);
        report.outcome = "onestop";
        updateOverlay(`대기열 통과 · 순번 ${report.userSeq ?? "?"} · ${report.totalMs}ms`, "ok");
        location.href = next.url;
        return { navigated: true, oneStopUrl: next.url, ...report };
      }
      if (next.action === "fallback") { report.outcome = `rank: ${next.reason}`; return { navigated: false, ...report }; }
      updateOverlayIfChanged(`대기열 ${next.myRank ?? "?"}번 · 전체 ${next.totalRank ?? "?"} · 순번 조회 중`, "info");
      await sleep(Math.max(0, RANK_POLL_MS - (performance.now() - tickPerf)));
    }
    report.outcome = "rank window expired";
    return { navigated: false, ...report };
  }

  // Absolute, not relative. On the NOL product page — which is where the user
  // actually stands when they pick a round — a relative path resolves to
  // nol.yanolja.com and 404s, which is why the picker came up empty on a show
  // that was plainly on sale. Measured: the absolute URL answers 200 with the
  // full round list cross-origin, and needs no cookies to do it.
  const GOODS_INFO_URL = `${GATE_ORIGIN}/api/ticket/v2/reserve-gate/goods-info`;

  // The 일정 the panel draws its picker from, cached because readShowCatalog is
  // polled four times a second and this is a network call. Keyed by goods+place
  // so switching shows refetches rather than showing the previous show's rounds.
  const scheduleCache = { key: "", value: null, fetching: false, at: 0 };
  const SCHEDULE_TTL_MS = 120000;

  /**
   * Every round of the show, from the same endpoint the official gate uses.
   *
   * This is the authoritative list: the `playSeq` values here are exactly the
   * ones secure-url accepts, which is why the picker is built from it rather
   * than from anything scraped off the product page. Requires placeCode —
   * measured, a request without it is a 400.
   */
  async function fetchSchedule(goodsCode, placeCode, bizCode) {
    const params = new URLSearchParams({
      bizCode: String(bizCode || "61776"),
      goodsCode: String(goodsCode || ""),
      lang: "ko",
      placeCode: String(placeCode || ""),
    });
    const response = await fetch(`${GOODS_INFO_URL}?${params}`, { credentials: "include" });
    if (!response.ok) throw new Error(`goods-info HTTP ${response.status}`);
    const data = await response.json();
    return {
      goods_code: String(data.goodsCode || goodsCode || ""),
      goods_name: String(data.goodsName || ""),
      place_name: String(data.placeName || ""),
      // yyyyMMddHHmmss, KST. The panel's 티켓 오픈 comes from here rather than
      // from anything the user typed.
      ticket_open_date: String(data.ticketOpenDate || ""),
      rounds: bookableRounds(data.playSeqList || []),
    };
  }

  /**
   * Only the rounds a user can still enter, newest sale window first.
   *
   * A round whose sale has closed still posts a perfectly good secure-url and
   * still returns a queue URL — and then onestop lands on 일정 선택 instead of
   * the seat map, because the round it was handed is not sellable. Measured,
   * attempt 2, with 겨울왕국 회차 024: entry "succeeded" and the user was left
   * on the schedule picker. Offering a closed round is therefore not a cosmetic
   * problem; it is the bug.
   */
  function bookableRounds(rows) {
    // yyyyMMddHHmmss in KST, which is what every time in this payload is.
    const now = new Date(Date.now() + 9 * 3600 * 1000)
      .toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
    return (rows || [])
      .map((row) => ({
        play_seq: String(row.playSeq || ""),
        play_date: String(row.playDate || ""),
        play_time: String(row.playTime || ""),
        day_of_week: String(row.dayOfWeek || ""),
        sale_open_time: String(row.saleOpenTime || ""),
        sale_close_time: String(row.saleCloseTime || ""),
      }))
      .filter((row) => {
        if (!row.play_seq || !row.play_date) return false;
        // Closed already. A missing close time is treated as open rather than
        // hiding a round the site would have sold.
        if (row.sale_close_time && row.sale_close_time <= now) return false;
        return true;
      })
      .sort((a, b) => (a.play_date + a.play_time).localeCompare(b.play_date + b.play_time));
  }

  /**
   * The cached schedule, kicking off a refresh when it is missing or stale.
   *
   * Synchronous by design: it is read from the 400ms poll, which cannot await.
   * The first poll after a show changes returns null and the next one has it.
   */
  /**
   * The venue code, dug out of the page when no one has told us one.
   *
   * goods-info refuses without it (400), and on a NOL product page neither the
   * arm nor the seat catalog has it yet — but the page's own payload does.
   */
  function placeCodeFromPage() {
    // NOL-native products carry L-prefixed codes (L0000001), not only digits.
    try {
      const html = document.documentElement.innerHTML;
      const found = html.match(/placeCode\\?":\\?"([A-Z]?\d{6,})/) || html.match(/"placeCode":"([A-Z]?\d{6,})"/);
      return found ? found[1] : "";
    } catch {
      return "";
    }
  }

  // What the panel says is on screen (goods + place), handed over the bridge
  // the moment it looks a show up — so rounds can load before any arm exists.
  function loadShowHint() {
    try {
      const raw = localStorage.getItem("nolsniper_show_v1");
      const value = raw ? JSON.parse(raw) : null;
      return value && typeof value === "object" ? value : null;
    } catch {
      return null;
    }
  }
  // The round with this playSeq, from the show's own schedule — the truth for
  // which date a seq means. Corrects a play_date carried on a stale, cross-
  // origin arm (nol vs interpark localStorage): the seq is authoritative, the
  // date only labels it. Returns null when the schedule is not cached yet.
  function roundBySeq(goodsCode, placeCode, seq, bizCode) {
    const schedule = scheduleFor(goodsCode, placeCode, bizCode);
    const rows = (schedule && schedule.rounds) || [];
    const want = String(seq || "");
    return want ? rows.find((r) => String(r.play_seq) === want) || null : null;
  }
  function scheduleFor(goodsCode, placeCode, bizCode) {
    const key = `${goodsCode}|${placeCode}`;
    if (!goodsCode || !placeCode) return null;
    const fresh = scheduleCache.key === key && Date.now() - scheduleCache.at < SCHEDULE_TTL_MS;
    if (fresh) return scheduleCache.value;
    if (scheduleCache.fetching) return scheduleCache.key === key ? scheduleCache.value : null;
    scheduleCache.fetching = true;
    fetchSchedule(goodsCode, placeCode, bizCode)
      .then((value) => {
        scheduleCache.key = key;
        scheduleCache.value = value;
        scheduleCache.at = Date.now();
      })
      .catch((error) => {
        log("goods-info", error);
        // Remember the failure against this key so a dead show does not refetch
        // four times a second forever.
        scheduleCache.key = key;
        scheduleCache.value = null;
        scheduleCache.at = Date.now();
      })
      .finally(() => {
        scheduleCache.fetching = false;
      });
    return null;
  }

  function openBookSession(arm) {
    const form = document.createElement("form");
    form.method = "post";
    form.action = "https://poticket.interpark.com/Book/BookSession.asp";
    form.target = "_self";
    const fields = {
      GroupCode: arm.goods_code,
      Tiki: "N",
      Point: "N",
      PlayDate: arm.play_date,
      PlaySeq: arm.play_seq || "001",
    };
    for (const [name, value] of Object.entries(fields)) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = String(value);
      form.appendChild(input);
    }
    document.body.appendChild(form);
    form.submit();
    return { bookSession: true, fields };
  }

  const BOOK_BUTTON_TEXT = /^예매하기$|^본인인증 후 예매하기$/;

  /**
   * The page's own 예매하기, found whether or not it is currently pressable.
   *
   * Deliberately *not* clickFirstMatching: that skips `disabled`, which is the
   * one state this has to be able to see. "The button is there but not live
   * yet" and "there is no button at all" are opposite diagnoses — the first is
   * normal before an open, the second means the login or 본인인증 is wrong —
   * and collapsing them is why a run before the open could only ever report the
   * second.
   */
  function findBookButton() {
    const nodes = [
      ...document.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit]"),
    ];
    return (
      nodes.find((el) => {
        if (isSniperOverlay(el)) return false;
        const text = (el.value || el.textContent || "").trim();
        return BOOK_BUTTON_TEXT.test(text);
      }) || null
    );
  }

  function bookButtonPressable(el) {
    if (!el || !isVisible(el)) return false;
    if (el.disabled) return false;
    if (el.getAttribute("aria-disabled") === "true") return false;
    if (el.getAttribute("data-disabled") === "true") return false;
    try {
      const style =
        typeof window.getComputedStyle === "function" ? window.getComputedStyle(el) : null;
      if (style && style.pointerEvents === "none") return false;
    } catch {
      /* a detached node has no computed style; treat it as pressable */
    }
    return true;
  }

  /**
   * Strip the client-side gate and click anyway. Last resort only.
   *
   * Attributes and inline pointer-events, never classes: the class names are
   * NOL's own and change without notice, and removing one can restyle the
   * button into something the page's handler no longer recognises. The
   * attributes are what actually stop the event.
   */
  function unlockBookButton(el) {
    for (const node of [el, el.closest?.("button, a, [role=button]")].filter(Boolean)) {
      try {
        node.disabled = false;
        node.removeAttribute("disabled");
        node.removeAttribute("aria-disabled");
        node.removeAttribute("data-disabled");
        node.style.pointerEvents = "auto";
      } catch {
        /* one unwritable node must not stop the others */
      }
    }
  }

  const CLICK_LOG_LIMIT = 40;

  function noteClickAttempt(offsetMs, state) {
    const log = armState.clickLog;
    if (!log) return;
    const last = log[log.length - 1];
    // One row per *change*, not per poll. At 15ms this would otherwise be 500
    // identical "버튼 비활성" lines, burying the only row that matters.
    if (last && last.state === state) {
      last.offsetMs = offsetMs;
      last.repeats = (last.repeats || 1) + 1;
      return;
    }
    log.push({ offsetMs, state });
    if (log.length > CLICK_LOG_LIMIT) log.splice(0, log.length - CLICK_LOG_LIMIT);
  }

  /**
   * Press the page's own 예매하기, across the open rather than at one instant.
   *
   * This is the route that actually runs on a NOL product page, because the
   * queue API sends no CORS header to that origin — see waitingApiUsableHere().
   * It used to be one click at exactly T against a finder that ignored disabled
   * nodes, so if NOL had not yet enabled the button (which is the ordinary
   * case: the backend opens a beat late, and the page needs another beat to
   * notice) there was nothing to click and the entry reported a missing button.
   *
   * Now it watches from ENTRY_CLICK_LEAD_MS before the target and clicks the
   * moment the button goes live, recording each change of state against the
   * target so the panel can show when it actually happened.
   */
  async function enterFromNolPage(arm) {
    const target = armTargetUnix(arm);
    // Start watching early. Nothing is clicked before the button is live, so
    // being early costs nothing and being late costs the open.
    await waitUntilServerUnix(target - ENTRY_CLICK_LEAD_MS / 1000);

    // Measured from whichever is later. Called on the fallback path the queue
    // API has often already spent its own 15s window, and a deadline anchored
    // to the target alone would have expired before the loop began — i.e. the
    // route that actually works would get zero attempts.
    const giveUpAt = Math.max(target, serverTimeUnix()) + ENTRY_CLICK_WINDOW_MS / 1000;
    const forceAt = target + ENTRY_FORCE_AFTER_MS / 1000;
    armState.clickLog = [];
    let tries = 0;
    let sawButton = false;

    while (serverTimeUnix() < giveUpAt) {
      tries += 1;
      const offsetMs = Math.round((serverTimeUnix() - target) * 1000);
      const button = findBookButton();

      if (!button) {
        noteClickAttempt(offsetMs, "missing");
      } else if (bookButtonPressable(button)) {
        sawButton = true;
        noteClickAttempt(offsetMs, "clicked");
        armState.clickTries = tries;
        armState.clickLatenessMs = offsetMs;
        forceClick(button);
        return { clicked: true, route: "dom-click", offsetMs, tries, ...(await confirmBookModal()) };
      } else if (!isVisible(button)) {
        sawButton = true;
        noteClickAttempt(offsetMs, "hidden");
      } else {
        sawButton = true;
        noteClickAttempt(offsetMs, "disabled");
        if (serverTimeUnix() >= forceAt) {
          noteClickAttempt(offsetMs, "forced");
          armState.clickTries = tries;
          armState.clickLatenessMs = offsetMs;
          unlockBookButton(button);
          forceClick(button);
          return {
            clicked: true,
            route: "dom-click-forced",
            forced: true,
            offsetMs,
            tries,
            ...(await confirmBookModal()),
          };
        }
      }

      await sleep(ENTRY_CLICK_POLL_MS);
    }

    armState.clickTries = tries;
    if (arm.place_code) {
      location.href = buildSsoUrl(arm);
      return { sso: true, route: "sso-gate", tries };
    }
    throw new Error(
      sawButton
        ? `예매하기 버튼이 ${Math.round(ENTRY_CLICK_WINDOW_MS / 1000)}초 동안 활성화되지 않았습니다. 공연이 열렸는지 확인하세요.`
        : "NOL 예매하기 버튼을 찾지 못했습니다. 로그인·본인인증을 확인하세요.",
    );
  }

  /**
   * The 예매하기 inside the modal the first click raises, if one appears.
   *
   * Polled rather than slept through: the old fixed 250ms was a guess that was
   * simultaneously too long on a fast page — 250ms of an open spent waiting for
   * something already on screen — and too short on a slow one, where the modal
   * arrived after we had stopped looking and the entry stalled there.
   */
  async function confirmBookModal(waitMs = ENTRY_MODAL_WAIT_MS) {
    const deadline = performance.now() + waitMs;
    while (performance.now() < deadline) {
      const modalBook = [...document.querySelectorAll("button, a")].find(
        (el) =>
          !isSniperOverlay(el) &&
          (el.getAttribute("data-testid") === "modal-booking-button" ||
            /^예매하기$/.test((el.textContent || "").trim())) &&
          isVisible(el),
      );
      if (modalBook) {
        forceClick(modalBook);
        return { modal: true };
      }
      await sleep(30);
    }
    // No modal is a perfectly ordinary outcome — many shows go straight
    // through — so this is not an error, only a fact worth reporting.
    return { modal: false };
  }

  /**
   * Can this origin even read the queue endpoint?
   *
   * Measured against the live endpoint, same goods code, three origins:
   *
   *   Origin: https://tickets.interpark.com  -> 401 + access-control-allow-origin
   *                                             + access-control-allow-credentials
   *   Origin: https://nol.yanolja.com        -> 403, no CORS header at all
   *   Origin: https://poticket.interpark.com -> 403, no CORS header at all
   *
   * (The 401 is only my probe having no login cookie; the page has one. What
   * matters is the header.)
   *
   * So from a NOL product page the browser refuses to hand the response to the
   * page, and every attempt lands as `TypeError: Load failed`. This is not
   * slowness and retrying cannot fix it: measured on a live arm, 215 attempts
   * across the whole 15-second window, every single one dead, and only then did
   * the run fall through to the page's own 예매하기 — which is what actually got
   * in. Fifteen seconds of the open spent on a request the browser was never
   * going to complete.
   *
   * The burst is kept exactly where it works. This only stops it being fired
   * somewhere it provably cannot.
   */
  const WAITING_API_ORIGINS = new Set([GATE_ORIGIN]);

  function waitingApiUsableHere() {
    return WAITING_API_ORIGINS.has(location.origin);
  }

  // A failure that means "the browser never completed this request" rather than
  // "the server answered something we did not like". fetchWaitingUrl turns every
  // HTTP answer into an Error carrying its status, so anything that still looks
  // like a bare network TypeError got no answer at all.
  function isUnreachableError(error) {
    return (
      error instanceof TypeError ||
      /Load failed|Failed to fetch|NetworkError|ERR_/i.test(String(error?.message || error))
    );
  }

  // How many answerless attempts in a row prove the endpoint is not reachable
  // from here. A dropped packet or two is exactly what the burst exists to ride
  // out; six in a row with nothing in between is a wall, not weather.
  const WAITING_UNREACHABLE_STREAK = 6;
  const WAITING_AUTH_STREAK = 3;
  const WAITING_MAX_ATTEMPTS = 150;

  // Terminal answers from the waiting API — retrying these is pointless.
  const WAITING_TERMINAL = /^(NP|BL)$/;

  function isUsableWaitingAnswer(value) {
    return value === "N" || (typeof value === "string" && /^https?:\/\//i.test(value));
  }

  /**
   * Repeatedly ask for a queue slot across the open boundary.
   *
   * A single perfectly-timed request is fragile: a clock error of a few tens of
   * milliseconds, one dropped packet, or the backend flipping to "open" a beat
   * late all cost the slot. Starting slightly early and retrying at roughly one
   * round-trip interval means the first request the server is willing to accept
   * is ours, without depending on hitting one exact instant.
   */
  // When the scheduler should stop waiting: early by exactly the lead the
  // request loop is built to use.
  function armEntryStartUnix(arm) {
    const target = armTargetUnix(arm);
    if (!Number.isFinite(target) || target <= 0) return null;
    return target - ENTRY_LEAD_MS / 1000;
  }

  /**
   * How hard to ask, and when.
   *
   * Measured: the queue endpoint answers in 11ms on a warm connection, and the
   * old flat 80ms interval left ~69ms of every cycle idle — so the show could
   * open and we would not notice for up to 80ms, 40ms on average. But asking
   * hard for the whole 15s window is 50 requests a second against the gateway
   * that answers GATEWAY_ABUSE_BLOCKED with a ~165s lockout, at the one moment
   * a lockout cannot be recovered from.
   *
   * So the density goes where it buys something. Before the open the answer
   * cannot be yes, and those requests exist only to keep the connection warm
   * and prove the session is good while there is still time to react.
   */
  const WAITING_POLL_SHAPE = [
    // [from ms relative to target, until ms, interval ms]
    [-Infinity, -100, 100],   // can't succeed yet — stay cheap
    [-100, 600, 20],          // the window that decides the position
    [600, Infinity, 80],      // it did not open on time; settle down
  ];

  function waitingIntervalAt(offsetMs, shape = WAITING_POLL_SHAPE) {
    for (const [from, until, interval] of shape) {
      if (offsetMs >= from && offsetMs < until) return interval;
    }
    return shape[shape.length - 1][2];
  }

  /**
   * Repeatedly ask for a queue slot across the open boundary.
   *
   * A single perfectly-timed request is fragile: a clock error of a few tens of
   * milliseconds, one dropped packet, or the backend flipping to "open" a beat
   * late all cost the slot. Starting early and retrying means the first request
   * the server is willing to accept is ours, without depending on hitting one
   * exact instant.
   *
   * Every attempt is recorded with its offset from the target, because what
   * this endpoint returns *before* a show opens has never been observed. If it
   * hands out a queue URL early, then arriving at T is already too late and the
   * whole strategy moves earlier — and the log is what settles that rather than
   * another guess.
   */
  async function acquireWaitingUrl(arm, { leadMs = ENTRY_LEAD_MS, windowMs = 15000, shape } = {}) {
    const target = armTargetUnix(arm) || serverTimeUnix();
    const startAt = target - leadMs / 1000;
    const giveUpAt = target + windowMs / 1000;

    while (serverTimeUnix() < startAt) {
      const waitMs = (startAt - serverTimeUnix()) * 1000;
      if (waitMs <= 4) break;
      await sleep(Math.min(20, waitMs - 4));
    }

    armState.waitingLog = [];
    let attempts = 0;
    let lastError = null;
    let unreachable = 0;
    let authFailures = 0;
    while (serverTimeUnix() < giveUpAt && attempts < WAITING_MAX_ATTEMPTS) {
      // A block ends the attempt, immediately.
      //
      // fetchWaitingUrl throws on BL / 403 / 429 after recording the cooldown,
      // but the throw was caught per-attempt and the loop carried on — 20ms
      // apart through the decisive window, then 80ms, for the remaining fifteen
      // seconds. That is ~50 requests a second against an account that is
      // already locked, and every one of them can push the lockout further past
      // the open, which is the one moment it cannot be waited out. The catch
      // loop has checked this on every tick for a while; this one never did.
      const blockedFor = gatewayBlockRemainingMs();
      if (blockedFor > 0) {
        armState.waitingAttempts = attempts;
        throw new Error(
          `접속 차단 — ${Math.ceil(blockedFor / 1000)}초 후에 다시 시도하세요.` +
            (seatState.blockedEndpoint ? ` (${seatState.blockedEndpoint})` : ""),
        );
      }
      attempts += 1;
      const sentOffsetMs = Math.round((serverTimeUnix() - target) * 1000);
      const startedPerf = performance.now();
      let outcome = "";
      try {
        const answer = await fetchWaitingUrl(arm);
        outcome = describeWaitingAnswer(answer);
        noteWaitingAttempt(sentOffsetMs, outcome, performance.now() - startedPerf);
        if (WAITING_TERMINAL.test(String(answer))) {
          armState.waitingAttempts = attempts;
          return answer;
        }
        if (isUsableWaitingAnswer(answer)) {
          armState.waitingAttempts = attempts;
          armState.acquiredLatenessMs = Math.round((serverTimeUnix() - target) * 1000);
          log(`waiting acquired after ${attempts} attempt(s)`);
          return answer;
        }
        // The server answered. Whatever it said, the road is open.
        unreachable = 0;
      } catch (error) {
        lastError = error;
        noteWaitingAttempt(sentOffsetMs, `오류 ${String(error).slice(0, 40)}`,
                           performance.now() - startedPerf);
        // Nothing came back at all. Retrying is what this loop is for when a
        // packet drops, but a request the browser will not even complete
        // answers the same way every time — and the caller has a fallback that
        // actually works, which it cannot reach until this gives up.
        // The old realm answering 401 will answer 401 again; three of those
        // are the answer, not a dropped packet. Stop with a reason.
        authFailures = isAuthError(error) ? authFailures + 1 : 0;
        if (authFailures >= WAITING_AUTH_STREAK) {
          armState.waitingAttempts = attempts;
          const message = "대기열 API가 로그인을 거부합니다 (401) — 예매하기 버튼으로 진입합니다";
          updateOverlay(message, "warn");
          throw new Error(message);
        }
        unreachable = isUnreachableError(error) ? unreachable + 1 : 0;
        if (unreachable >= WAITING_UNREACHABLE_STREAK) {
          armState.waitingAttempts = attempts;
          armState.waitingUnreachable = true;
          traceCall("waitingUnreachable", null, {
            attempts,
            origin: location.origin,
            error: String(error).slice(0, 120),
          });
          throw error;
        }
      }
      const elapsed = performance.now() - startedPerf;
      if (attempts % 12 === 0) {
        updateOverlay(`대기열 요청 ${attempts}회 재시도…`, "info");
      }
      const interval = waitingIntervalAt((serverTimeUnix() - target) * 1000, shape);
      await sleep(Math.max(0, interval - elapsed));
    }
    armState.waitingAttempts = attempts;
    if (lastError) throw lastError;
    return null;
  }

  // What came back, short enough to sit in a log line. The distinction that
  // matters is "nothing usable yet" versus "a queue URL" — those are the two
  // states whose boundary we are trying to find.
  function describeWaitingAnswer(answer) {
    if (answer === null || answer === undefined || answer === "") return "(빈 응답)";
    const text = String(answer);
    if (/^https?:\/\//i.test(text)) {
      try {
        return `대기열 ${new URL(text).host}`;
      } catch (error) {
        return "대기열 URL";
      }
    }
    if (text === "N") return "N (대기열 없음)";
    if (text === "NP") return "NP (선예매 인증 필요)";
    if (text === "BL") return "BL (차단)";
    return text.slice(0, 40);
  }

  const WAITING_LOG_LIMIT = 40;

  function noteWaitingAttempt(offsetMs, outcome, ms) {
    const log = armState.waitingLog;
    if (!log) return;
    log.push({ offsetMs, outcome, ms: Math.round(ms) });
    // Keep the boundary, not the tail: the interesting entries are the ones
    // around the flip, and a 15s window at 20ms would otherwise bury them.
    if (log.length > WAITING_LOG_LIMIT) log.splice(0, log.length - WAITING_LOG_LIMIT);
  }

  async function fireEntry(arm) {
    const firedAt = serverTimeUnix();
    const target = armTargetUnix(arm);
    // Against the corrected target, not 티켓 오픈: a -250ms correction that
    // worked would otherwise read as being 250ms early every single time, and
    // the one number you tune by would never move.
    const latenessMs = (firedAt - target) * 1000;
    log("entry fire", { firedAt, target, latenessMs, offsetMs: arm.entry_offset_ms || 0 });
    armState.latenessMs = latenessMs;
    armState.firedAtServer = firedAt;
    armState.entryOffsetMs = Number(arm.entry_offset_ms) || 0;
    armState.enteredVia = "";
    armState.route = "";
    armState.clickLog = [];
    armState.clickTries = 0;
    armState.clickLatenessMs = null;
    armState.lastError = "";
    // The round it actually used. A rehearsal that reports 회차 017 while the
    // 예매 창 shows 022 has found the bug for you.
    const live = withLivePlaySeq(getInitData());
    armState.goodsCode = String(arm.goods_code || live?.goods?.goodsCode || "");
    armState.playSeq = String(live?.playSeq?.playSeq || live?.playSeq || arm.play_seq || "");

    if (arm.dry_run) {
      armState.enteredVia = "dry-run";
      armState.route = "dry-run";
      updateOverlay(`테스트 진입 ${latenessMs >= 0 ? "+" : ""}${latenessMs.toFixed(2)} ms`, "ok");
      return { dryRun: true, latenessMs };
    }

    await waitForCaptchaClear();

    // The route that actually works, tried first wherever it can be tried. It
    // needs no button, no SSO hop and no gate boot — one GET for the credential
    // and one POST for the queue URL, from the origin that holds the session.
    if (secureUrlUsableHere() && arm.goods_code && arm.play_seq) {
      // Bounded, classified retries. Before this it was one shot: the first
      // call at -390ms answered UnableReservationTime (not open yet), and the
      // fire fell straight into the api-ticketfront loop below, which answers
      // 401 for every show and spent the whole window saying "N회 재시도…".
      // Measured 2026-09-04 12:00 — entry landed ~15s after the open.
      const result = await enterViaSecureUrlWithRetries(arm);
      if (result) return { ...result, latenessMs };
    }

    if (isNolProductPage()) {
      updateOverlay("NOL 예매 진입…", "info");
      // The queue endpoint sends no CORS header to this origin — see
      // waitingApiUsableHere(). Asking anyway costs the whole 15s window and
      // gets a TypeError every time; the page's own 예매하기 below is the route
      // that actually works from here.
      if (arm.use_waiting_api !== false && waitingApiUsableHere()) {
        try {
          const waitingUrl = await acquireWaitingUrl(arm);
          armState.waitingUrl = waitingUrl || "";
          if (waitingUrl === "NP") throw new Error("선예매 인증이 필요합니다 (NP)");
          if (waitingUrl === "BL") throw new Error("비정상 예매로 차단되었습니다 (BL)");
          if (typeof waitingUrl === "string" && /^https?:\/\//i.test(waitingUrl)) {
            armState.enteredVia = "waiting";
            armState.route = "waiting-api";
            rememberQueueHost(waitingUrl);
            location.href = waitingUrl;
            return { waitingUrl, latenessMs };
          }
          if (waitingUrl === "N") {
            armState.enteredVia = "book";
            armState.route = "book-session";
            return { ...openBookSession(arm), waitingUrl, latenessMs };
          }
        } catch (error) {
          armState.lastError = String(error);
          log("NOL waiting API", error);
        }
      } else if (arm.use_waiting_api !== false) {
        armState.enteredVia = "";
        log(`waiting API not readable from ${location.origin}; using the page's own route`);
      }
      const entered = await enterFromNolPage(arm);
      // A queue call that could not be made is not a failed entry when the
      // page's own route worked. Leaving it set is why a run that got in read
      // 진입 실패 · TypeError: Load failed on the panel — the one line you check
      // to know whether the open went well, saying the opposite of the truth.
      // A throw from enterFromNolPage skips this and keeps the error.
      armState.lastError = "";
      if (!armState.enteredVia) armState.enteredVia = "book";
      armState.route = entered.route || "dom-click";
      return { ...entered, latenessMs };
    }

    if (isGatesPage()) {
      armState.route = "gates";
      updateOverlay("게이트 세션 연결 중…", "info");
      return { gates: true, latenessMs };
    }

    let waitingUrl = null;
    // api-ticketfront's /waiting answers 401 "자동 로그아웃" for every show once
    // logged in over NOL SSO (see core/entry.py). Where secure-url is usable it
    // has just been tried for the whole window; asking the dead endpoint too
    // only delays the 예매하기 click below.
    if (arm.use_waiting_api !== false && isGoodsPage() && waitingApiUsableHere()
        && !secureUrlUsableHere()) {
      try {
        waitingUrl = await acquireWaitingUrl(arm);
        armState.waitingUrl = waitingUrl || "";
        log("waiting API", waitingUrl);
      } catch (error) {
        armState.lastError = String(error);
        log("waiting API failed", error);
      }
    }

    if (waitingUrl === "NP") throw new Error("선예매 인증이 필요합니다 (NP)");
    if (waitingUrl === "BL") throw new Error("비정상 예매로 차단되었습니다 (BL)");
    if (typeof waitingUrl === "string" && /^https?:\/\//i.test(waitingUrl)) {
      armState.route = "waiting-api";
      updateOverlay("대기열 URL로 진입…", "info");
      rememberQueueHost(waitingUrl);
      location.href = waitingUrl;
      return { waitingUrl, latenessMs };
    }

    if (waitingUrl === "N") {
      armState.route = "book-session";
      updateOverlay("대기열 없음 — BookSession 진입", "info");
      return { ...openBookSession(arm), waitingUrl, latenessMs };
    }

    // The queue API can fail fast — a throw, or a terminal answer — and leave
    // us here while the countdown is still running. The same watch-and-click
    // loop the NOL page uses handles both: it waits out the rest of the
    // countdown and then keeps looking, rather than taking one shot at a button
    // the page may not have enabled yet. `enterFromNolPage` falls back to the
    // SSO gate on its own when place_code is known, so that branch is gone too.
    updateOverlay("예매하기 대기 중…", "info");
    const entered = await enterFromNolPage(arm);
    armState.lastError = "";
    armState.route = entered.route || "dom-click";
    updateOverlay("예매하기 클릭", "warn");
    return { ...entered, waitingUrl, latenessMs };
  }

  const QUEUE_HOST_KEY = "nolsniper_queue_host_v1";

  /**
   * Warm the queue host before the open, if we have ever seen it.
   *
   * Measured: a cold TCP+TLS handshake to the booking hosts costs ~37ms. The
   * /waiting request itself is already warm by T — the 400ms lead sees to that
   * — but the *navigation* that follows goes to a different host entirely, and
   * that one is cold at the exact moment it is on the critical path of claiming
   * a place in line.
   *
   * The host is not knowable on a first run, so this learns it: remember it
   * when a queue URL arrives, preconnect to it next time.
   */
  function preconnectQueueHost() {
    let host = "";
    try {
      host = localStorage.getItem(QUEUE_HOST_KEY) || "";
    } catch (error) {
      return "";
    }
    if (!host || document.querySelector(`link[data-nolsniper-preconnect="${host}"]`)) return host;
    try {
      const link = document.createElement("link");
      link.rel = "preconnect";
      link.href = host;
      link.crossOrigin = "anonymous";
      link.dataset.nolsniperPreconnect = host;
      document.head?.appendChild(link);
      log(`preconnect ${host}`);
    } catch (error) {
      /* head not ready; the navigation still works, just cold */
    }
    return host;
  }

  function rememberQueueHost(waitingUrl) {
    try {
      const origin = new URL(String(waitingUrl), location.origin).origin;
      if (origin && origin !== location.origin) {
        localStorage.setItem(QUEUE_HOST_KEY, origin);
        armState.queueHost = origin;
      }
    } catch (error) {
      /* not a URL — "N", "NP", "BL" all land here and none is a host */
    }
  }

  // Is the scheduler already running for exactly this arm (goods + round)?
  function sameArmRunning(arm) {
    const goods = String(armState.armedGoodsCode || armState.goodsCode || "");
    const seq = String(armState.armedPlaySeq || armState.playSeq || "");
    return !!goods && goods === String(arm?.goods_code || "") && seq === String(arm?.play_seq || "");
  }
  async function runArmScheduler(arm) {
    // Each attempt starts clean. lastError is otherwise only cleared inside
    // fireEntry, which a refused arm never reaches — so one refusal would sit
    // on screen through every later attempt.
    armState.lastError = "";

    // Every one of these used to return silently, so an arm that did nothing
    // looked identical to one that had not been asked. Measured on a live
    // session: armState came back all zeros — syncMs 0, clockQuality "", no
    // attempts, no error — because the 예매 창 was on the seat map, where there
    // is nothing to enter. Nothing said so.
    const refuse = (why) => {
      armState.lastError = why;
      log("arm refused", why);
      traceCall("armRefused", null, why);
    };
    if (!arm) return refuse("예약 정보가 없습니다 — 조작판에서 다시 [대기 시작]을 누르세요.");
    if (loginState() === false) return refuse("[로그인 필요 — 세션이 없습니다] 예매 창에서 로그인한 뒤 다시 누르세요.");
    if (!arm.enabled) return refuse("예약이 꺼져 있습니다.");
    if (arm.fired || armState.fired) return refuse("이미 이번 예약으로 진입했습니다.");
    // Was a bare `return`. When `running` could get stuck true this was the
    // silence the user actually experienced: press 대기 시작, nothing happens,
    // nothing said. It cannot get stuck any more, but a concurrent press still
    // deserves an answer.
    // The panel pushes the arm and then the command; the second arrival for
    // the same arm is the same request, not a fault to report.
    if (armState.running && sameArmRunning(arm)) return { ok: true, already: true };
    if (armState.running) return refuse("이미 대기 예약이 진행 중입니다.");

    // The entry only means anything where an entry can happen. On the seat map
    // or inside the queue there is no line left to join.
    if (!isNolProductPage() && !isGoodsPage()) {
      return refuse(
        isSeatPage()
          ? "이미 좌석맵에 있습니다 — 들어갈 대기열이 없습니다."
          : isWaitingPage() || isGatesPage()
            ? "이미 대기열에 있습니다."
            : "공연 페이지가 아닙니다 — 예매 창에서 공연을 여세요.",
      );
    }

    // An arm during a block cannot get in line and every attempt can extend the
    // lockout past the open — the one moment it cannot be recovered from. The
    // queue path used to neither set nor check this, so a block earned by the
    // seat path would let an arm fire straight into it.
    const blockedFor = gatewayBlockRemainingMs();
    if (blockedFor > 0) {
      const seconds = Math.ceil(blockedFor / 1000);
      armState.lastError =
        `접속 차단 중 — ${seconds}초 후에 다시 시도하세요.` +
        (seatState.blockedEndpoint ? ` (${seatState.blockedEndpoint})` : "");
      updateOverlay(`접속 차단 중<br>${seconds}초 남음`, "error");
      return;
    }

    // Everything from here on is inside one try/finally.
    //
    // `running` used to be set here, with two awaits — syncServerClock and
    // waitUntilServerUnix — outside the try that resets it. A throw from either
    // left it true forever, and because bootRoute calls this without awaiting,
    // the rejection went nowhere. The guard at the top of this function is
    // `if (armState.running) return`, so the next press, and every press after
    // it, was refused in silence. One bad sync disabled 대기 시작 for the life of
    // the page and said nothing.
    // The credential is minted on tickets.interpark.com only. Counting down
    // anywhere else — measured 17:00, the 예매 창 was on nol.yanolja.com — sends
    // the fire down the 2.5s cross-site button path instead of the 33ms queue
    // call. Move now; the arm survives in storage and bootRoute re-arms on the
    // goods page.
    if (!secureUrlUsableHere() && arm.goods_code) {
      updateOverlay("진입 원점으로 이동 중 — tickets.interpark.com 에서 대기합니다", "info");
      location.href = `${GATE_ORIGIN}/goods/${encodeURIComponent(String(arm.goods_code))}`;
      return { ok: false, reparked: true };
    }
    armState.running = true;
    armState.armedGoodsCode = String(arm.goods_code || "");
    armState.armedPlaySeq = String(arm.play_seq || "");
    try {
      updateOverlay("서버 시각 동기화 중…", "info");
      // Where the time goes, so "it took too long" can be answered with numbers
      // rather than argued about.
      const syncStarted = performance.now();
      await syncServerClock(Number(arm.offset_seconds || 0));
      armState.syncMs = Math.round(performance.now() - syncStarted);
      armState.clockQuality = clockState.quality;
      armState.clockOffsetMs = Math.round((clockState.offsetSeconds || 0) * 1000);
      armState.queueHost = preconnectQueueHost();
      armState.clockJumpMs = 0;
      const remaining = armTargetUnix(arm) - serverTimeUnix();
      updateOverlay(`${arm.dry_run ? "테스트 " : ""}대기열 예약<br>${Math.max(0, remaining).toFixed(1)}초`, "info");
      // Stop short of the open by exactly the lead the request loop expects.
      // Waiting out the full deadline here is what made that lead dead code.
      const cancelled = () => {
        const current = loadArmConfig();
        if (!current || current.enabled === false) return true;
        // Left the entry origin mid-wait (a click on a NOL link, a redirect):
        // go back to the goods page; the arm persists and re-arms there.
        if (!secureUrlUsableHere()) {
          location.href = `${GATE_ORIGIN}/goods/${encodeURIComponent(String(arm.goods_code))}`;
          return true;
        }
        return false;
      };
      const entryStart = armEntryStartUnix(arm) ?? armTargetUnix(arm);
      // Mint the credential PREMINT_LEAD_MS before the burst, so the first
      // shot is one POST and not a GET-then-POST — that GET was the round trip
      // that put every 0ms shot at +60ms. Retried once; a second failure
      // leaves the burst to mint on its first shot.
      if (secureUrlUsableHere() && arm.goods_code && !arm.dry_run) {
        await waitUntilServerUnix(entryStart - PREMINT_LEAD_MS / 1000, { cancelled });
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            await mintMemberInfo(arm, { maxAgeMs: 0 });
            break;
          } catch (error) {
            armState.memberInfo = null;
            log("premint failed; the burst will mint", error);
            await sleep(500);
          }
        }
      }
      await waitUntilServerUnix(entryStart, { cancelled });

      // The wait is over; if the device clock moved during it, say so. The
      // target itself did not move — serverTimeUnix is anchored to
      // performance.now() — but a user who has just shifted their clock needs
      // to be told which of the two readings in front of them is the real one.
      const jump = clockJumpSeconds();
      if (Math.abs(jump) > CLOCK_JUMP_TOLERANCE_S) {
        armState.clockJumpMs = Math.round(jump * 1000);
        log("device clock moved during the wait", jump);
        updateOverlay(
          `기기 시계가 ${jump > 0 ? "앞으로" : "뒤로"} ${Math.abs(jump).toFixed(0)}초 바뀌었습니다<br>발사 시각은 그대로 유지합니다`,
          "warn",
        );
      }

      const firedPerf = performance.now();
      try {
        await fireEntry(arm);
        armState.enterMs = Math.round(performance.now() - firedPerf);
        arm.fired = true;
        armState.fired = true;
        saveArmConfig({ ...arm, fired: true });
      } catch (error) {
        armState.lastError = String(error);
        updateOverlay(`진입 실패: ${error}`, "error");
      }
    } catch (error) {
      // Anything before the fire — a sync, a preconnect, the wait itself.
      if (error && error.cancelled) {
        // 대기 중지: not a failure, and nothing to report as one.
        armState.lastError = "";
        updateOverlay("오픈 대기를 취소했습니다.", "warn");
        return;
      }
      armState.lastError = `예약 준비 실패: ${String(error).slice(0, 90)}`;
      updateOverlay(`예약 준비 실패: ${error}`, "error");
      traceCall("armFailed", null, String(error).slice(0, 120));
    } finally {
      armState.running = false;
    }
  }

  /**
   * Try the entry again after we have been bounced back to the product page.
   *
   * Re-entry is a recovery. It used to be a second front door, and an unguarded
   * one: driven by the 400ms watcher with no in-flight latch, no check that the
   * open had even happened, and a `setInterval` callback that does not await, so
   * nothing serialised the attempts. From the instant an arm landed, every tick
   * started a *new* fireEntry — each parking until T-400ms and then polling
   * /waiting at 20ms for fifteen seconds. Within ~16s there were 41 of them
   * stacked, all aimed at the one endpoint that answers GATEWAY_ABUSE_BLOCKED
   * with a ~165s lockout, at the exact moment a lockout cannot be recovered
   * from. `reentryTries`, the only trace, was published and rendered nowhere.
   *
   * Three things hold it down now: one attempt in flight at a time, nothing
   * before the target time, and a floor on the gap between attempts.
   */
  const REENTRY_SPACING_MS = 3000;
  const REENTRY_LIMIT = 40;

  // ---- Standing in the watched 구역 ----------------------------------------
  //
  // Being in the block with its seats mounted *before* one frees is the whole
  // difference between clicking in a frame and clicking in a second. Travel is
  // the largest cost in the loop — measured through noteMapMove: leaving a
  // block, opening another and fitting it runs to the better part of a second —
  // and every millisecond of it is paid after detection, when it is the one
  // thing that cannot be afforded.
  //
  // This used to run once, at 감시 시작. But the view does not stay put: losing
  // a race raises a modal, dismissing it and clearing the cart re-render the
  // map, enterBlockForSeats normalises the framing with 전체보기 mid-run, and
  // the user may pan away while the watch is running. From the first such
  // event onwards every catch paid the travel again — the preparation was real
  // but it did not last, which is indistinguishable from not having it.
  //
  // So the position is re-checked while the watch is idle, which is the one
  // time it is free: nothing is in play, and the alternative is sleeping. It is
  // never checked while a seat is being chased, never while the user has the
  // map under their finger, and never more often than PARK_RECHECK_MS.
  const PARK_RECHECK_MS = 4000;

  async function parkInWatchedBlock(config, watchKeys, { force = false } = {}) {
    // auto_assign has no circles to click and asks the server to allocate, so
    // there is no viewport for it to be standing in.
    if (config.auto_assign) return null;
    const now = nowMs();
    // A venue we cannot open is not going to become openable by being asked
    // every four seconds, and each attempt costs three click hypotheses at
    // ~900ms apiece — spent not polling. Back off, but never give up: the
    // failure is usually a modal or a framing that clears on its own.
    const failures = seatState.parkFailures || 0;
    const wait = PARK_RECHECK_MS * Math.min(2 ** failures, 8);
    if (!force && now - (seatState.parkedCheckedAt || 0) < wait) return null;
    seatState.parkedCheckedAt = now;
    // Fighting the user for the viewport is worse than waiting a moment.
    watchMapPointer();
    if (pointerHeldOnMap) return { ok: false, via: "user-dragging" };
    // Nothing on the map moves through a modal backdrop, and clicking blindly
    // into one is how a "block entry" silently answers the dialog instead.
    if (blockingOverlayNodes().length) return { ok: false, via: "modal" };
    try {
      const rect = normalizeWatchRect(config.watch_rect);
      const watchedKeys = rect
        ? blocksInWatchRect(seatState.lastBlocks || [], rect) || watchKeys
        : watchKeys;
      const openNow = currentOpenBlock();
      const target = blockToStandIn(watchedKeys, openNow);
      if (!target) return { ok: false, via: "no-target" };
      const key = String(target.blockKey);

      if (openNow === key) {
        seatState.parkFailures = 0;
        // Already in the right 구역. Fitting it mounts the rest of its seats,
        // but 전체보기 is a real map move: doing it every few seconds would
        // yank the view out from under anyone reading it. Once per arrival.
        if (seatState.parkedBlock === key && !force) return { ok: true, via: "already" };
        seatState.parkedBlock = key;
        await noteMapMove("fitBlock", key, () => fitBlockToView());
        return { ok: true, via: "fit" };
      }

      updateOverlay(`감시할 구역 ${target.selfDefineBlock || key} 여는 중…`, "info");
      // Through noteMapMove, so the panel reports what this actually costs.
      // These were the one set of map moves not being measured, and they are
      // the ones you wait on.
      if (openNow) await noteMapMove("leaveBlock", openNow, () => leaveBlockToVenue());
      const entered = await noteMapMove("enterBlock", key, () => enterBlockForSeats(target));
      if (!entered.ok) {
        // Not parked. Say so, so the next idle tick tries again rather than
        // believing it is standing somewhere it is not.
        seatState.parkedBlock = "";
        seatState.parkFailures = failures + 1;
        return { ok: false, via: "enter-failed" };
      }
      await noteMapMove("fitBlock", key, () => fitBlockToView());
      seatState.parkedBlock = key;
      seatState.parkFailures = 0;
      if (!force) seatState.reparks = (seatState.reparks || 0) + 1;
      return { ok: true, via: "entered" };
    } catch (error) {
      // Standing in the right place is an optimisation; the run still works
      // without it, and a throw here must not end the watch.
      seatState.parkedBlock = "";
      seatState.parkFailures = failures + 1;
      traceCall("park", null, { error: String(error).slice(0, 160) });
      return { ok: false, via: "error" };
    }
  }

  function resetReentryState() {
    armState.reentryInFlight = false;
    armState.reentryAt = 0;
    armState.reentryTries = 0;
  }

  async function maybeReenter() {
    // The latch, not armState.running: fireEntry called from here never sets
    // that flag, so `running` alone left the attempts free to overlap.
    if (armState.reentryInFlight) return;
    // An arm already in progress owns the queue endpoint. Re-entering beside it
    // is the storm, not a recovery.
    if (armState.running) return;
    const seat = loadSeatConfig();
    const arm = loadArmConfig();
    if (!seat.reentry || !arm?.enabled) return;
    // Before the open there is nothing to recover from — the scheduler is
    // already waiting for T, and a second entry beside it is a duplicate. This
    // is deliberately keyed off the target rather than `fired`, because `fired`
    // does not survive: apply_state rewrites nolsniper_arm_v1 from the panel's
    // copy, which stays false, on every state-file change.
    const target = armTargetUnix(arm);
    if (Number.isFinite(target) && target > 0 && serverTimeUnix() < target) return;
    if (isSeatPage() && getInitData()?.sessionId) return;
    if (armState.reentryTries >= REENTRY_LIMIT) return;
    if (isWaitingPage() || isGatesPage()) return;
    if (!(isNolProductPage() || isGoodsPage())) return;
    if (armState.reentryAt && nowMs() - armState.reentryAt < REENTRY_SPACING_MS) return;

    armState.reentryInFlight = true;
    armState.reentryAt = nowMs();
    armState.reentryTries += 1;
    updateOverlay(`재진입 ${armState.reentryTries}회`, "warn");
    try {
      await fireEntry({ ...arm, dry_run: false, fired: false });
    } catch (error) {
      // Was console-only, so a re-entry that could never work looked exactly
      // like one that was about to.
      armState.lastError = `재진입 실패: ${String(error).slice(0, 80)}`;
      log("reentry failed", error);
    } finally {
      // Re-stamped on the way out, so the floor is the gap *between* attempts
      // rather than between their starts. An attempt can easily outlast 3s —
      // the 예매하기 watch alone spends up to 8 — and stamping only at the start
      // meant the floor had already expired by the time it was next consulted,
      // leaving the in-flight latch as the sole guard against back-to-back
      // retries at an endpoint that answers repetition with a lockout.
      armState.reentryAt = nowMs();
      armState.reentryInFlight = false;
    }
  }

  function normalizeGradeToken(value) {
    return String(value ?? "").replace(/\s+/g, "").toLowerCase();
  }

  // seatGrade is a per-show ordinal ("1" is VIP석 on a musical but EARLY ENTRY
  // PACKAGE on a concert), so a preference entry has to match the grade name too.
  function rankGrade(seat, gradeOrder) {
    if (!gradeOrder.length) return 0;
    const code = normalizeGradeToken(seat.seatGrade);
    const name = normalizeGradeToken(seat.seatGradeName);
    for (let index = 0; index < gradeOrder.length; index += 1) {
      const needle = normalizeGradeToken(gradeOrder[index]);
      if (!needle) continue;
      if (needle === code || needle === name || (name && name.includes(needle))) return index;
    }
    return -1;
  }

  const SEAT_STRATEGIES = ["center", "left", "right"];

  // 32-bit FNV-1a. Gives `random` a stable order for a fixed seed, so a seat
  // keeps its place across retries within one run, while a fresh seed per run
  // sends the next run somewhere else.

  // Two numeric sort keys per seat, computed once (a Schwartzian transform —
  // cheaper than a comparator over tens of thousands of candidates).
  //
  // For left/center/right the *horizontal* key leads on purpose. If posTop led,
  // all three would converge on the front row and the whole point — three people
  // on three strategies landing on disjoint seats — would be lost.
  // How close a seat is to the stage, and only then which side it sits on.
  //
  // Every mode used to sort by x first and treat the stage as a tiebreak, so
  // 왼쪽부터 took the leftmost seat in the building — possibly the back row.
  // Distance leads now, and the side is a *filter* rather than a sort key.
  // That is what makes the choice always mean something, and it is why no row
  // grouping is needed: on an extruded stage the seats flanking the thrust win
  // on distance by themselves, from whichever side they approach it.
  function stageDistance(seat, stage) {
    const left = seat.posLeft == null ? null : seat.posLeft;
    const top = seat.posTop == null ? null : seat.posTop;
    if (top === null) return null;
    // Straight-line distance to the stage. Depth alone measured only how far
    // back a seat is and never how far to the side, so on a venue with side
    // blocks running the full depth a far-left seat level with the front row
    // scored the same as a centre seat in that row — and the macro could take a
    // seat at the edge of the house and call it 무대 가까운 순.
    if (stage && left !== null) {
      const dx = left - stage.x;
      const dy = top - stage.y;
      return Math.sqrt(dx * dx + dy * dy);
    }
    return top;
  }

  // 가운데 weights a step sideways more than a step back, so a central seat a
  // few rows deeper beats one out toward the wing. Pure straight-line distance
  // treated them as equal cost, which — on a round where the middle blocks have
  // sold and only a wing block is free up front — always chose the wing (the
  // "무조건 오른쪽" the user saw). The horizontal anchor is the stage's own x
  // (the front-row extent midpoint), never the free-seat median, so a lopsided
  // free pool cannot drag "centre" toward the crowded side. 1.0 restores the
  // old isotropic behaviour.
  const CENTER_HWEIGHT = 1.6;
  function strategyKeys(seat, strategy, seed, centerX, stage = null) {
    // `== null` on purpose: toCandidate normalises to null, but DOM-built and
    // test-built seats simply omit the field. Treating `undefined` as a number
    // yields NaN, and one NaN key makes the whole comparison meaningless.
    const left = seat.posLeft == null ? null : seat.posLeft;
    const top = seat.posTop == null ? null : seat.posTop;
    const mid = centerX == null ? null : centerX;
    // A missing key sorts last, so positioned seats lead and the positionless
    // tail falls through to the rowNo/seatNo chain exactly as it did before.
    const NONE = Number.POSITIVE_INFINITY;
    const sideward = left === null || mid === null ? NONE : Math.abs(left - mid);
    // 가운데, with a real stage: anchor centrality on the stage x and make a
    // sideways unit cost more than a depth unit. Left/right keep the plain
    // straight-line distance and let their side filter do the narrowing.
    if (strategy === "center" && stage && left !== null && top !== null
        && stage.x !== null && stage.x !== undefined) {
      const dx = (left - stage.x) * CENTER_HWEIGHT;
      const dy = top - stage.y;
      return [Math.sqrt(dx * dx + dy * dy), sideward];
    }
    const near = stageDistance(seat, stage);
    return [near === null || near === undefined ? NONE : near, sideward];
  }

  // Which seats a side preference will even consider. 가운데 takes them all;
  // 왼쪽/오른쪽 keep their half and let distance decide within it.
  function seatOnChosenSide(seat, strategy, centerX) {
    if (strategy !== "left" && strategy !== "right") return true;
    if (centerX === null || centerX === undefined) return true;
    if (seat.posLeft === null || seat.posLeft === undefined) return true;
    return strategy === "left" ? seat.posLeft <= centerX : seat.posLeft >= centerX;
  }

  // 0 for the ground floor (1층/1F/unknown), 1 for anything above it.
  // 0 = ground floor (1층 / 지하 / B1 / no info), 1 = any upper floor. Reads
  // the floor string first ("객석 1층", "2F"…); when the map gives none, the
  // block key encodes it — Sejong Center is 001:1xx (1층) / 001:2xx (2층) /
  // 001:3xx (3층), so the hundreds digit is the floor. Without this fallback a
  // sketch that carries only {k,x,y} ranked every seat as ground and the
  // 1층>2층 gate silently did nothing.
  function floorRank(seat) {
    const floor = String(seat?.floor || seat?.floorName || "").replace(/\s+/g, "");
    if (floor) {
      if (/지하|^B\d|^G(?!\d)/i.test(floor)) return 0;
      const named = /(\d+)\s*(층|F)/i.exec(floor);
      if (named) return Number(named[1]) >= 2 ? 1 : 0;
    }
    const key = String(seat?.blockKey || "");
    const m = /:(\d)\d\d(?::|$)/.exec(key) || /:(\d)\d\d/.exec(key);
    if (m) return Number(m[1]) >= 2 ? 1 : 0;
    return 0;
  }
  function rankCandidates(
    candidates,
    gradeOrder,
    blockKeys = [],
    { strict = false, strategy = "center", seed = 0, centerX = null, stage = null } = {},
  ) {
    const allowed = blockKeys.length ? new Set(blockKeys.map(String)) : null;
    // Older saved settings name modes that no longer exist ("stage", "random").
    const mode = SEAT_STRATEGIES.includes(strategy) ? strategy : "center";
    const middle = centerX === null ? medianPosLeft(candidates) : centerX;
    const shape = stage || seatState.mapStage || null;
    const ranked = [];
    for (const seat of candidates) {
      if (allowed && seat.blockKey && !allowed.has(String(seat.blockKey))) continue;
      if (!seatOnChosenSide(seat, mode, middle)) continue;
      let rank = rankGrade(seat, gradeOrder);
      if (rank === -1) {
        if (strict && gradeOrder.length) continue;
        rank = gradeOrder.length;
      }
      const [keyA, keyB] = strategyKeys(seat, mode, seed, middle, shape);

      ranked.push({ ...seat, _rank: rank, _floor: floorRank(seat), _posA: keyA, _posB: keyB });
    }
    const sorted = ranked.sort((a, b) => {
      // Grade preference still wins outright — a strategy only decides which
      // seat *within* a grade tier.
      if (a._rank !== b._rank) return a._rank - b._rank;
      // 1층 outranks any upper floor within a grade: a front-centre 2F seat
      // must never beat a 1F seat on distance alone.
      if (a._floor !== b._floor) return a._floor - b._floor;
      if (a._posA !== b._posA) return a._posA - b._posA;
      if (a._posB !== b._posB) return a._posB - b._posB;
      if (a.rowNo !== b.rowNo) return String(a.rowNo).localeCompare(String(b.rowNo), "ko", { numeric: true });
      if (a.seatNo !== b.seatNo) return String(a.seatNo).localeCompare(String(b.seatNo), "ko", { numeric: true });
      return String(a.seatInfoId).localeCompare(String(b.seatInfoId));
    });
    recordSeatOrder(sorted, shape);
    return sorted;
  }

  // What the ordering actually produced, so "무대 가까운 순 is random" becomes a
  // thing anyone can check rather than a thing I have to guess about. The panel
  // shows the winner and the seats it beat, each with its coordinates and its
  // distance to the stage — if those distances climb down the list, the order
  // is right whatever the chosen seat looks like on the map.
  function recordSeatOrder(sorted, shape) {
    seatState.lastOrder = sorted.slice(0, 5).map((seat) => ({
      label: seat.label || `${seat.rowNo || ""} ${seat.seatNo || ""}`.trim() || String(seat.seatInfoId),
      x: numOrNull(seat.posLeft),
      y: numOrNull(seat.posTop),
      // Infinity survives neither JSON nor the panel's formatting.
      dist: Number.isFinite(seat._posA) ? Math.round(seat._posA) : null,
    }));
    seatState.lastStagePoint = shape ? { x: Math.round(shape.x), y: Math.round(shape.y) } : null;
    if (seatState.lastOrder.length) {
      traceCall("seatOrder", seatState.lastStagePoint, seatState.lastOrder);
    }
  }

  // One place to build the ranking options, because all four call sites must
  // agree. If one of them missed the strategy, the API path and the DOM fallback
  // would silently order seats differently within a single run.
  function pickerOptions(config, { isCatch = false } = {}) {
    // With an area drawn, the area *is* the filter — 취켓팅 takes whatever frees
    // up inside it at any grade, nearest the stage first. A cancellation is
    // gone in seconds; refusing it because the grade was not on a list is how
    // you watch an empty seat go to somebody else.
    const watched = isCatch && normalizeWatchRect(config.watch_rect) !== null;
    return {
      strict: watched ? false : isCatch ? config.catch_grade_strict !== false : Boolean(config.grade_strict),
      // The area narrows which seats are eligible; the order you chose then
      // decides between what is left. Forcing 가운데 here made 왼쪽/오른쪽 inert
      // whenever an area was drawn, with no sign that it had been ignored.
      strategy: config.seat_strategy || "center",
      seed: seatState.shuffleSeed || 0,
      // The stage's own x is the truest centre (front-row extent midpoint);
      // fall back to the venue extent midpoint, then to the free-pool median.
      centerX: seatState.mapStage?.x ?? seatState.mapCenterX ?? null,
    };
  }

  // Computed over every seat in the venue, free or not, so a seat's rank cannot
  // drift as the free pool shrinks between ticks.
  // Where the stage is: the middle of the front row.
  //
  // This replaces an occupancy grid that flood-filled the empty space to find
  // an extruded stage. It was dormant on every ordinary venue, and on one real
  // layout it mistook the aisle between two floor blocks for the stage — so
  // seats beside that aisle scored distance ~0 and a seat well back outranked
  // the front row. A point at the centre of the front row is never wildly
  // wrong, which the grid could be.
  //
  // Computed from *every* seat, never from the free ones: a stage that moves as
  // seats sell would reorder the venue underneath itself.
  function stagePoint(blocks) {
    let front = Infinity;
    const points = [];
    for (const block of blocks || []) {
      for (const seat of block.seats || []) {
        const x = numOrNull(seat.posLeft);
        const y = numOrNull(seat.posTop);
        if (x === null || y === null) continue;
        points.push([x, y]);
        if (y < front) front = y;
      }
    }
    if (!points.length || front === Infinity) return null;

    // The x extent of the front row itself, so the stage sits over the front
    // rather than over the middle of the whole house — which on an asymmetric
    // venue are different places.
    const band = front + Math.max(1, (Math.max(...points.map((p) => p[1])) - front) * 0.04);
    const frontXs = points.filter(([, y]) => y <= band).map(([x]) => x);
    const xs = frontXs.length ? frontXs : points.map(([x]) => x);
    return { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: front };
  }

  // The geometric centre of the house: the midpoint between its leftmost and
  // rightmost seat. Deliberately the extent midpoint, not the median — the
  // median is pulled toward whichever side holds more seats, so on an
  // asymmetric venue (or a round where one side has sold) it drifted off true
  // centre and dragged 왼쪽/오른쪽 and the centrality tiebreak with it.
  function venueCenterX(blocks) {
    let min = Infinity;
    let max = -Infinity;
    for (const block of blocks || []) {
      for (const seat of block.seats || []) {
        const left = numOrNull(seat.posLeft);
        if (left === null) continue;
        if (left < min) min = left;
        if (left > max) max = left;
      }
    }
    if (min === Infinity) return null;
    return (min + max) / 2;
  }

  function medianPosLeft(candidates) {
    const values = [];
    for (const seat of candidates) if (seat.posLeft !== null && seat.posLeft !== undefined) values.push(seat.posLeft);
    if (!values.length) return null;
    values.sort((a, b) => a - b);
    return values[Math.floor(values.length / 2)];
  }

  // Package/table seats share a seatGroupId and can only be taken as a whole
  // set, so they are offered to the picker as a single atomic unit.
  function groupCandidates(candidates) {
    const units = [];
    const byGroup = new Map();
    for (const seat of candidates) {
      if (!seat.seatGroupId) {
        units.push([seat]);
        continue;
      }
      let unit = byGroup.get(seat.seatGroupId);
      if (!unit) {
        unit = [];
        byGroup.set(seat.seatGroupId, unit);
        units.push(unit);
      }
      unit.push(seat);
    }
    return units;
  }

  function selectSeatUnit(candidates, quantity, adjacent = true) {
    const qty = Math.max(1, Number(quantity) || 1);
    const units = groupCandidates(candidates);
    for (const unit of units) {
      if (unit.length > 1 && unit.length === qty) return unit;
    }
    const loose = units.filter((unit) => unit.length === 1).map((unit) => unit[0]);
    if (!loose.length) return [];
    if (qty <= 1) return loose.slice(0, 1);
    const picked = adjacent ? pickAdjacent(loose, qty) : loose.slice(0, qty);
    // Never hand back fewer seats than were asked for.
    //
    // Both paths above end in a slice, so with one seat free and 매수 2 they
    // returned that single seat — and the caller went on to click it and press
    // 선택 완료, booking one seat for someone who asked for two. In 취켓팅 that
    // is the normal case, because a cancellation frees one seat at a time.
    // Returning nothing makes the run wait for a real pair instead.
    return picked.length === qty ? picked : [];
  }

  function pickAdjacent(candidates, quantity) {
    const qty = Math.max(1, Number(quantity) || 1);
    if (qty <= 1) return candidates.slice(0, 1);
    const byRow = new Map();
    for (const seat of candidates) {
      const key = seat.rowNo || "";
      if (!byRow.has(key)) byRow.set(key, []);
      byRow.get(key).push(seat);
    }
    for (const row of byRow.values()) {
      const ordered = [...row].sort((a, b) => String(a.seatNo).localeCompare(String(b.seatNo), "ko", { numeric: true }));
      for (let start = 0; start <= ordered.length - qty; start += 1) {
        const window = ordered.slice(start, start + qty);
        const nums = window.map((seat) => Number(String(seat.seatNo).replace(/\D/g, "")) || 0);
        let ok = true;
        for (let i = 0; i < nums.length - 1; i += 1) {
          if (nums[i] + 1 !== nums[i + 1]) {
            ok = false;
            break;
          }
        }
        if (ok) return window;
      }
    }
    return candidates.slice(0, qty);
  }

  // The component that owns a seat circle is SeatSvgCircle, whose props are
  // { seat, blockKey, isSelected, isDisabled, onSeatClick, … }. Its pointerup
  // handler is the entire selection path and it gates on exactly these props:
  //
  //     (!isDisabled || isSelected) && onSeatClick(seat, isSelected, blockKey)
  //
  // so `isDisabled` here is the authority on whether a click can do anything —
  // far better than guessing from a hashed CSS-module class name.
  function seatRenderProps(el) {
    if (!el) return null;
    const fiberKey = Object.keys(el).find(
      (key) => key.startsWith("__reactFiber") || key.startsWith("__reactInternalInstance"),
    );
    if (!fiberKey) return null;
    let fiber = el[fiberKey];
    for (let depth = 0; depth < 16 && fiber; depth += 1) {
      const props = fiber.memoizedProps || fiber.pendingProps || {};
      if (props.seat?.seatInfoId && ("isDisabled" in props || "isSelected" in props)) {
        return {
          seat: props.seat,
          blockKey: props.blockKey ?? null,
          isDisabled: Boolean(props.isDisabled),
          isSelected: Boolean(props.isSelected),
        };
      }
      fiber = fiber.return;
    }
    return null;
  }

  function seatFromFiber(el) {
    if (!el) return null;
    const keys = Object.keys(el);
    const fiberKey = keys.find(
      (key) => key.startsWith("__reactFiber") || key.startsWith("__reactInternalInstance"),
    );
    if (fiberKey) {
      let fiber = el[fiberKey];
      for (let depth = 0; depth < 16 && fiber; depth += 1) {
        const props = fiber.memoizedProps || fiber.pendingProps || {};
        // blockKey sits beside `seat`, not inside it, so returning props.seat
        // alone lost it — every "which 구역 is this" test read undefined.
        if (props.seat?.seatInfoId) {
          return props.blockKey && !props.seat.blockKey
            ? { ...props.seat, blockKey: props.blockKey }
            : props.seat;
        }
        if (props.seatInfoId && props.seatGrade) return props;
        fiber = fiber.return;
      }
    }
    const propsKey = keys.find((key) => key.startsWith("__reactProps"));
    if (propsKey) {
      const props = el[propsKey] || {};
      // Same sibling blockKey as the fiber branch above — this path dropped it.
      if (props.seat?.seatInfoId) {
        return props.blockKey && !props.seat.blockKey
          ? { ...props.seat, blockKey: props.blockKey }
          : props.seat;
      }
      if (props.seatInfoId && props.seatGrade) return props;
    }
    const dataset = el.dataset || {};
    const seatInfoId = dataset.seatInfoId || dataset.seatinfoid;
    if (seatInfoId) {
      return {
        seatInfoId,
        seatGrade: dataset.seatGrade || dataset.seatgrade,
        seatGradeName: dataset.seatGradeName || dataset.seatgradename || "",
        rowNo: dataset.rowNo || dataset.rowno || "",
        seatNo: dataset.seatNo || dataset.seatno || "",
        blockKey: dataset.blockKey || dataset.blockkey || null,
        seatGroupId: dataset.seatGroupId || dataset.seatgroupid || null,
        isExposable: dataset.isExposable !== "false",
      };
    }
    return null;
  }

  function collectSeatCircles() {
    const startedPerf = performance.now();
    const nodes = [
      ...document.querySelectorAll("circle.js-seat"),
      ...document.querySelectorAll('circle[class*="SeatMap"]'),
      ...document.querySelectorAll('[class*="SeatMap"] circle'),
      ...document.querySelectorAll("svg circle"),
    ];
    // Set, not indexOf. Four selectors overlap, so `nodes` is roughly four
    // copies of the venue and an indexOf dedupe is O(n²) over that: measured at
    // 58.5ms on a 14,881-seat venue against 1.0ms here, and this runs several
    // times per tick.
    const unique = [...new Set(nodes)];
    const kept = unique.filter((node) => {
      const radius = Number(node.getAttribute("r") || node.r?.baseVal?.value || 0);
      return radius === 0 || (radius >= 1 && radius <= 24);
    });
    // What this actually costs, so the remaining call sites can be judged on
    // evidence rather than on how expensive they look.
    const spent = performance.now() - startedPerf;
    seatState.domScans = (seatState.domScans || 0) + 1;
    seatState.domScanMs = (seatState.domScanMs || 0) + spent;
    seatState.domScanWorstMs = Math.max(seatState.domScanWorstMs || 0, spent);
    return kept;
  }

  function seatMapRoot() {
    return (
      document.querySelector('[class*="seatMap"]') ||
      document.querySelector('[class*="SeatMap"]') ||
      document.querySelector('[class*="placeImg"]')?.parentElement ||
      null
    );
  }

  // How long a seat lost to another buyer stays out of the pool. Long enough
  // not to re-race the same person for a seat they are actively holding, short
  // enough that an abandoned cart comes back within one 취켓팅 sitting.
  const TAKEN_COOLDOWN_MS = 30000;

  // A seat the map would not give us, as opposed to one another buyer holds.
  // Shorter, because the cause is usually local and momentary — the circle was
  // not drawn, the block was not open, a modal swallowed the click — and the
  // seat is often takeable on the very next pass.
  const UNREACHABLE_COOLDOWN_MS = 3000;

  function markSeatTaken(seatInfoId) {
    if (!seatInfoId) return;
    seatState.takenUntil.set(String(seatInfoId), nowMs() + TAKEN_COOLDOWN_MS);
    seatState.takenConflicts = (seatState.takenConflicts || 0) + 1;
  }

  /**
   * Park a seat the page declined for any reason other than a lost race.
   *
   * This is the only thing that makes a decline survive its tick. 취켓팅 rebuilds
   * `candidates` from freed/live at the top of every pass, so the decline
   * branch's local `candidates.filter(...)` was discarded before anything read
   * it — the same seat came back every 100ms, forever, at ~1.5s an attempt.
   * `seatState.takenUntil` is the one place that outlives a tick, and both
   * bitmap and DOM collectors already honour it.
   */
  function markSeatUnreachable(seatInfoId) {
    if (!seatInfoId) return;
    const id = String(seatInfoId);
    // Its own map, not takenUntil. "We could not reach it" and "someone else is
    // holding it" are different facts with different consequences: only the
    // first should suppress a fresh 0->1 transition, because a buyer abandoning
    // a cart is precisely the cancellation 취켓팅 exists to catch.
    const until = nowMs() + UNREACHABLE_COOLDOWN_MS;
    if ((seatState.unreachableUntil.get(id) || 0) >= until) return;
    seatState.unreachableUntil.set(id, until);
    // Counted apart from takenConflicts. Which of the two is climbing says
    // whether we are losing to other people or to our own seat map.
    seatState.unreachableSkips = (seatState.unreachableSkips || 0) + 1;
  }

  function seatUnreachableNow(seatInfoId) {
    const until = seatState.unreachableUntil.get(String(seatInfoId));
    if (!until) return false;
    if (nowMs() >= until) {
      seatState.unreachableUntil.delete(String(seatInfoId));
      return false;
    }
    return true;
  }

  /**
   * Already in our own cart.
   *
   * The bitmap trails a selection by whole ticks — only two blocks are re-read
   * per tick — so on a 매수 2 order the seat we just took still reads free, gets
   * picked again as the adjacent pair, and the click comes back
   * 이미 선택된 좌석입니다. That conflict is one the macro generates itself.
   */
  function seatHeldByUs(seatInfoId) {
    return seatState.heldSeatIds.has(String(seatInfoId));
  }

  function seatInCooldown(seatInfoId) {
    const until = seatState.takenUntil.get(String(seatInfoId));
    if (!until) return false;
    if (nowMs() >= until) {
      seatState.takenUntil.delete(String(seatInfoId));
      return false;
    }
    return true;
  }

  function sweepTakenCooldowns() {
    // 취켓팅 runs unbounded, so this map would otherwise grow for the whole
    // sitting.
    const now = nowMs();
    for (const [id, until] of seatState.takenUntil) {
      if (now >= until) seatState.takenUntil.delete(id);
    }
    for (const [id, until] of seatState.unreachableUntil) {
      if (now >= until) seatState.unreachableUntil.delete(id);
    }
  }

  // Which 구역 is open, measured rather than remembered.
  //
  // seatState.blockEntered is only set when *we* opened a block, and the user
  // normally opens it themselves — so every "am I in the right block" test read
  // as false and the switch never fired. That is the ordinary case: sit in one
  // 구역, watch a seat free in another, never reach it. Every rendered seat
  // already carries props.blockKey, so the answer is in the DOM.
  /**
   * seatInfoId -> blockKey, built once per venue instead of scanned per seat.
   *
   * The rendered seat does not always carry its block: measured on a live
   * venue, all 273 drawn seats came back with blockKey undefined, which
   * silently disabled every "which 구역 am I in" test. seatMeta knows, and we
   * already hold it per block, so look the seat up there.
   *
   * It used to look it up with a nested loop over every seat in the house, and
   * every caller asks per *drawn* seat. On a 21,600-seat venue with the 구역
   * open that is 1,800 x 21,600 string comparisons for one answer —
   * currentOpenBlock() measured at 913ms, on the travel decision the watch
   * makes after a seat frees. The index costs one pass over the same data and
   * turns each lookup into a hash hit.
   *
   * Rebuilt whenever the seat data changes. lastBlocks is replaced wholesale by
   * pollFreedSeats and grows a block at a time as batches land, so identity
   * alone is not enough to notice — the seat total is checked too. Masks are
   * mutated in place and do not affect this map.
   */
  const seatBlockIndex = { byId: new Map(), from: null, seats: -1 };

  function seatBlockLookup() {
    const blocks = seatState.lastBlocks || [];
    let seats = 0;
    for (const block of blocks) seats += (block.seats || []).length;
    if (seatBlockIndex.from === blocks && seatBlockIndex.seats === seats) {
      return seatBlockIndex.byId;
    }
    const byId = new Map();
    for (const block of blocks) {
      const key = String(block.blockKey);
      for (const seat of block.seats || []) byId.set(String(seat.seatInfoId), key);
    }
    seatBlockIndex.byId = byId;
    seatBlockIndex.from = blocks;
    seatBlockIndex.seats = seats;
    return byId;
  }

  function blockKeyForSeatId(seatInfoId) {
    return seatBlockLookup().get(String(seatInfoId)) || null;
  }

  function currentOpenBlock(readSeat = seatFromFiber) {
    const tally = new Map();
    let seen = 0;
    for (const node of collectSeatCircles()) {
      const seat = readSeat(node);
      if (!seat?.seatInfoId) continue;
      const key = seat.blockKey || blockKeyForSeatId(seat.seatInfoId);
      if (!key) continue;
      seen += 1;
      tally.set(String(key), (tally.get(String(key)) || 0) + 1);
    }
    if (seen < 3) return null;
    let best = null;
    let bestCount = 0;
    for (const [key, count] of tally) {
      if (count > bestCount) {
        best = key;
        bestCount = count;
      }
    }
    return best;
  }

  // ---- Entering a block on a venue that draws no seats ---------------------
  //
  // A stadium's first screen is a picture of the venue: 26011315 has 43 blocks
  // and 28,932 seats in the API and *zero* seat circles in the DOM, because the
  // page draws the block map as a bitmap with a hit-test overlay. There is
  // nothing to pan toward and no element to query for — the seats do not exist
  // until a 구역 is opened. So the macro has to open one, the same way a person
  // does, and the only question is where to click.
  //
  // block-data gives each block an absolute box, but not the space it is in:
  // the boxes reach x=1475 while the overlay viewBox is 1214 wide, so they are
  // not overlay coordinates. Rather than guess, each mapping below is *tried*
  // and judged by whether seats actually appeared. A click on bare venue image
  // does nothing, so a wrong guess costs a few hundred milliseconds and no
  // more. Whichever works is remembered for the rest of the show.

  const BLOCK_ENTRY_HYPOTHESES = ["extent-to-image", "viewbox-fit", "extent-to-overlay"];

  function venueImageRect() {
    const images = [...document.querySelectorAll("img")]
      .map((img) => ({ img, box: img.getBoundingClientRect() }))
      .filter(({ box }) => box.width > 200 && box.height > 200)
      .sort((a, b) => b.box.width * b.box.height - a.box.width * a.box.height);
    return images.length ? images[0].box : null;
  }

  function overlayFit() {
    const svg = document.querySelector("svg");
    if (!svg) return null;
    const box = svg.getBoundingClientRect();
    const vb = String(svg.getAttribute("viewBox") || "").split(/\s+/).map(Number);
    if (vb.length !== 4 || !vb[2] || !vb[3]) return null;
    // preserveAspectRatio defaults to xMidYMid meet. This reproduces the venue
    // image's measured position exactly, so it is arithmetic, not a guess.
    const scale = Math.min(box.width / vb[2], box.height / vb[3]);
    return {
      scale,
      x: box.x + (box.width - vb[2] * scale) / 2,
      y: box.y + (box.height - vb[3] * scale) / 2,
      viewBox: { w: vb[2], h: vb[3] },
    };
  }

  function blockAbsoluteExtent() {
    const boxed = (seatState.discoveredBlocks || []).filter((b) => b.absoluteLeft != null);
    if (!boxed.length) return null;
    return {
      left: Math.min(...boxed.map((b) => b.absoluteLeft)),
      top: Math.min(...boxed.map((b) => b.absoluteTop)),
      right: Math.max(...boxed.map((b) => b.absoluteRight)),
      bottom: Math.max(...boxed.map((b) => b.absoluteBottom)),
    };
  }

  function blockClickPoint(block, hypothesis) {
    if (!block || block.absoluteLeft == null) return null;
    const cx = (block.absoluteLeft + block.absoluteRight) / 2;
    const cy = (block.absoluteTop + block.absoluteBottom) / 2;

    if (hypothesis === "extent-to-image" || hypothesis === "extent-to-overlay") {
      const extent = blockAbsoluteExtent();
      const target =
        hypothesis === "extent-to-image" ? venueImageRect() : document.querySelector("svg")?.getBoundingClientRect();
      if (!extent || !target) return null;
      const spanX = extent.right - extent.left || 1;
      const spanY = extent.bottom - extent.top || 1;
      // Where the block sits within the venue, as a fraction, laid onto the
      // drawing. Independent of whatever units the API chose.
      return {
        clientX: target.x + ((cx - extent.left) / spanX) * target.width,
        clientY: target.y + ((cy - extent.top) / spanY) * target.height,
      };
    }

    if (hypothesis === "viewbox-fit") {
      const fit = overlayFit();
      if (!fit) return null;
      return { clientX: fit.x + cx * fit.scale, clientY: fit.y + cy * fit.scale };
    }
    return null;
  }

  function describePoint(point) {
    const el = document.elementFromPoint(point.clientX, point.clientY);
    if (!el) return { hit: null };
    const rect = el.getBoundingClientRect();
    return {
      hit: `${el.tagName}.${String(el.getAttribute("class") || "").split(/\s+/)[0].slice(0, 28)}`,
      size: { w: Math.round(rect.width), h: Math.round(rect.height) },
      pointerEvents: window.getComputedStyle?.(el)?.pointerEvents || null,
      parent: el.parentElement
        ? `${el.parentElement.tagName}.${String(el.parentElement.getAttribute("class") || "").split(/\s+/)[0].slice(0, 28)}`
        : null,
      handlers: (() => {
        const key = Object.keys(el).find((k) => k.startsWith("__reactProps"));
        return key ? Object.keys(el[key] || {}).filter((k) => /^on/.test(k)).slice(0, 8) : [];
      })(),
    };
  }

  function insideMap(el) {
    if (!el) return false;
    const map = seatMapRoot() || document.querySelector("svg")?.parentElement;
    if (!map) return false;
    return map === el || map.contains(el);
  }

  function clickVenueAt(point) {
    const target = document.elementFromPoint(point.clientX, point.clientY);
    if (!target) return false;
    // The point is computed from block coordinates, and a wrong mapping puts it
    // anywhere on the page — including the header. Only ever dispatch into the
    // map itself; a miss should do nothing rather than press 마이페이지.
    if (!insideMap(target) && !/^(svg|image|img|rect|path|g|circle)$/i.test(target.tagName)) {
      traceCall("clickVenueAt", null, { refused: target.tagName, cls: String(target.getAttribute?.("class") || "").slice(0, 40) });
      return false;
    }
    const shared = {
      bubbles: true,
      cancelable: true,
      composed: true,
      clientX: point.clientX,
      clientY: point.clientY,
      view: window,
    };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      const Ctor = type.startsWith("pointer") && typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
      try {
        target.dispatchEvent(new Ctor(type, type.startsWith("pointer") ? { ...shared, pointerId: 1, isPrimary: true } : shared));
      } catch (error) {
        /* keep going: some event types are not constructible everywhere */
      }
    }
    return true;
  }

  // Getting back out to the venue view, so a different 구역 can be opened.
  //
  // Entering a block is only half of it: the bitmap finds a freed seat anywhere
  // across all 43 blocks, and it will usually not be in the one that happens to
  // be open. Without a way out the macro sits inside block 001 watching a seat
  // free up in block 017 that it can never reach.
  //
  // Judged the same way as entry — by whether the seats went away — so a
  // control that does nothing is detected rather than assumed to have worked.
  // Anything this returns will be clicked, so it must never contain a control
  // that leaves the page.
  //
  // The first version was dangerous. It skipped its position filter entirely
  // when the map element could not be found — `if (box)` — so it fell back to
  // every button and link in the document, it included `a` anchors, and
  // leaveBlockToVenue clicked them one by one until the seats disappeared.
  // Navigating away makes seats disappear, so pressing 마이페이지 or a logout
  // link scored as success. It really did press them.
  //
  // Now: no map, no controls. Never an anchor. Must be inside the map element,
  // not merely near it. And a denylist for anything that reads like leaving.
  const NEVER_CLICK = /마이|로그|예매확인|취소|나가|홈|home|logout|login|mypage|back|이전/i;

  function mapAreaControls() {
    const map = seatMapRoot() || document.querySelector("svg")?.parentElement;
    const box = map?.getBoundingClientRect?.();
    // Without a known map area we cannot tell a zoom button from a logout
    // link, and guessing is how the macro pressed 마이페이지.
    if (!map || !box?.width) return [];

    const out = [];
    // Deliberately no `a`: an anchor navigates, and nothing we want here is one.
    for (const el of map.querySelectorAll("button,[role=button]")) {
      const rect = el.getBoundingClientRect();
      if (rect.width < 14 || rect.height < 14 || rect.width > 120 || rect.height > 120) continue;
      // Inside the map's own box, with a small allowance for a floating zoom
      // cluster that overhangs its edge.
      if (rect.left < box.left - 20 || rect.right > box.right + 40) continue;
      if (rect.top < box.top - 20 || rect.bottom > box.bottom + 40) continue;
      const label = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${el.textContent || ""}`;
      if (NEVER_CLICK.test(label)) continue;
      if (el.closest("a,[href]")) continue;
      out.push(el);
      if (out.length >= 10) break;
    }
    return out;
  }

  function describeControl(el) {
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      cls: String(el.getAttribute("class") || "").split(/\s+/)[0].slice(0, 30),
      label: `${el.getAttribute("aria-label") || ""}${el.getAttribute("title") || ""}${(el.textContent || "").trim()}`.slice(0, 24),
      box: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    };
  }

  // Getting back out to the venue view, so a different 구역 can be opened.
  //
  // Entering a block is only half of it: the bitmap finds a freed seat anywhere
  // across all 43 blocks, and it will usually not be in whichever one happens
  // to be open. Without a way out the macro sits inside block 001 watching a
  // seat free up in block 017 that it can never reach.
  //
  // Which control does this is not documented and guessing wrongly is what the
  // first attempt did — a labelled-button search, a background double-click and
  // Escape all left it inside. So every control in the map area is tried in
  // turn and judged by whether the seats actually went away. Whichever works is
  // remembered, so the cost is paid once per show.
  function mapControlByLabel(pattern) {
    return mapAreaControls().find((el) => pattern.test(describeControl(el).label));
  }

  // Fit the open 구역 to the viewport so every seat in it is mounted at once.
  //
  // The map is virtualised by viewport, so panning seat-by-seat is a losing
  // game. This venue exposes 좌석도 전체보기, and clicking it left all 2,201
  // seats of the open block in the DOM — which means the whole block becomes
  // clickable in one action and there is nothing left to pan toward.
  async function fitBlockToView({ settleMs = 700 } = {}) {
    const button = mapControlByLabel(/전체보기/);
    if (!button) return { ok: false, via: "no-fit-button" };
    const before = collectSeatCircles().length;
    button.click();
    const started = Date.now();
    while (Date.now() - started < settleMs) {
      await sleep(100);
      const now = collectSeatCircles().length;
      if (now >= before) {
        return { ok: true, seats: now, gained: now - before, ms: Date.now() - started };
      }
    }
    return { ok: true, seats: collectSeatCircles().length, ms: Date.now() - started };
  }

  async function leaveBlockToVenue({ settleMs = 700 } = {}) {
    if (collectSeatCircles().length < 3) {
      seatState.blockEntered = "";
      return { ok: true, via: "already-out" };
    }

    const tryAndCheck = async (label, act, waitMs = settleMs) => {
      try {
        act();
      } catch (error) {
        return null;
      }
      const started = Date.now();
      while (Date.now() - started < waitMs) {
        await sleep(100);
        // Still on the seat map? Leaving the site also empties the map, and
        // treating that as "we left the block" is what let a stray click on a
        // navigation control look like success.
        if (!isSeatPage()) {
          seatState.lastError = "예매 창이 좌석맵을 벗어났습니다. 예매하기부터 다시 들어오세요.";
          return { ok: false, via: "navigated-away" };
        }
        if (collectSeatCircles().length < 3) {
          seatState.blockEntered = "";
          seatState.leaveControl = label;
          // Normalise the view before returning. One zoom-out step drops the
          // seats but leaves an intermediate zoom, and a block click computed
          // against the full-venue layout then lands nowhere — measured: leave
          // succeeded in 206ms and the very next re-entry failed on all three
          // mappings. 전체보기 puts the venue back to a known framing.
          const fit = mapControlByLabel(/전체보기/);
          if (fit) {
            try {
              fit.click();
              await sleep(250);
            } catch (error) {
              /* the view is still usable without it */
            }
          }
          return { ok: true, via: label, ms: Date.now() - started };
        }
      }
      return null;
    };

    // A control already known to work on this show.
    if (seatState.leaveControl) {
      const known = mapAreaControls().find(
        (el) => describeControl(el).label === seatState.leaveControl,
      );
      if (known) {
        const hit = await tryAndCheck(seatState.leaveControl, () => known.click());
        if (hit) return hit;
      }
    }

    // Zooming out step by step is the likeliest way back: 좌석도 전체보기 only
    // fits the open block to the screen — measured, it left all 2,201 of its
    // seats mounted — so it is not a way out at all.
    const zoomOut = mapControlByLabel(/축소/);
    if (zoomOut) {
      for (let step = 0; step < 8; step += 1) {
        const hit = await tryAndCheck("zoom-out", () => zoomOut.click(), 250);
        if (hit) return { ...hit, steps: step + 1 };
      }
    }

    const controls = mapAreaControls();
    // Bottom-most first: on this map the zoom cluster sits at the bottom right.
    controls.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
    for (const el of controls) {
      const info = describeControl(el);
      if (/확대|등급|예매대기/.test(info.label)) continue; // never zoom in or open a panel
      const hit = await tryAndCheck(info.label || `${info.tag}.${info.cls}`, () => el.click());
      if (hit) return { ...hit, control: info };
    }

    const map = seatMapRoot();
    const box = map?.getBoundingClientRect?.();
    if (box) {
      const escaped = await tryAndCheck("escape", () =>
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
      );
      if (escaped) return escaped;
    }
    return {
      ok: false,
      via: "still-inside",
      seats: collectSeatCircles().length,
      tried: controls.map(describeControl),
    };
  }

  async function enterBlockForSeats(block, { settleMs = 900, retried = false } = {}) {
    if (!block) return { ok: false, via: "no-block" };

    // Square the venue up first, every time.
    //
    // The click point is computed against the venue at its normal framing. Once
    // the view has been zoomed or panned the same coordinates land somewhere
    // else entirely: measured, (231,159) opened block 001 from a fresh page and
    // then hit nothing at all after a single zoom-out step. Normalising costs
    // ~250ms once and removes the whole class of failure.
    // Nothing can be clicked through a modal backdrop.
    for (let clear = 0; clear < 3 && dismissAnyBlockingOverlay(); clear += 1) {
      await sleep(200);
    }

    const fitFirst = mapControlByLabel(/전체보기/);
    if (fitFirst && !retried) {
      try {
        fitFirst.click();
        await sleep(260);
      } catch (error) {
        /* usable without it */
      }
    }

    const before = collectSeatCircles().length;
    // A mapping that has already worked is tried *first*, not alone.
    //
    // It used to be the whole list, and it is never cleared — so after one
    // venue taught it "viewbox-fit", a different venue whose mapping is
    // "extent-to-image" got one wrong click, the full settle wait, and a
    // failure, with the two mappings that would have worked never tried. The
    // 구역 simply never opened on the second show of a session.
    const learned = seatState.blockEntryHypothesis;
    const order = learned
      ? [learned, ...BLOCK_ENTRY_HYPOTHESES.filter((h) => h !== learned)]
      : BLOCK_ENTRY_HYPOTHESES;

    for (const hypothesis of order) {
      const point = blockClickPoint(block, hypothesis);
      if (!point) continue;
      if (!clickVenueAt(point)) continue;

      const started = Date.now();
      while (Date.now() - started < settleMs) {
        await sleep(120);
        const now = collectSeatCircles().length;
        if (now > before + 2) {
          // Seats appeared: this mapping is the right one for this venue.
          seatState.blockEntryHypothesis = hypothesis;
          seatState.blockEntered = block.blockKey;
          return { ok: true, hypothesis, seats: now, ms: Date.now() - started };
        }
      }
    }
    // Missed on every mapping. Usually that means the venue is not at its
    // normal framing, so square it up and try once more before giving up.
    seatState.blockEntryMisses = (seatState.blockEntryMisses || 0) + 1;
    return {
      ok: false,
      via: "no-seats-appeared",
      tried: order.length,
      fitButton: Boolean(fitFirst),
    };
  }

  // ---- Reaching a seat the map has not drawn -------------------------------
  //
  // The map virtualises: only seats inside the viewport exist in the DOM, and
  // selectSeats is map-click only because the API path locks seats server-side
  // while leaving 선택 좌석 empty. So on a stadium most candidates are simply
  // unreachable until the view moves to them.
  //
  // The venue->screen mapping is measured, not assumed. Seats already on screen
  // carry both their venue coordinates (posLeft/posTop, the same space the
  // picker draws) and a real getBoundingClientRect, so two of them determine
  // the scale and offset. That avoids depending on the SVG viewBox or on any
  // particular zoom library, and it re-measures itself after every move.

  function calibrateVenueToScreen(readSeat = seatFromFiber) {
    const samples = [];
    for (const node of collectSeatCircles()) {
      const seat = readSeat(node);
      const vx = numOrNull(seat?.posLeft);
      const vy = numOrNull(seat?.posTop);
      if (vx == null || vy == null) continue;
      const box = node.getBoundingClientRect();
      if (!box.width && !box.height) continue;
      samples.push({ vx, vy, sx: box.left + box.width / 2, sy: box.top + box.height / 2 });
      if (samples.length >= 240) break;
    }
    if (samples.length < 2) return null;

    const spread = (values) => Math.max(...values) - Math.min(...values);
    const mean = (values) => values.reduce((a, b) => a + b, 0) / values.length;
    const vxs = samples.map((s) => s.vx);
    const vys = samples.map((s) => s.vy);
    const sxs = samples.map((s) => s.sx);
    const sys = samples.map((s) => s.sy);

    // One uniform scale, taken from whichever axis is better spread — a single
    // row of seats has no vertical spread to measure from.
    let scale = null;
    if (spread(vxs) > 1 && spread(sxs) > 1) scale = spread(sxs) / spread(vxs);
    else if (spread(vys) > 1 && spread(sys) > 1) scale = spread(sys) / spread(vys);
    if (!scale || !Number.isFinite(scale) || scale <= 0) return null;

    const ox = mean(sxs) - mean(vxs) * scale;
    const oy = mean(sys) - mean(vys) * scale;
    return {
      scale,
      samples: samples.length,
      toScreen: (vx, vy) => ({ x: ox + vx * scale, y: oy + vy * scale }),
    };
  }

  function findMapTransform() {
    const roots = [seatMapRoot(), ...document.querySelectorAll('[class*="eatMap"],[class*="placeImg"]')]
      .filter(Boolean)
      .slice(0, 6);
    for (const el of roots) {
      const fiberKey = Object.keys(el).find(
        (key) => key.startsWith("__reactFiber") || key.startsWith("__reactInternalInstance"),
      );
      if (!fiberKey) continue;
      let fiber = el[fiberKey];
      for (let depth = 0; depth < 60 && fiber; depth += 1) {
        const props = fiber.memoizedProps || fiber.pendingProps || {};
        for (const cand of [props.instance, props.zoomInstance, fiber.stateNode]) {
          if (!cand || typeof cand !== "object") continue;
          if (typeof cand.setTransform === "function" && cand.transformState) return cand;
        }
        fiber = fiber.return;
      }
    }
    return null;
  }

  // The user is dragging the map. Fighting them for the viewport is worse than
  // waiting a moment.
  let pointerHeldOnMap = false;
  function watchMapPointer() {
    if (window.__nolsniperMapPointerWatch) return;
    window.__nolsniperMapPointerWatch = true;
    window.addEventListener("pointerdown", () => { pointerHeldOnMap = true; }, true);
    for (const type of ["pointerup", "pointercancel"]) {
      window.addEventListener(type, () => { pointerHeldOnMap = false; }, true);
    }
  }

  function scrollableAncestor(node) {
    let walk = node;
    for (let up = 0; up < 8 && walk; up += 1) {
      if (walk.scrollWidth > walk.clientWidth + 4 || walk.scrollHeight > walk.clientHeight + 4) {
        return walk;
      }
      walk = walk.parentElement;
    }
    return null;
  }

  // Drag the map the way a person does.
  //
  // Measured on a live venue: with a block open and 1,674 seats drawn, the
  // page exposed no setTransform instance and no scrollable container, so
  // every aim failed with "no-transform" while 243 free seats sat off screen.
  // These maps are dragged with a pointer, and dispatching that drag needs no
  // library API at all — it is the one mechanism guaranteed to exist, because
  // it is the one the user themselves uses.
  function dragMapBy(dx, dy) {
    const root = seatMapRoot() || document.querySelector("svg")?.parentElement;
    const box = root?.getBoundingClientRect?.();
    if (!box?.width) return false;
    const startX = box.left + box.width / 2;
    const startY = box.top + box.height / 2;
    const target = document.elementFromPoint(startX, startY) || root;
    if (!target) return false;
    // A drag is a mousedown and mouseup — on a button that is a click. Only
    // drag inside the map.
    if (!insideMap(target) && target !== root) return false;

    const send = (type, x, y, extra = {}) => {
      const init = {
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX: x,
        clientY: y,
        view: window,
        buttons: type === "pointerup" || type === "mouseup" ? 0 : 1,
        ...extra,
      };
      const Ctor =
        type.startsWith("pointer") && typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
      try {
        target.dispatchEvent(new Ctor(type, type.startsWith("pointer") ? { ...init, pointerId: 1, isPrimary: true, pointerType: "mouse" } : init));
      } catch (error) {
        /* not every event type is constructible in every engine */
      }
    };

    // Dragging the content by -delta brings the target toward the centre.
    const endX = startX - dx;
    const endY = startY - dy;
    send("pointerdown", startX, startY);
    send("mousedown", startX, startY);
    // Several steps: a single jump reads as a click to most drag handlers.
    const STEPS = 6;
    for (let step = 1; step <= STEPS; step += 1) {
      const t = step / STEPS;
      send("pointermove", startX + (endX - startX) * t, startY + (endY - startY) * t);
      send("mousemove", startX + (endX - startX) * t, startY + (endY - startY) * t);
    }
    send("pointerup", endX, endY);
    send("mouseup", endX, endY);
    return true;
  }

  function panMapBy(dx, dy) {
    const transform = findMapTransform();
    if (transform) {
      const { scale, positionX, positionY } = transform.transformState || {};
      if (Number.isFinite(positionX) && Number.isFinite(positionY)) {
        try {
          transform.setTransform(positionX - dx, positionY - dy, scale, 0);
          return "setTransform";
        } catch (error) {
          /* fall through */
        }
      }
    }
    // Some venues render inside a plain scroll container — check ancestors, not
    // just the map root, which is often not the element that scrolls.
    const scroller = scrollableAncestor(seatMapRoot());
    if (scroller) {
      scroller.scrollBy({ left: dx, top: dy });
      return "scroll";
    }
    if (dragMapBy(dx, dy)) return "drag";
    return null;
  }

  async function ensureSeatRendered(seatInfoId, seat, { timeoutMs = 1500, readSeat = seatFromFiber } = {}) {
    if (seatNodeFor(seatInfoId, readSeat)) return { ok: true, via: "already" };
    const vx = numOrNull(seat?.posLeft);
    const vy = numOrNull(seat?.posTop);
    if (vx == null || vy == null) return { ok: false, via: "no-coords" };
    // Never wrestle the page while it is blocked or while the user is dragging.
    if (seatTakenDialogVisible() || seatErrorDialogVisible()) return { ok: false, via: "dialog" };
    if (pointerHeldOnMap) return { ok: false, via: "user-dragging" };

    const calibration = calibrateVenueToScreen(readSeat);
    if (!calibration) return { ok: false, via: "no-calibration" };

    const root = seatMapRoot();
    const box = root?.getBoundingClientRect?.();
    if (!box?.width) return { ok: false, via: "no-map" };

    const target = calibration.toScreen(vx, vy);
    const dx = target.x - (box.left + box.width / 2);
    const dy = target.y - (box.top + box.height / 2);
    // Already centred and still not drawn: moving will not help.
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return { ok: false, via: "centred-but-absent" };

    const started = Date.now();
    const how = panMapBy(dx, dy);
    if (!how) return { ok: false, via: "no-transform" };

    while (Date.now() - started < timeoutMs) {
      await sleep(80);
      if (seatNodeFor(seatInfoId, readSeat)) {
        return { ok: true, via: how, ms: Date.now() - started, scale: calibration.scale };
      }
    }
    return { ok: false, via: how, ms: Date.now() - started, timedOut: true };
  }

  // ---- Watching the map instead of scanning it ------------------------------
  //
  // renderedSeatIndex walks every circle and reads a React fiber per node, on
  // every tick, to answer one question: is the seat we want drawn and enabled.
  // A MutationObserver answers it as the page changes, so the hot path becomes
  // a map lookup — and, more importantly, the moment a seat flips to available
  // is known immediately rather than up to a tick later.
  const seatIndex = {
    byId: new Map(),
    observer: null,
    root: null,
    built: 0,
  };

  function indexSeatNode(node) {
    const seat = seatFromFiber(node);
    const id = seat?.seatInfoId ? String(seat.seatInfoId) : null;
    if (!id) return;
    seatIndex.byId.set(id, node);
  }

  function forgetSeatNode(node) {
    const seat = seatFromFiber(node);
    const id = seat?.seatInfoId ? String(seat.seatInfoId) : null;
    if (id && seatIndex.byId.get(id) === node) seatIndex.byId.delete(id);
  }

  function rebuildSeatIndex() {
    seatIndex.byId.clear();
    for (const node of collectSeatCircles()) indexSeatNode(node);
    seatIndex.built = Date.now();
    return seatIndex.byId;
  }

  function watchSeatMap() {
    const root = seatMapRoot() || document.querySelector("svg")?.parentElement;
    if (!root) return false;
    if (seatIndex.observer && seatIndex.root === root) return true;
    if (seatIndex.observer) seatIndex.observer.disconnect();
    seatIndex.root = root;
    rebuildSeatIndex();
    try {
      seatIndex.observer = new MutationObserver((records) => {
        for (const record of records) {
          if (record.type === "childList") {
            // Circles came or went: the round on screen may have changed, so
            // the cached DOM round is stale until the next look.
            invalidateLivePlaySeq();
            // The map mounts and unmounts circles as the viewport moves, and
            // only the mounting half was handled. Unmounted seats stayed in the
            // index pointing at detached nodes, so a seat could be counted as
            // clickable and then "clicked" into nothing — the run waited for a
            // cart that could never change and discarded a real seat as
            // unselectable.
            for (const node of record.removedNodes || []) {
              if (node.nodeType !== 1) continue;
              if (node.tagName === "circle") forgetSeatNode(node);
              else if (node.querySelectorAll) {
                for (const inner of node.querySelectorAll("circle")) forgetSeatNode(inner);
              }
            }
            for (const node of record.addedNodes || []) {
              if (node.nodeType !== 1) continue;
              if (node.tagName === "circle") indexSeatNode(node);
              else if (node.querySelectorAll) {
                for (const inner of node.querySelectorAll("circle")) indexSeatNode(inner);
              }
            }
            continue;
          }
          if (record.target?.tagName === "circle") indexSeatNode(record.target);
        }
        // A seat we are waiting on may have just become available.
        checkDomAgreement();
      });
      seatIndex.observer.observe(root, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: ["class", "fill", "aria-disabled", "style"],
      });
      return true;
    } catch (error) {
      seatIndex.observer = null;
      return false;
    }
  }

  // The index, kept fresh by the observer. Falls back to a full scan when no
  // observer could be attached, so a venue whose subtree we cannot find behaves
  // exactly as it did before.
  // Which of these seats can actually be clicked right now. Recomputed rather
  // than computed once, because opening a 구역 changes the answer and the whole
  // point is to act on that without going back round the loop.
  /**
   * Time a map move, and keep what it cost.
   *
   * The settle budgets these run against (900/700/250 ms) are ceilings someone
   * chose, not costs anyone measured — the only real figure is a 389 ms note in
   * a comment. Travelling to a seat is the largest latency in 취켓팅 once one
   * frees, so tuning it needs the actual distribution rather than another
   * guess. Recorded per kind so one watch answers it.
   */
  async function noteMapMove(kind, key, run) {
    const started = performance.now();
    const result = await run();
    const ms = Math.round(performance.now() - started);
    const seen = (seatState.mapMoves[kind] ||= { n: 0, totalMs: 0, worstMs: 0, failed: 0 });
    seen.n += 1;
    seen.totalMs += ms;
    seen.worstMs = Math.max(seen.worstMs, ms);
    if (result && result.ok === false) seen.failed += 1;
    traceCall(kind, key, { ...result, ms });
    return result;
  }

  // ---- What a catch actually costs, segment by segment ---------------------
  //
  // 취켓팅 is lost in the gap between a seat becoming free and the hold landing
  // on the page, and until now the only figure for that gap was one number
  // (catchLatencyMs) covering detect->click alone. The rest — the site's own
  // preselect round trip, the quiet gap we hold before 선택 완료, the confirm
  // itself — was invisible, so every attempt to make it faster was a guess.
  //
  // Four stamps, all relative to the instant the availability bitmap flipped
  // 0->1 for the seat we went for:
  //
  //   click          a real pointer press left our hands
  //   preselectSent  the page's OWN preselect request left the browser
  //   preselectDone  the server answered it
  //   cart           선택 좌석 rose — the hold is on the page, not just on a server
  //   confirm        선택 완료 was pressed
  //   outcome        the page answered it
  //
  // click -> preselectSent is the one that decides whether an API soft hold
  // could ever beat the map click. Everything from preselectSent onwards is the
  // site's own round trip, which any path pays; only what sits in front of it
  // is winnable. Measuring it is what turns that question from an argument into
  // a number.
  //
  // Kept as a short history rather than a single reading: one catch on a quiet
  // show says nothing about an open, and a median over a sitting is what tells
  // a real regression from a slow server.
  const CATCH_TIMING_KEEP = 12;

  function startCatchTiming(detectedAtPerf) {
    seatState.catchTiming = {
      detect: detectedAtPerf,
      at: new Date().toISOString().slice(11, 23),
      stages: {},
    };
  }

  function noteCatchStage(stage, atPerf = performance.now()) {
    const timing = seatState.catchTiming;
    // First stamp wins. A retry within the same catch is a different seat's
    // story, and overwriting would quietly turn a slow segment into a fast one.
    if (!timing || timing.stages[stage] != null) return;
    timing.stages[stage] = Math.round(atPerf - timing.detect);
  }

  function finishCatchTiming(outcome) {
    const timing = seatState.catchTiming;
    if (!timing) return;
    seatState.catchTiming = null;
    const stages = timing.stages;
    // Nothing was clicked: no segment to report, and keeping it would poison
    // the medians with attempts that never reached the map.
    if (stages.click == null) return;
    const gap = (from, to) =>
      stages[from] != null && stages[to] != null ? stages[to] - stages[from] : null;
    const record = {
      at: timing.at,
      outcome,
      detectToClick: stages.click,
      // How long the page sat on our press before asking the server. This is
      // the whole of what a parallel API hold could save.
      clickToPreselect: gap("click", "preselectSent"),
      preselectMs: gap("preselectSent", "preselectDone"),
      holdToCart: gap("preselectDone", "cart"),
      clickToCart: gap("click", "cart"),
      cartToConfirm: gap("cart", "confirm"),
      confirmToOutcome: gap("confirm", "outcome"),
      totalMs: stages.outcome ?? stages.confirm ?? stages.cart ?? stages.click,
    };
    seatState.catchTimings.push(record);
    while (seatState.catchTimings.length > CATCH_TIMING_KEEP) seatState.catchTimings.shift();
    traceCall("catchTiming", null, record);
  }

  /**
   * The last catch, in the four numbers that decide it, for the 예매 창 overlay.
   *
   * On the map rather than only in the status file, because this is the figure
   * you want the moment a catch lands — and because a segment that suddenly
   * doubles on one show is how a slow server or a lost parking spot announces
   * itself. Empty until a catch has actually completed a segment, so it never
   * shows a row of dashes.
   */
  function catchTimingLine() {
    const timing = seatState.catchTimings[seatState.catchTimings.length - 1];
    if (!timing) return "";
    const parts = [];
    if (timing.detectToClick != null) parts.push(`감지→클릭 ${timing.detectToClick}`);
    if (timing.clickToPreselect != null) parts.push(`→가선점요청 ${timing.clickToPreselect}`);
    if (timing.clickToCart != null) parts.push(`→선택좌석 ${timing.clickToCart}`);
    if (timing.cartToConfirm != null) parts.push(`→선택완료 ${timing.cartToConfirm}`);
    return parts.length ? `${parts.join(" · ")} ms` : "";
  }

  // Median per segment over the sitting, which is what a tuning decision needs.
  function catchTimingSummary() {
    const rows = seatState.catchTimings || [];
    if (!rows.length) return null;
    const median = (field) => {
      const values = rows.map((row) => row[field]).filter((value) => value != null).sort((a, b) => a - b);
      return values.length ? values[Math.floor(values.length / 2)] : null;
    };
    return {
      samples: rows.length,
      detectToClick: median("detectToClick"),
      clickToPreselect: median("clickToPreselect"),
      preselectMs: median("preselectMs"),
      holdToCart: median("holdToCart"),
      clickToCart: median("clickToCart"),
      cartToConfirm: median("cartToConfirm"),
      confirmToOutcome: median("confirmToOutcome"),
      totalMs: median("totalMs"),
      last: rows[rows.length - 1],
    };
  }

  // Which seat to travel to, when none is drawn yet. See the note at the call
  // site: reachability beats distance, because the travel costs more than the
  // difference between two seats usually does.
  // Staying in the block already open is usually right — a block switch costs
  // more than the gap between two seats. But not when the open block is a wing
  // and a much better (central) seat is free elsewhere: honouring the open
  // block then meant the grab took whatever side the map happened to boot on
  // ("무조건 오른쪽"). Switch once when the open block's best seat is far down
  // the ranked list; otherwise keep the fast path.
  const OPEN_BLOCK_KEEP_RANK = 6;
  function aimForCandidates(candidates, openBlock) {
    if (openBlock) {
      const hereIndex = candidates.findIndex((seat) => String(seat.blockKey) === String(openBlock));
      if (hereIndex >= 0) {
        const tolerance = Math.max(OPEN_BLOCK_KEEP_RANK, Math.ceil(candidates.length * 0.05));
        // The open block holds a near-best seat: no switch worth its cost.
        if (hereIndex <= tolerance) return candidates[hereIndex];
        // The open block is a wing while the top of the list is elsewhere —
        // aim at the best seat so the loop moves to its (more central) block.
      }
    }
    return candidates[0] || null;
  }

  /**
   * Has the venue moved since we last swept because of it?
   *
   * Only ever *adds* a sweep. When the trigger is unusable — the show hides its
   * remaining counts, the round is not on sale (measured: rounds reporting
   * remainCnt 0 with 600+ seats still on the map), or the panel could not
   * reach the feed — this answers false and the rolling sweep carries on
   * exactly as before. Being unable to see is not the same as seeing nothing.
   */
  function triggerFired() {
    const trigger = seatState.watchTrigger;
    if (!trigger || trigger.usable !== true) return false;
    const changedAt = Number(trigger.changed_at) || 0;
    if (!changedAt || changedAt <= seatState.triggerActedAt) return false;
    seatState.triggerActedAt = changedAt;
    return true;
  }

  // The poller behind hyper-focus. Deliberately NOT a timer: WKWebView clamps
  // setInterval/setTimeout hard when the 예매 창 is not the frontmost window
  // (measured: a 30ms interval fired ~1×/s, the loop's 30ms sleep took 400ms).
  // Instead, FOCUS_WORKERS chained fetches each re-issue the moment their
  // answer lands, so the cadence is set by the round trip (~2 in flight ≈ one
  // reading every RTT/2), capped at CATCH_MAX_REQUESTS_PER_SEC. Each answer is
  // diffed in memory; an answer older than the last applied is dropped so a
  // stale mask can never resurrect a seat that has since been taken. And the
  // press happens right here, in the callback — not in a loop tick that may be
  // throttled — so detection→press is a function call, not a scheduler.
  const FOCUS_WORKERS = 2;
  // `epoch` counts spawns and `workers` counts live loops. A worker belongs to
  // the epoch that spawned it: a stop followed by a restart inside one fetch
  // round trip used to leave the old pair alive beside the new one (the
  // restart re-raised `active` before they had looked), and every such cycle
  // added two more loops against the 60/s budget.
  const focusPoller = { active: false, key: "", seq: 0, applied: 0, inFlight: 0, responses: [], gen: 0, sent: [], epoch: 0, workers: 0 };
  function stopFocusPoller() {
    focusPoller.active = false; focusPoller.key = "";
  }
  function focusPollerHz() {
    const now = performance.now();
    focusPoller.responses = focusPoller.responses.filter((t) => now - t < 1000);
    return focusPoller.responses.length;
  }
  function focusPollerCanSend() {
    const now = performance.now();
    focusPoller.sent = focusPoller.sent.filter((t) => now - t < 1000);
    return focusPoller.sent.length < CATCH_MAX_REQUESTS_PER_SEC && gatewayBlockRemainingMs() <= 0;
  }
  function focusPollerAlive(runGen, epoch = focusPoller.epoch) {
    // Deliberately NOT gated on runGen: a sold-out landing hands off to a
    // nested quiet-watch run (a fresh runGen) that owns the same focus block,
    // and gating on the starting run killed the workers at that handoff
    // (measured: 16 req then 0 on a sold-out block). A live catch on the same
    // block is the same watch, whichever run object is driving it. It IS gated
    // on the spawn epoch: a worker outlived by a stop+restart must not join
    // the pair the restart spawned.
    return focusPoller.active && epoch === focusPoller.epoch && seatState.running && !seatState.stopRequested
      && !seatState.locked && seatState.runMode === "catch" && seatState.catchFocusBlock === focusPoller.key;
  }
  // The floor on one worker's poll period. A bitmap request answers in ~20ms,
  // so this is normally free (the fetch already took it); it only bites when a
  // request resolves — or rejects — fast (a cached error, a 429), which is
  // exactly when two workers `continue`-spun on microtasks with no macrotask
  // yield and starved the renderer into Chrome's "페이지 응답 없음" dialog.
  // Flooring the period AND yielding a real macrotask every iteration keeps
  // ~50 req/s while guaranteeing the main thread paints at 60fps.
  const FOCUS_YIELD_MS = 20;
  async function focusWorker(initData, config, runGen, gradeOrder, blockKeys, epoch = focusPoller.epoch) {
    focusPoller.workers += 1;
    try {
      await focusWorkerLoop(initData, config, runGen, gradeOrder, blockKeys, epoch);
    } finally {
      focusPoller.workers = Math.max(0, focusPoller.workers - 1);
    }
  }
  async function focusWorkerLoop(initData, config, runGen, gradeOrder, blockKeys, epoch) {
    while (focusPollerAlive(runGen, epoch)) {
      if (focusPollerCanSend()) {
        const mySeq = ++focusPoller.seq;
        focusPoller.sent.push(performance.now());
        focusPoller.inFlight += 1;
        let masks = null;
        // The fetch itself paces the worker (~20ms RTT) and — unlike a timer —
        // is NOT clamped when the 예매 창 is backgrounded, so the rate holds.
        try { masks = await fetchMasksFor(initData, [focusPoller.key]); } catch (error) { masks = null; }
        focusPoller.inFlight = Math.max(0, focusPoller.inFlight - 1);
        focusPoller.responses.push(performance.now());
        if (focusPollerAlive(runGen, epoch) && mySeq > focusPoller.applied) {
          focusPoller.applied = mySeq;
          const block = (seatState.lastBlocks || []).find((b) => String(b.blockKey) === focusPoller.key);
          const freed = applyBlockMask(block, masks ? masks[0] || null : null, config);
          if (freed.length) {
            seatState.lastFreedVia = "focus";
            const ranked = rankCandidates(freed, gradeOrder, blockKeys, pickerOptions(config, { isCatch: true }));
            if ((Number(config.quantity) || 1) === 1 && !seatState.locked && !seatState.pressSequenceBusy) {
              // Detection→press→confirm→snap all happen in pressSequence, off
              // the poll loop; it has its own network awaits, never a spin.
              seatState.pressSequenceBusy = true;
              pressSequence(ranked, config).catch(() => {}).finally(() => { seatState.pressSequenceBusy = false; });
            } else {
              seatState.pageFreed.push(...ranked);
            }
          }
        }
        // A MessageChannel hop after every send — never throttled, and it hands
        // the renderer a frame. This is what stops two workers spinning on
        // microtasks (a fast-failing fetch) into 페이지 응답 없음, WITHOUT the
        // setTimeout floor that a background window would clamp to ~1s.
        await yieldFast();
      } else {
        // Rate-limited (60/s) or gateway-blocked: nothing to send, so idle a
        // real ~20ms here. A clamp when backgrounded is harmless — we are
        // deliberately waiting for the send window either way.
        await yieldFast();
        if (!focusPollerAlive(runGen, epoch)) break;
        await sleep(FOCUS_YIELD_MS);
      }
    }
    focusPoller.inFlight = Math.max(0, focusPoller.inFlight);
  }
  // Press a freed seat and lock it, fastest path there is:
  //   detect (bitmap flip, freedAtPerf) → native pointer press (<2ms)
  //   → page preselect answers (one RTT) → 선택 완료 pressed at once, no
  //     "sidebar quiet" wait → page select answers → held.
  // A taken answer marks the seat and presses the next freed one immediately.
  const PRESS_SNAP_MAX = 4;
  async function pressSequence(ranked, config) {
    const tried = [];
    // The run this press belongs to. A stop (or a fresh run) bumps the
    // generation, and a sequence parked on a network await must notice when
    // it wakes — without this, 전부 정지 pressed mid-hold still went on to
    // press 선택 완료 for the run that had just been stopped.
    const gen = window.__nolsniperRunGen;
    const halted = () => seatState.locked || seatState.stopRequested || runWasSuperseded(gen);
    const bail = (lat) => { finishCatchTiming("stopped"); if (lat) lat.outcome = "stopped"; };
    for (const seat of ranked.slice(0, PRESS_SNAP_MAX)) {
      if (halted()) return;
      if (!seatNodeFor(seat.seatInfoId)) continue;
      const detect = seat.freedAtPerf || performance.now();
      startCatchTiming(detect);
      const since = Date.now();
      const pressed = clickSeatOnMap(seat.seatInfoId, { countBefore: selectedSeatCount() });
      const pressAt = performance.now();
      if (!pressed) continue;
      noteCatchStage("click", pressAt);
      seatState.fastClickedId = String(seat.seatInfoId); seatState.fastClickedAt = nowMs();
      seatState.fastClicks = (seatState.fastClicks || 0) + 1;
      tried.push(seat.seatInfoId);
      const lat = (seatState.lastCatchLatency = { seat: seat.label || seat.seatInfoId, pressMs: +(pressAt - detect).toFixed(2), preselectMs: null, confirmMs: null, holdMs: null, outcome: "pressed" });
      const pre = await waitForSeatNet("preselect", since, 2500);
      if (pre.aborted || halted()) return bail(lat);
      lat.preselectMs = pre.at ? Math.round(performance.now() - detect) : null;
      if (pre.ok === false || pre.timeout) {
        // Lost the race (or the page refused): 0ms cool-down and snap on.
        markSeatTaken(seat.seatInfoId);
        finishCatchTiming(pre.timeout ? "preselect-timeout" : "taken");
        lat.outcome = pre.timeout ? "preselect-timeout" : "taken";
        continue;
      }
      // Confirm the instant the hold is acknowledged — wait only for the button
      // to exist (MessageChannel hops, not timers; bounded at 400ms).
      const confirmDeadline = performance.now() + 400;
      let confirmed = false;
      while (performance.now() < confirmDeadline) {
        if (halted()) return bail(lat);
        if (clickConfirmSelect()) { confirmed = true; break; }
        await yieldFast();
      }
      lat.confirmMs = confirmed ? Math.round(performance.now() - detect) : null;
      if (!confirmed) { lat.outcome = "no-confirm-button"; seatState.lastSeat = seat.label || ""; seatState.locked = true; return; }
      const sel = await waitForSeatNet("select", since, 3000);
      if (sel.aborted) return bail(lat);
      lat.holdMs = sel.at ? Math.round(performance.now() - detect) : null;
      if (sel.ok === false) {
        markSeatTaken(seat.seatInfoId);
        finishCatchTiming("select-rejected");
        lat.outcome = "select-rejected";
        continue;
      }
      noteCatchStage("outcome");
      finishCatchTiming(sel.timeout ? "unconfirmed" : "reserved");
      lat.outcome = sel.timeout ? "unconfirmed" : "reserved";
      seatState.locked = true; seatState.confirmStarted = true;
      seatState.lastSeat = seat.label || String(seat.seatInfoId);
      seatState.lastExit = "reservedUserContinues";
      seatState.lastSeatPos = { x: seat.posLeft, y: seat.posTop, block: seat.blockKey };
      updateOverlay(`예약 요청 ${seatState.lastSeat} · 감지→클릭 ${lat.pressMs}ms · 가선점 ${lat.preselectMs}ms · 확정 ${lat.holdMs}ms`, "ok");
      return;
    }
  }
  function startFocusPoller(initData, blockKey, config, runGen, gradeOrder = [], blockKeys = []) {
    // Already watching this block: adopt the caller's run, keep the workers.
    if (focusPoller.active && focusPoller.key === String(blockKey)) { focusPoller.gen = runGen; return; }
    stopFocusPoller();
    // A new epoch retires every worker still finishing a fetch from before.
    const epoch = ++focusPoller.epoch;
    focusPoller.active = true; focusPoller.key = String(blockKey); focusPoller.gen = runGen;
    for (let i = 0; i < FOCUS_WORKERS; i += 1) {
      void focusWorker(initData, config, runGen, gradeOrder, blockKeys, epoch).catch(() => {});
    }
  }
  // How long a freshly entered block gets to draw its circles before the loop
  // concludes the seats are not there.
  const BLOCK_DRAW_GRACE_MS = 3000;
  async function waitForClickable(candidates, budgetMs) {
    const started = Date.now();
    let found = clickableAmong(candidates);
    while (!found.length && Date.now() - started < budgetMs) {
      await sleep(100);
      found = clickableAmong(candidates);
    }
    return found;
  }

  function clickableAmong(candidates) {
    const rendered = liveSeatIndex();
    seatState.domCircleCount = rendered.size;
    return candidates.filter((seat) => {
      const node = rendered.get(String(seat.seatInfoId));
      if (!node || node.isConnected === false) return false;
      return !seatNodeDisabled(node);
    });
  }

  function liveSeatIndex() {
    if (!watchSeatMap()) return renderedSeatIndex();
    // A mounted circle can predate the observer; rebuild if it looks stale.
    if (!seatIndex.byId.size) rebuildSeatIndex();
    return seatIndex.byId;
  }

  function collectDomCandidates(gradeOrder, blockKeys = [], config = {}) {
    const nodes = collectSeatCircles();
    seatState.domCircleCount = nodes.length;
    const watch = normalizeWatchRect(config.watch_rect);
    const candidates = [];
    for (const node of nodes) {
      // Same correction as seatNodeDisabled: the hashed class is base styling,
      // present on every seat, so testing it emptied this list entirely.
      if (seatNodeDisabled(node)) continue;
      const seat = seatFromFiber(node);
      if (!seat?.seatInfoId || !seat?.seatGrade) continue;
      if (seat.isExposable === false) continue;
      if (seatInCooldown(seat.seatInfoId)) continue;
      if (seatUnreachableNow(seat.seatInfoId)) continue;
      if (seatHeldByUs(seat.seatInfoId)) continue;
      if (seat.seatGroupId && config.allow_group_seats === false) continue;
      if (!seatInWatchRect(seat, watch)) continue;
      const row = seat.rowNo || seat.areaName || "";
      const no = seat.seatNo || seat.entranceNo || "";
      candidates.push({
        seatInfoId: seat.seatInfoId,
        seatGrade: String(seat.seatGrade),
        seatGradeName: seat.seatGradeName || "",
        rowNo: row,
        seatNo: no,
        // Fall back to seatMeta: the circle often has no block of its own, and
        // a candidate without one skips the whole 구역 logic downstream.
        blockKey: seat.blockKey || blockKeyForSeatId(seat.seatInfoId),
        seatGroupId: seat.seatGroupId || null,
        posLeft: numOrNull(seat.posLeft),
        posTop: numOrNull(seat.posTop),
        label: `[${seat.seatGradeName || seat.seatGrade}] ${row} ${no}`.trim(),
      });
    }
    return rankCandidates(candidates, gradeOrder, blockKeys, pickerOptions(config));
  }

  // Only seats inside the current viewport exist in the DOM — the map keeps an
  // R-tree over seat positions and renders just the visible ones. Building this
  // index once per pass turns "is this seat clickable" into a map lookup rather
  // than a fresh DOM+fiber walk per candidate.
  function renderedSeatIndex() {
    const index = new Map();
    for (const node of collectSeatCircles()) {
      const seat = seatFromFiber(node);
      if (seat?.seatInfoId) index.set(String(seat.seatInfoId), node);
    }
    return index;
  }

  /**
   * The circle for one seat.
   *
   * Answered from the observer-maintained index when there is one. The scan
   * below is a linear walk of every mounted circle with a fiber read apiece,
   * and it sits on the two paths that decide the race: the click itself, and
   * checkDomAgreement, which asks once per watched seat on every tick and again
   * inside the mutation callback. Measured on an 1,800-circle 구역 that is
   * ~0.55ms per lookup against ~0.01ms from the index.
   *
   * The index is only consulted when the observer is actually attached and the
   * caller reads seats the same way the index was built. Without an observer
   * liveSeatIndex() rebuilds by scanning everything, which is strictly more
   * work than a find that can stop early — so that case keeps the scan.
   *
   * An entry is verified before it is trusted: the map unmounts circles as the
   * viewport moves, and a detached node swallows events silently.
   */
  function seatNodeFor(seatInfoId, readSeat = seatFromFiber) {
    if (!seatInfoId) return null;
    const wanted = String(seatInfoId);
    if (readSeat === seatFromFiber && seatIndex.observer && seatIndex.root) {
      const indexed = seatIndex.byId.get(wanted);
      // Verified, not merely looked up. React recycles circles as the viewport
      // moves: the observer re-files a reused node under its new seat, but the
      // entry it was filed under before still points at it. Trusting that
      // entry would fire a real pointer press at whatever seat the circle is
      // showing *now* — a click on a seat nobody asked for, which is worse
      // than a slow one. One fiber read is what makes the lookup safe, and it
      // is still O(1) against a walk of every circle in the 구역.
      if (
        indexed &&
        indexed.isConnected !== false &&
        String(seatFromFiber(indexed)?.seatInfoId) === wanted
      ) {
        return indexed;
      }
      // A miss is not proof of absence — a circle can predate the observer —
      // so fall through to the scan rather than reporting the seat unreachable.
    }
    for (const node of collectSeatCircles()) {
      const seat = readSeat(node);
      if (String(seat?.seatInfoId) === wanted) return node;
    }
    return null;
  }

  // The seat circles carry onPointerDown / onPointerUp — there is no onClick on
  // them at all (the onSeatClick above is what those two call). Dispatching a
  // MouseEvent('click') therefore did nothing: the seat took on its selected
  // colour because our API preselect had succeeded, but the page's own handler
  // never ran, so React never learned about it, 선택 좌석 stayed empty and the
  // step could not advance.
  // Why a click did or did not happen. Every failure so far looked identical
  // from outside the browser, because nothing recorded which of the several
  // early exits was taken.
  function traceClickAttempt(seatInfoId, node, outcome, extra = {}) {
    const box = node?.getBoundingClientRect?.();
    let atPoint = null;
    if (box && box.width && box.height) {
      const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      atPoint = hit === node ? "self" : hit ? `${hit.tagName}.${String(hit.getAttribute("class") || "").slice(0, 40)}` : "none";
    }
    const rendered = node ? seatRenderProps(node) : null;
    traceCall("click", seatInfoId, {
      outcome,
      found: Boolean(node),
      // What the component itself thinks, which is what its handler gates on.
      props: rendered
        ? { isDisabled: rendered.isDisabled, isSelected: rendered.isSelected, blockKey: rendered.blockKey }
        : "no-react-props",
      cls: node ? String(node.getAttribute("class") || "") : null,
      // Seats are r="1" and the map zooms, so an on-screen box can be sub-pixel.
      box: box ? { w: Number(box.width.toFixed(2)), h: Number(box.height.toFixed(2)) } : null,
      atPoint,
      ...extra,
    });
  }

  function clickSeatOnMap(seatInfoId, { countBefore = null } = {}) {
    // The focus fast path already pressed this seat a moment ago; a second
    // press would toggle it off. Report it as dispatched and move on.
    if (seatState.fastClickedId === String(seatInfoId) && nowMs() - (seatState.fastClickedAt || 0) < 1500) {
      traceClickAttempt(seatInfoId, null, "fast-path-dispatched", { before: countBefore });
      return true;
    }
    const node = seatNodeFor(seatInfoId);
    if (!node) {
      traceClickAttempt(seatInfoId, null, "no-node");
      return false;
    }
    // Belt and braces against a stale index: events dispatched into a detached
    // node go nowhere, and the run would read that as the page refusing a seat
    // that was in fact fine.
    if (node.isConnected === false) {
      traceClickAttempt(seatInfoId, node, "detached");
      return false;
    }
    // The page will not sell a seat it has drawn as disabled, and clicking one
    // raises its own 좌석 요청이 잘못되었습니다 dialog. The availability bitmap can
    // be a step ahead of, or behind, what the map is showing; when they
    // disagree the map is the one whose click we are about to fire.
    if (seatNodeDisabled(node)) {
      traceClickAttempt(seatInfoId, node, "node-disabled");
      return false;
    }
    // The cart is read by the caller, before the click, and handed in. Reading
    // it here meant a body.innerText — a full-document layout over a venue's
    // worth of circles — between deciding to click and clicking, for a number
    // that goes nowhere but the trace.
    firePointerSelect(node);
    if (seatState.markStartup) seatState.markStartup("firstClick");
    traceClickAttempt(seatInfoId, node, "dispatched", { before: countBefore });
    return true;
  }

  function seatNodeDisabled(node) {
    // Ask the component, not the stylesheet. The class test below matched
    // `SeatMap_disabled__AZO_T`, which every seat circle carries as part of its
    // base styling — so it rejected every seat, no DOM click was ever attempted,
    // and selection silently fell through to the API path on every try.
    const rendered = seatRenderProps(node);
    if (rendered) return rendered.isDisabled && !rendered.isSelected;

    if (node.getAttribute("aria-disabled") === "true") return true;
    return node.style?.pointerEvents === "none";
  }

  function firePointerSelect(node) {
    const box = node.getBoundingClientRect?.() || { left: 0, top: 0, width: 0, height: 0 };
    const shared = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: box.left + box.width / 2,
      clientY: box.top + box.height / 2,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      button: 0,
    };
    // Exactly one interaction, the same as a real click on this element.
    //
    // The seat itself carries only onPointerDown/onPointerUp, but an ancestor
    // carries onClick. Sending a synthetic click *as well* therefore delivered
    // two actions from one intended press, and on a map where selecting is a
    // toggle the second undoes or conflicts with the first.
    if (typeof PointerEvent === "function") {
      node.dispatchEvent(new PointerEvent("pointerdown", { ...shared, buttons: 1 }));
      node.dispatchEvent(new PointerEvent("pointerup", { ...shared, buttons: 0 }));
      return;
    }
    node.dispatchEvent(new MouseEvent("click", { ...shared, buttons: 0 }));
  }

  function lookupBlockKey(seat) {
    if (seat?.blockKey) return String(seat.blockKey);
    const wanted = String(seat?.seatInfoId || "");
    if (!wanted) return null;
    for (const block of seatState.lastBlocks || []) {
      if ((block.seats || []).some((item) => String(item.seatInfoId) === wanted)) {
        return block.blockKey ? String(block.blockKey) : null;
      }
    }
    return null;
  }

  async function fetchNolJson(path) {
    const response = await fetch(`${NOL_ORIGIN}${path}`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status} for ${path}`);
    return response.json();
  }

  async function fetchJson(url, options = {}) {
    const initData = getInitData();
    const headers = { ...onestopHeaders(initData), ...(options.headers || {}) };
    const response = await fetch(url, { credentials: "include", ...options, headers });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      // These four endpoints carry no GraphQL envelope, so a block here used to
      // surface as a plain HTTP error and the loop kept asking — seatStatus
      // most of all, at roughly four requests a second for as long as a watch
      // runs.
      let parsed = null;
      try {
        parsed = JSON.parse(detail);
      } catch (error) {
        /* not JSON; the status and headers can still say we are blocked */
      }
      const endpoint = String(url).split("?")[0];
      const blockedMs = readGatewayBlock(parsed, {
        status: response.status,
        headers: response.headers,
      });
      if (blockedMs >= 0) throw noteGatewayBlock(blockedMs, endpoint);
      throw new Error(`HTTP ${response.status} for ${url}${detail ? ` · ${detail.slice(0, 160)}` : ""}`);
    }
    return response.json();
  }

  function resolveBlockKeys(config, allBlocks) {
    const explicit = (config.block_keys || []).map(String).filter(Boolean);
    if (explicit.length) return explicit;
    const names = (config.block_names || []).map(String).filter(Boolean);
    if (!names.length) return [];
    const needles = names.map((name) => name.toLowerCase());
    return allBlocks
      .filter((block) => {
        const label = `${block.blockName || ""} ${block.selfDefineBlock || ""} ${block.blockKey || ""}`.toLowerCase();
        return needles.some((needle) => label.includes(needle));
      })
      .map((block) => String(block.blockKey))
      .filter(Boolean);
  }

  function padBlock(value) {
    return String(value).padStart(3, "0");
  }

  async function discoverBlockKeysViaMeta(initData, { outer = 6, inner = 10 } = {}) {
    const keys = [];
    for (let a = 1; a <= outer; a += 1) {
      for (let b = 1; b <= inner; b += 1) {
        keys.push(`${padBlock(a)}:${padBlock(b)}`);
      }
    }
    const found = [];
    for (const batch of chunk(keys, 2)) {
      if (seatState.stopRequested) break;
      try {
        const blocks = await fetchMetaBatch(initData, batch);
        for (const block of blocks || []) {
          if (block?.blockKey && (block.seats || []).length) {
            found.push({
              blockKey: String(block.blockKey),
              blockName: block.blockName || block.selfDefineBlock || String(block.blockKey),
              selfDefineBlock: block.selfDefineBlock || "",
            });
          }
        }
      } catch (error) {
        log("seatMeta scan batch failed", error);
      }
    }
    const unique = [];
    const seen = new Set();
    for (const block of found) {
      if (seen.has(block.blockKey)) continue;
      seen.add(block.blockKey);
      unique.push(block);
    }
    return unique;
  }

  // Everything keyed to one 회차, dropped when the 회차 changes.
  //
  // Block keys embed the round — the same venue is 017:001 on one round and
  // 022:001 on the next — so a block list, an availability read or an opened
  // 구역 from the previous round describes seats that no longer exist. Measured
  // live: the page drew round 022 with 40 selectable seats while the macro
  // polled round 017 and read 0 free, indistinguishable from a sold-out show.
  //
  // Shared, because the round can change in two quite different ways: during a
  // run, and while simply browsing. Only the first used to be noticed.
  function adoptBlocksKey(blocksKey) {
    if (!blocksKey || blocksKey === ":") return false;
    if (seatState.blocksKey === blocksKey) return false;
    const was = seatState.blocksKey;
    seatState.blocksKey = blocksKey;
    if (!was) return false;
    seatState.discoveredBlocks = null;
    seatState.lastBlocks = [];
    seatState.showCatalog = null;
    seatState.blockEntered = "";
    // Learned from the venue we just left, and venues differ. Keeping it made
    // the next show start by trying the wrong mapping.
    seatState.blockEntryHypothesis = "";
    // onestopHeaders()'s memoized sessionId/channel/lang lookup — see there.
    // Erring on the side of re-scanning too often rather than too rarely: a
    // round change is the established "the environment moved" signal every
    // other per-round cache here already keys off.
    seatState.headerContextCache = null;
    traceCall("roundChanged", blocksKey, { was });
    return true;
  }

  // The round on screen, read from a handful of seats rather than all of them.
  //
  // This runs on every host snapshot, and a venue can have tens of thousands of
  // circles; walking them four times a second to answer a question a dozen
  // seats already answer would make the poll the most expensive thing the page
  // does. Sampling is enough because every seat in a drawn 구역 shares a round.
  function sampledRoundKey(limit = 12) {
    // Off the live DOM, not off the seat index. The index is kept by a
    // MutationObserver, and this is the one thing that notices a 일정 change —
    // reading a stale index would report the old round, which is precisely the
    // failure this exists to catch (the page drawing 022 while the macro polled
    // 017 and read 0 free, forever).
    //
    // But it runs on every host snapshot, four times a second, on the same
    // thread as the catch loop. It needs a dozen seats, not the venue, so try
    // the specific selector first and only fall back to the full four-pass
    // scan if the page does not use it.
    let nodes = [...document.querySelectorAll("circle.js-seat")];
    if (nodes.length < 3) nodes = collectSeatCircles();
    if (nodes.length < 3) return null;
    // Once, outside the loop, and never allowed to throw: this runs on every
    // host snapshot, and getInitData reaches into page storage that is not
    // always there.
    let goods = "";
    try {
      goods = String(getInitData()?.goods?.goodsCode || "");
    } catch (error) {
      return null;
    }
    if (!goods) return null;
    for (let index = 0; index < nodes.length && index < limit; index += 1) {
      const key = seatFromFiber(nodes[index])?.blockKey;
      if (!key) continue;
      const seq = String(key).split(":")[0];
      if (seq) return `${goods}:${seq}`;
    }
    return null;
  }

  // Which 구역 to stand in, given we can only stand in one.
  //
  // A seat that is not drawn cannot be clicked, and the map only mounts the
  // block in the viewport — so on a big venue the watch is genuinely fast in
  // exactly one block and pays leaveBlock + enterBlock + fitBlock (~640ms,
  // measured) for a seat that frees anywhere else. Which block that is
  // therefore matters, and it used to be whichever came first out of
  // block-data: `discoveredBlocks.find(...)`. Standing in E7 with E7 watched,
  // the run would leave it and travel to E1 because E1 sorted earlier — paying
  // the full cost to end up somewhere no better.
  function blockToStandIn(watchedKeys, openNow) {
    const watched = new Set((watchedKeys || []).map(String));
    const blocks = (seatState.discoveredBlocks || []).filter((block) =>
      watched.has(String(block.blockKey)),
    );
    if (!blocks.length) return null;

    // Already somewhere we are watching: stay. Travel costs ~640ms and buys
    // nothing, and the user may have navigated here deliberately.
    if (openNow && watched.has(String(openNow))) {
      return (
        blocks.find((block) => String(block.blockKey) === String(openNow)) || {
          blockKey: String(openNow),
        }
      );
    }

    // Otherwise the block with the most seats in it. We can only be fast in
    // one, so be fast in the one most likely to produce a cancellation.
    const sizes = new Map(
      (seatState.lastBlocks || []).map((block) => [
        String(block.blockKey),
        (block.seats || []).filter(seatSellable).length,
      ]),
    );
    let best = blocks[0];
    let bestSize = sizes.get(String(best.blockKey)) || 0;
    for (const block of blocks.slice(1)) {
      const size = sizes.get(String(block.blockKey)) || 0;
      if (size > bestSize) {
        best = block;
        bestSize = size;
      }
    }
    return best;
  }

  async function fetchBlockKeys(rawInitData) {
    const initData = withLivePlaySeq(rawInitData);
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const seq = String(playSeq?.playSeq || playSeq || "");

    // Blocks belong to one 회차, not to the show.
    //
    // Block keys embed the round — the same venue is 017:001 on one round and
    // 022:001 on the next — and this cache was keyed on nothing at all, so the
    // first round's blocks were reused for every later one. Measured live: the
    // page was drawing round 022 with 40 selectable seats while the macro
    // polled availability for round 017 and read 0 free, forever. It looked
    // exactly like a sold-out show, and it is why nothing was ever caught.
    adoptBlocksKey(`${goods?.goodsCode || ""}:${seq}`);
    if (seatState.discoveredBlocks?.length) return seatState.discoveredBlocks;
    // Official client sends only goodsCode, placeCode, playSeq. Extra playDate
    // is rejected with HTTP 400.
    const query = { goodsCode: goods.goodsCode, placeCode: goods.placeCode, playSeq: seq };
    try {
      const payload = await fetchJson(`/onestop/api/seats/block-data?${new URLSearchParams(query)}`);
      const blocks = payload?.blocks || payload?.data?.blocks || payload;
      if (Array.isArray(blocks) && blocks.length) {
        const mapped = blocks.map((block) => ({
          blockKey: String(block?.blockKey || block?.key || block),
          blockName: block?.blockName || block?.name || block?.selfDefineBlock || "",
          selfDefineBlock: block?.selfDefineBlock || block?.blockName || "",
          // Venue-image coords — used to aim zoom when no seat circles are drawn.
          absoluteLeft: numOrNull(block?.absoluteLeft),
          absoluteTop: numOrNull(block?.absoluteTop),
          absoluteRight: numOrNull(block?.absoluteRight),
          absoluteBottom: numOrNull(block?.absoluteBottom),
        })).filter((block) => block.blockKey && block.blockKey !== "undefined");
        if (mapped.length) {
          seatState.discoveredBlocks = mapped;
          attachCatalogBlocks(mapped);
          return mapped;
        }
      }
    } catch (error) {
      seatState.lastError = String(error);
      log("block-data failed", error);
    }
    updateOverlay("구역 목록 API 실패 — seatMeta로 구역을 찾습니다", "warn");
    const scanned = await discoverBlockKeysViaMeta(initData);
    seatState.discoveredBlocks = scanned;
    attachCatalogBlocks(scanned);
    return scanned;
  }

  function catalogBlockRow(block) {
    return {
      block_key: block.blockKey,
      block_name: block.blockName || block.selfDefineBlock || block.blockKey,
      label: block.selfDefineBlock || block.blockName || block.blockKey,
      left: block.absoluteLeft,
      top: block.absoluteTop,
      right: block.absoluteRight,
      bottom: block.absoluteBottom,
    };
  }

  // The parked sketch is keyed to the show it was built from.
  //
  // It used to be a bare point list on window, and `parkSketch` only ever
  // overwrote it when handed a non-empty array — so it never cleared. Opening a
  // second show found an empty catalog, had the *previous* show's sketch
  // injected into it, and `enrichCatalogSketch` then saw a non-empty sketch and
  // returned without ever fetching the real seats. The picker drew the wrong
  // venue, with a stage inferred from the wrong seats.
  function currentSketchKey() {
    // Read the identity straight from the page payload first: getInitData also
    // walks the Interpark context, and anything that throws in there would come
    // back as "no key", which silently re-enables the cross-show reuse this
    // key exists to prevent.
    // The round belongs in the key, not just the show. Block keys embed the
    // playSeq — the same venue is 002:101 on one round and 001:101 on the next
    // — so a sketch reused across rounds carries keys that match nothing the
    // current round returns.
    const withSeq = (data) => {
      const code = data?.goods?.goodsCode;
      if (!code) return null;
      const seq = String(data?.playSeq?.playSeq || data?.playSeq || "");
      return seq ? `${code}:${seq}` : String(code);
    };
    // Strict goods match: the stored booking session is the *previous* show on
    // a fresh product/goods page, and keying the sketch off it is what drew
    // another venue's seats under a new show's name. A key whose goods is not
    // the show this URL names is no key at all.
    try {
      const direct = withSeq(window.__NEXT_DATA__?.props?.pageProps?.initData);
      if (direct) return sketchKeyFits(direct) ? direct : null;
    } catch (error) {
      /* fall through */
    }
    try {
      const key = withSeq(getInitData());
      return sketchKeyFits(key) ? key : null;
    } catch (error) {
      return null;
    }
  }
  // Does this sketch key belong to the show the page names? Pages that name no
  // show (queue, home) cannot contradict it, so they accept any key.
  function sketchKeyFits(key) {
    if (!key) return false;
    const m = location.pathname.match(/\/(?:goods|ticket\/products)\/([A-Z0-9]+)/i);
    if (!m) return true;
    return String(key).split(":")[0].toUpperCase() === m[1].toUpperCase();
  }

  function parkSketch(points, key = currentSketchKey()) {
    if (Array.isArray(points) && points.length) {
      parkedSketch.points = points;
      parkedSketch.key = key || null;
    }
    return parkedSketch.points || [];
  }

  function parkedSketchFor(key) {
    if (!parkedSketch.points?.length) return [];
    // No key on either side means we cannot prove it belongs here; only reuse
    // it when both are known and equal.
    if (!key || !parkedSketch.key || parkedSketch.key !== key) return [];
    return parkedSketch.points;
  }

  function restoreParkedSketch(catalog) {
    const key = currentSketchKey();
    if (catalog?.sketch?.length) {
      parkSketch(catalog.sketch, key);
      return catalog;
    }
    const parked = parkedSketchFor(key);
    if (!parked.length) return catalog;
    if (!catalog) return { fetched_at: Date.now(), sketch: parked, blocks: [], grades: [], errors: [] };
    catalog.sketch = parked;
    return catalog;
  }

  // Venues carry administrative blocks parked outside the seating: a 차액
  // surcharge block on one show, a floorless no-grade block on another. On
  // 26012217 block 002:308 sat at x 223..250 with no takeable seat at all while
  // every real seat was inside x 33..203, so drawing it hung a cluster beside
  // the venue and stretched the frame around empty space.
  //
  // The test is the whole block, never the individual seat. NOL draws sold and
  // unavailable seats — they are the grey dots — and dropping them one by one
  // punched holes through the middle of the house and reported a seat count far
  // below the real one. A block with no takeable seat anywhere is furniture; a
  // block with even one is a real part of the room and is drawn entire.
  function blockBox(block) {
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    for (const seat of block?.seats || []) {
      const x = numOrNull(seat.posLeft);
      const y = numOrNull(seat.posTop);
      if (x == null || y == null) continue;
      if (x < left) left = x;
      if (x > right) right = x;
      if (y < top) top = y;
      if (y > bottom) bottom = y;
    }
    return left === Infinity ? null : { left, top, right, bottom };
  }

  // Which blocks belong to the room.
  //
  // Having no takeable seat is not enough on its own to call a block furniture.
  // 26012217 has three such blocks: 306 and 307 are the small L/R groups NOL
  // draws at the sides of the 3rd floor, while 308 is 100 seats with no floor,
  // no row label and no grade, parked at x 223..250 when every real seat is
  // inside x 33..203. Dropping all three lost two groups the venue shows; the
  // one that has to go is the one sitting outside the room.
  function seatingBlocks(blocks) {
    const list = (blocks || []).filter((block) => (block?.seats || []).length);
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    for (const block of list) {
      for (const seat of block.seats) {
        if (seat.isExposable === false) continue;
        const x = numOrNull(seat.posLeft);
        const y = numOrNull(seat.posTop);
        if (x == null || y == null) continue;
        if (x < left) left = x;
        if (x > right) right = x;
        if (y < top) top = y;
        if (y > bottom) bottom = y;
      }
    }
    // Nothing takeable anywhere (a sold-out round): draw the venue as it is
    // rather than erase it.
    if (left === Infinity) return list;
    return list.filter((block) => {
      if (block.seats.some((seat) => seat.isExposable !== false)) return true;
      const box = blockBox(block);
      if (!box) return false;
      return box.left <= right && box.right >= left && box.top <= bottom && box.bottom >= top;
    });
  }

  function sketchFromSeatBlocks(blocks) {
    const points = [];
    for (const block of seatingBlocks(blocks)) {
      const key = String(block?.blockKey || "");
      if (!key) continue;
      for (const seat of block.seats || []) {
        const x = numOrNull(seat.posLeft);
        const y = numOrNull(seat.posTop);
        if (x == null || y == null) continue;
        points.push({ k: key, x, y });
      }
    }
    if (points.length <= ZONE_SKETCH_MAX) return points;

    return downsampleSketch(points);
  }

  function attachCatalogBlocks(blocks) {
    const rows = (blocks || []).map(catalogBlockRow).filter((row) => row.block_key);
    if (!rows.length) return seatState.showCatalog;
    const prev = restoreParkedSketch(seatState.showCatalog) || {
      fetched_at: Date.now(),
      grades: [],
      blocks: [],
      sketch: parkSketch([]),
      schedules: [],
      remain_by_grade: {},
      errors: [],
    };
    const sketch = prev.sketch?.length ? prev.sketch : parkSketch(prev.sketch);
    const sameCount = Array.isArray(prev.blocks) && prev.blocks.length === rows.length;
    const sameKeys =
      sameCount && rows.every((row, i) => row.block_key === (prev.blocks[i] && prev.blocks[i].block_key));
    if (sameKeys && sketch.length) {
      seatState.showCatalog = { ...prev, blocks: rows, sketch };
      return seatState.showCatalog;
    }
    seatState.showCatalog = {
      ...prev,
      blocks: rows,
      sketch,
      fetched_at: Date.now(),
    };
    return seatState.showCatalog;
  }

  // Official map is grape dots, not block AABBs. The panel redraws this sketch.
  const ZONE_SKETCH_MAX = 6000;

  function downsampleSketch(points) {
    if (points.length <= ZONE_SKETCH_MAX) return points;
    // Sample by position, not by array index.
    //
    // Seats arrive ordered block-by-block then row-by-row, so keeping every Nth
    // one thinned each block along its own rows, which reads as stripes and
    // under-draws narrow blocks worst — on 26011315 the thinnest block kept
    // 10.9% of its seats. Laying a grid over the venue and keeping one seat per
    // cell samples evenly in space instead: same venue, 17/17 blocks, and the
    // thinnest block up to 17.6%.
    //
    // min/max by loop, not Math.min(...xs): the spread passes one argument per
    // seat, and a big enough venue would overflow the call stack.
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    for (const point of points) {
      if (point.x < left) left = point.x;
      if (point.x > right) right = point.x;
      if (point.y < top) top = point.y;
      if (point.y > bottom) bottom = point.y;
    }
    const width = right - left || 1;
    const height = bottom - top || 1;
    const cells = Math.max(1, Math.floor(Math.sqrt(ZONE_SKETCH_MAX * (width / height || 1))));
    const cellW = width / cells;
    const cellH = height / Math.max(1, Math.floor(ZONE_SKETCH_MAX / cells));

    const seen = new Set();
    const kept = [];
    for (const point of points) {
      const cell = `${point.k}:${Math.floor((point.x - left) / (cellW || 1))}:${Math.floor(
        (point.y - top) / (cellH || 1),
      )}`;
      if (seen.has(cell)) continue;
      seen.add(cell);
      kept.push(point);
    }
    return kept;
  }

  async function buildZoneSketch(initData, blocks) {
    const keys = (blocks || []).map((block) => block.blockKey).filter(Boolean);
    if (!keys.length) return [];
    const metaBlocks = (
      await mapLimit(chunk(keys, 2), Math.min(4, SEAT_META_CONCURRENCY || 4), (batch) =>
        fetchMetaBatch(initData, batch),
      )
    ).flat();
    const points = [];
    // Every block with seats, not just the sellable ones.
    //
    // seatingBlocks() drops a block that has no exposable seat and lies clear
    // of the exposable bounds. For the *watch* that is right — nothing can free
    // up in a block nobody can buy from. For the *picker* it silently deletes
    // part of the room: 26012673 sells 1F/2F A-C only, so D, E and one side
    // block — 712 real seats, five of eleven — vanished, and the drawn map had
    // three columns where the 예매 창 beside it showed five. A map that is not a
    // map of the room cannot be aimed with.
    //
    // So: draw everything, mark what cannot sell, and let the watch filter.
    for (const block of metaBlocks || []) {
      const key = String(block?.blockKey || "");
      if (!key || !(block.seats || []).length) continue;
      for (const seat of block.seats || []) {
        const x = numOrNull(seat.posLeft);
        const y = numOrNull(seat.posTop);
        // Include sold seats too — the grey dots are what make the house shape.
        if (x == null || y == null) continue;
        // `s` is written only for the minority that cannot sell; absent means
        // sellable. The sketch rides in a state file that is already ~300KB and
        // is parsed on every poll.
        const point = { k: key, x, y };
        if (seat.isExposable === false) point.s = 0;
        points.push(point);
      }
    }
    return downsampleSketch(points);
  }

  // What the official map exposes for moving the viewport.
  //
  // Seats only exist in the DOM while inside the viewport, and selectSeats is
  // map-click only, so an off-screen seat cannot be taken at all. The previous
  // zoom code was deleted and this file is untracked, so there is no source to
  // recover — this reports the real API instead of guessing at one.
  function probeMapTransform() {
    const out = { roots: [], found: null };
    const root = seatMapRoot();
    out.container = root
      ? (({ x, y, width, height }) => ({ x, y, width, height }))(root.getBoundingClientRect())
      : null;
    const svg = document.querySelector('[class*="eatMap"] svg, svg[viewBox]');
    out.viewBox = svg?.getAttribute?.("viewBox") || null;

    const named = (obj) => {
      const names = new Set();
      for (const key of Object.keys(obj || {})) names.add(key);
      const proto = Object.getPrototypeOf(obj || {});
      for (const key of Object.getOwnPropertyNames(proto || {})) names.add(key);
      return [...names].slice(0, 40);
    };
    const interesting = /setTransform|zoomToElement|zoomToClickPosition|centerView|zoomIn|zoomOut|resetTransform|instance|transformState/;

    const roots = [root, ...document.querySelectorAll('[class*="eatMap"],[class*="placeImg"]')]
      .filter(Boolean)
      .slice(0, 6);
    for (const el of roots) {
      const fiberKey = Object.keys(el).find(
        (key) => key.startsWith("__reactFiber") || key.startsWith("__reactInternalInstance"),
      );
      if (!fiberKey) continue;
      let fiber = el[fiberKey];
      for (let depth = 0; depth < 60 && fiber; depth += 1) {
        const props = fiber.memoizedProps || fiber.pendingProps || {};
        for (const [label, cand] of [
          ["props.instance", props.instance],
          ["props.zoomInstance", props.zoomInstance],
          ["stateNode", fiber.stateNode],
          ["memoizedState", fiber.memoizedState?.memoizedState],
        ]) {
          if (!cand || typeof cand !== "object") continue;
          const keys = named(cand);
          if (!keys.some((key) => interesting.test(key))) continue;
          const entry = {
            where: label,
            depth,
            keys,
            transformState: cand.transformState
              ? {
                  scale: cand.transformState.scale,
                  positionX: cand.transformState.positionX,
                  positionY: cand.transformState.positionY,
                }
              : null,
            setup: cand.setup
              ? { minScale: cand.setup.minScale, maxScale: cand.setup.maxScale }
              : null,
          };
          out.roots.push(entry);
          if (!out.found) out.found = entry;
        }
        fiber = fiber.return;
      }
      if (out.found) break;
    }
    out.rootCount = out.roots.length;
    return out;
  }

  // How this page represents a block, so one can be entered.
  //
  // A stadium does not draw seats at all until a 구역 is chosen: 26011315 has
  // 43 blocks and 28,932 seats in seatMeta and exactly 1 seat circle in the
  // DOM. Panning cannot help — there is nothing drawn to pan toward — so the
  // macro has to click into a block first. This reports what there is to click.
  // What the block-selection view is made of, and how its coordinates land on
  // screen. Kept synchronous: fetching the venue drawing to read its viewBox
  // hung the whole audit, and none of this needs the network.
  //
  // Established so far on 26011315 (Maroon 5, 43 blocks, 28,932 seats):
  //   * the venue is an <img> bitmap with a hit-test <svg> over it — there are
  //     no seat circles and no block elements to query for
  //   * the overlay maps viewBox -> screen by "meet": scale = rect.h/vb.h and
  //     a centring x offset. That reproduces the venue image's measured
  //     position exactly, so it is arithmetic rather than a guess
  //   * block-data's absolute boxes reach x=1475 while the overlay viewBox is
  //     only 1214 wide, so blocks are NOT in overlay space
  //   * seat posLeft/posTop does look like block absolute space: block 001:001
  //     is [393,80,519,161] and its seats start at (395.9, 83)
  //
  // The last point is what makes this solvable without guessing: once any block
  // is open, rendered seats give absolute -> screen directly, and every other
  // block's position follows.
  function probeBlockElements() {
    const out = {};
    const svg = document.querySelector("svg");
    if (svg) {
      const box = svg.getBoundingClientRect();
      const vb = String(svg.getAttribute("viewBox") || "").split(/\s+/).map(Number);
      out.overlay = {
        viewBox: svg.getAttribute("viewBox"),
        rect: { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height) },
      };
      if (vb.length === 4 && vb[2] && vb[3]) {
        // preserveAspectRatio defaults to xMidYMid meet.
        const scale = Math.min(box.width / vb[2], box.height / vb[3]);
        out.overlay.fit = {
          scale,
          offsetX: box.x + (box.width - vb[2] * scale) / 2,
          offsetY: box.y + (box.height - vb[3] * scale) / 2,
        };
      }
    }

    const blocks = seatState.discoveredBlocks || [];
    const boxed = blocks.filter((b) => b.absoluteLeft != null);
    if (boxed.length) {
      out.absoluteExtent = {
        left: Math.min(...boxed.map((b) => b.absoluteLeft)),
        top: Math.min(...boxed.map((b) => b.absoluteTop)),
        right: Math.max(...boxed.map((b) => b.absoluteRight)),
        bottom: Math.max(...boxed.map((b) => b.absoluteBottom)),
      };
    }

    // The calibration that settles it, available only while seats are drawn.
    const calibration = calibrateVenueToScreen();
    out.seatCalibration = calibration
      ? { scale: calibration.scale, samples: calibration.samples }
      : null;
    if (calibration && boxed.length) {
      // Where this mapping says each block is. If it is the right space, these
      // land on the venue image; if not, they will be obviously outside it.
      out.blockScreenGuess = boxed.slice(0, 6).map((b) => {
        const mid = calibration.toScreen(
          (b.absoluteLeft + b.absoluteRight) / 2,
          (b.absoluteTop + b.absoluteBottom) / 2,
        );
        return { key: b.blockKey, x: Math.round(mid.x), y: Math.round(mid.y) };
      });
    }
    return out;
  }

  // Report what the venue really contains, ignoring every cache.
  //
  // The picker drew 273 seats across 2 blocks on a show whose own map clearly
  // holds more, and the seat feed is the only place that can say whether the
  // block list is short or the per-block seat lists are. Results go to the
  // trace so they can be read out of the state file.
  async function auditBlocks() {
    // Same correction the product path uses, or the diagnosis reports the very
    // stale round it is meant to expose.
    const initData = withLivePlaySeq(getInitData());
    if (!initData?.goods) return { error: "no initData on this page" };
    const goods = initData.goods;
    const seq = String(initData.playSeq?.playSeq || initData.playSeq || "");
    const query = { goodsCode: goods.goodsCode, placeCode: goods.placeCode, playSeq: seq };

    const report = {
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: seq,
      cachedBlocks: (seatState.discoveredBlocks || []).length,
      cachedSketch: (seatState.showCatalog?.sketch || []).length,
    };

    // 1. The block list, fetched fresh.
    try {
      const payload = await fetchJson(`/onestop/api/seats/block-data?${new URLSearchParams(query)}`);
      const blocks = payload?.blocks || payload?.data?.blocks || payload;
      report.blockDataType = Array.isArray(blocks) ? "array" : typeof blocks;
      report.blockDataCount = Array.isArray(blocks) ? blocks.length : null;
      report.blockKeys = Array.isArray(blocks)
        ? blocks.map((b) => String(b?.blockKey || b?.key || b)).slice(0, 60)
        : null;
      // Keep one raw entry so unexpected shapes are visible rather than guessed.
      report.blockSample = Array.isArray(blocks) ? blocks[0] : payload;
      report.payloadKeys = payload && typeof payload === "object" ? Object.keys(payload).slice(0, 20) : null;
    } catch (error) {
      report.blockDataError = String(error).slice(0, 200);
    }

    // 2. Seat counts per block, straight from seatMeta.
    const keys = report.blockKeys || (seatState.discoveredBlocks || []).map((b) => b.blockKey);
    report.metaCounts = {};
    report.metaErrors = {};
    for (const batch of chunk(keys, 2)) {
      try {
        const metaBlocks = await fetchMetaBatch(initData, batch);
        for (const block of metaBlocks || []) {
          const key = String(block?.blockKey || "");
          report.metaCounts[key] = (block.seats || []).length;
        }
        for (const key of batch) {
          if (!(key in report.metaCounts)) report.metaErrors[key] = "no block returned";
        }
      } catch (error) {
        for (const key of batch) report.metaErrors[key] = String(error).slice(0, 160);
      }
    }
    report.metaTotal = Object.values(report.metaCounts).reduce((a, b) => a + b, 0);

    // Are posLeft/posTop venue-global, or local to each block? If they are
    // local, drawing every block in one space stacks them on top of each other
    // and the copy of the map is scrambled however complete the data is.
    // Comparing each block's seat extents against its absolute* box settles it.
    report.geometry = {};
    for (const batch of chunk(keys, 2)) {
      let metaBlocks = [];
      try {
        metaBlocks = await fetchMetaBatch(initData, batch);
      } catch (error) {
        continue;
      }
      for (const block of metaBlocks || []) {
        const key = String(block?.blockKey || "");
        const seats = (block.seats || []).filter(
          (seat) => numOrNull(seat.posLeft) !== null && numOrNull(seat.posTop) !== null,
        );
        if (!seats.length) continue;
        const xs = seats.map((seat) => Number(seat.posLeft));
        const ys = seats.map((seat) => Number(seat.posTop));
        const known = (seatState.discoveredBlocks || []).find(
          (candidate) => String(candidate.blockKey) === key,
        );
        report.geometry[key] = {
          posLeft: [Math.min(...xs), Math.max(...xs)],
          posTop: [Math.min(...ys), Math.max(...ys)],
          absolute: known
            ? [known.absoluteLeft, known.absoluteTop, known.absoluteRight, known.absoluteBottom]
            : null,
          rows: [...new Set(seats.map((seat) => seat.rowNo))].slice(0, 16),
          grades: [...new Set(seats.map((seat) => seat.seatGradeName))].slice(0, 8),
          gradeCodes: [...new Set(seats.map((seat) => String(seat.seatGrade)))].slice(0, 8),
          floors: [...new Set(seats.map((seat) => seat.floor))].slice(0, 8),
          exposable: seats.filter((seat) => seat.isExposable !== false).length,
          sample: {
            seatInfoId: seats[0].seatInfoId,
            rowNo: seats[0].rowNo,
            seatNo: seats[0].seatNo,
            salesPrice: seats[0].salesPrice,
            isExposable: seats[0].isExposable,
          },
        };
      }
    }

    // 3. What the page itself has drawn, as an independent check.
    try {
      report.domSeatCircles = collectSeatCircles().length;
    } catch (error) {
      report.domSeatCircles = null;
    }

    // 4. How to move the viewport, which decides whether an off-screen seat is
    //    reachable at all on a big venue.
    try {
      report.transform = probeMapTransform();
    } catch (error) {
      report.transform = { error: String(error).slice(0, 200) };
    }
    try {
      report.blockElements = probeBlockElements();
      report.blockingOverlay = describeBlockingOverlay();
    } catch (error) {
      report.blockElements = { error: String(error).slice(0, 200) };
    }

    // 5. The decisive experiment, and the only way to settle which coordinate
    //    space the blocks are in: try to open one and see whether seats appear.
    //
    //    Safe to run from a diagnosis — opening a 구역 is navigation, it holds
    //    no seat and books nothing. Only attempted when the map is drawing
    //    nothing, which is precisely the broken state being investigated.
    try {
      // If a block is already open, prove the round trip: out to the venue and
      // back into a *different* block. That is the move a real catch needs when
      // the freed seat is not in the block that happens to be open.
      if (collectSeatCircles().length >= 3 && (seatState.discoveredBlocks || []).length) {
        const wasIn = seatState.blockEntered || "(unknown)";
        const fitted = await fitBlockToView();
        const left = await leaveBlockToVenue();
        report.blockRoundTrip = {
          startedInside: wasIn,
          seatsBefore: fitted.seats ?? null,
          fit: fitted,
          left,
        };
        if (left.ok) {
          const other = (seatState.discoveredBlocks || []).filter(
            (block) => block.absoluteLeft != null && String(block.blockKey) !== String(wasIn),
          )[0];
          if (other) {
            report.blockRoundTrip.reEnter = {
              blockKey: other.blockKey,
              result: await enterBlockForSeats(other),
              seatsAfter: collectSeatCircles().length,
            };
          }
        }
      } else if (collectSeatCircles().length < 3 && (seatState.discoveredBlocks || []).length) {
        const biggest = [...seatState.discoveredBlocks]
          .filter((block) => block.absoluteLeft != null)
          .sort(
            (a, b) =>
              (b.absoluteRight - b.absoluteLeft) * (b.absoluteBottom - b.absoluteTop) -
              (a.absoluteRight - a.absoluteLeft) * (a.absoluteBottom - a.absoluteTop),
          )[0];
        if (biggest) {
          const points = {};
          for (const hypothesis of BLOCK_ENTRY_HYPOTHESES) {
            const point = blockClickPoint(biggest, hypothesis);
            // What is actually under the point. Dispatching into an unknown
            // element is how a click can look correct and do nothing.
            points[hypothesis] = point
              ? { x: Math.round(point.clientX), y: Math.round(point.clientY), ...describePoint(point) }
              : null;
          }
          report.blockEntry = {
            tried: biggest.blockKey,
            name: biggest.selfDefineBlock || biggest.blockName,
            box: [biggest.absoluteLeft, biggest.absoluteTop, biggest.absoluteRight, biggest.absoluteBottom],
            points,
            seatsBefore: collectSeatCircles().length,
            result: await enterBlockForSeats(biggest),
            seatsAfter: collectSeatCircles().length,
          };
        }
      }
    } catch (error) {
      report.blockEntry = { error: String(error).slice(0, 200) };
    }

    // 6. Open a block and count what the page actually draws, against what the
    //    availability bitmap claims for those same blocks. The bitmap is the
    //    only reason the macro believes a show is sold out; if the page draws
    //    selectable seats while the bitmap reports none, the bitmap is the bug
    //    and everything downstream of it is chasing a phantom.
    try {
      const target = (seatState.discoveredBlocks || []).filter((b) => b.absoluteLeft != null)[0];
      if (target) {
        if (collectSeatCircles().length < 3) {
          report.groundTruthEntry = await enterBlockForSeats(target);
          await fitBlockToView();
        }
        const nodes = collectSeatCircles();
        let free = 0;
        let disabled = 0;
        let noSeat = 0;
        const byBlock = {};
        const samples = [];
        for (const node of nodes) {
          const seat = seatFromFiber(node);
          if (!seat?.seatInfoId) {
            noSeat += 1;
            continue;
          }
          const key = String(seat.blockKey || "?");
          byBlock[key] = (byBlock[key] || 0) + 1;
          const off = seatNodeDisabled(node);
          if (off) disabled += 1;
          else free += 1;
          // Raw render props for the seats we believe are free. If the page
          // simply omits isDisabled, our "free" count is an artefact of reading
          // undefined as false — not availability.
          if (!off && samples.length < 6) {
            const rp = seatRenderProps(node) || {};
            samples.push({
              id: seat.seatInfoId,
              row: seat.rowNo,
              no: seat.seatNo,
              grade: seat.seatGradeName,
              blockKeyOnProps: rp.blockKey === undefined ? "(undefined)" : String(rp.blockKey),
              isDisabled: rp.isDisabled === undefined ? "(undefined)" : rp.isDisabled,
              isSelected: rp.isSelected === undefined ? "(undefined)" : rp.isSelected,
              propKeys: Object.keys(rp).slice(0, 12),
            });
          }
        }
        const bitmap = {};
        for (const block of seatState.lastBlocks || []) {
          const key = String(block.blockKey);
          if (!(key in byBlock)) continue;
          bitmap[key] = (block.mask || []).filter(Boolean).length;
        }
        const initSeq = String(initData?.playSeq?.playSeq || initData?.playSeq || "");
        report.groundTruth = {
          initDataPlaySeq: initSeq,
          discoveredBlockKeys: (seatState.discoveredBlocks || []).map((b) => b.blockKey).slice(0, 4),
          circles: nodes.length,
          domFree: free,
          domDisabled: disabled,
          withoutSeatProps: noSeat,
          perBlockDrawn: byBlock,
          bitmapFreeForThoseBlocks: bitmap,
          bitmapFreeTotal: freeSeatCount(),
          samples,
        };
      }
    } catch (error) {
      report.groundTruth = { error: String(error).slice(0, 200) };
    }

    // 7. The bitmap against the page, seat by seat.
    //
    //    The page draws 40 selectable seats while the availability bitmap
    //    reports 0 free for the same venue. One of them is wrong, and the
    //    bitmap is what 취켓팅 acts on — so if it is the liar, the watch can
    //    never fire no matter how well everything downstream works.
    try {
      const keys = (seatState.discoveredBlocks || []).map((b) => b.blockKey).filter(Boolean);
      if (keys.length) {
        const payload = await fetchSeatStatus(initData, keys.slice(0, 2));
        const parsed = parseSeatStatus(payload);
        const meta = await fetchMetaBatch(initData, keys.slice(0, 2));
        const rendered = new Map();
        for (const node of collectSeatCircles()) {
          const seat = seatFromFiber(node);
          if (seat?.seatInfoId) rendered.set(String(seat.seatInfoId), seatNodeDisabled(node));
        }

        const out = { blocks: [], rawSample: null };
        for (const block of parsed || []) {
          const metaBlock = (meta || []).find(
            (m) => String(m.blockKey) === String(block.blockKey),
          );
          const seats = metaBlock?.seats || [];
          const mask = block.mask || [];
          let maskFree = 0;
          let domFree = 0;
          let agree = 0;
          let maskSaysTakenDomSaysFree = 0;
          const disagreements = [];
          for (let i = 0; i < seats.length; i += 1) {
            const id = String(seats[i]?.seatInfoId || "");
            const free = Boolean(mask[i]);
            if (free) maskFree += 1;
            if (!rendered.has(id)) continue;
            const domIsFree = rendered.get(id) === false;
            if (domIsFree) domFree += 1;
            if (domIsFree === free) agree += 1;
            else if (domIsFree && !free) {
              maskSaysTakenDomSaysFree += 1;
              if (disagreements.length < 3) {
                disagreements.push({ i, id, row: seats[i]?.rowNo, no: seats[i]?.seatNo });
              }
            }
          }
          out.blocks.push({
            blockKey: block.blockKey,
            metaSeats: seats.length,
            maskLength: mask.length,
            maskFree,
            renderedOfThisBlock: seats.filter((x) => rendered.has(String(x?.seatInfoId))).length,
            domFree,
            agree,
            maskSaysTakenDomSaysFree,
            disagreements,
          });
        }
        // The raw string, so a decode error is visible rather than inferred.
        const raw = Array.isArray(payload) ? payload[0] : payload?.data?.[0];
        out.rawSample = raw
          ? { keys: Object.keys(raw).slice(0, 8), sample: JSON.stringify(raw).slice(0, 220) }
          : { payloadType: typeof payload, sample: JSON.stringify(payload).slice(0, 220) };
        report.bitmapVsDom = out;
      }
    } catch (error) {
      report.bitmapVsDom = { error: String(error).slice(0, 200) };
    }
    return report;
  }

  async function enrichCatalogSketch(initData, blocks) {
    attachCatalogBlocks(blocks);
    // Only skip the fetch when the sketch we already hold belongs to this show.
    const key = currentSketchKey();
    if (seatState.showCatalog?.sketch?.length && parkedSketchFor(key).length) {
      return seatState.showCatalog;
    }
    try {
      const sketch = await buildZoneSketch(initData, blocks);
      if (!sketch.length) return seatState.showCatalog;
      parkSketch(sketch, key);
      seatState.showCatalog = {
        ...(seatState.showCatalog || {}),
        sketch,
        fetched_at: Date.now(),
      };
    } catch (error) {
      log("zone sketch failed", error);
    }
    return seatState.showCatalog;
  }

  function chunk(items, size) {
    const chunks = [];
    for (let index = 0; index < items.length; index += size) chunks.push(items.slice(index, index + size));
    return chunks;
  }

  // Stadium-sized venues have hundreds of blocks. Firing every seatMeta request
  // at once gets the session throttled, so requests run with a fixed number of
  // workers and `shouldStop` lets a caller bail as soon as it has enough seats.
  // Whether the last mapLimit returned everything it was asked for. A batch
  // that throws is dropped from the results and the caller cannot tell the
  // difference between "the venue has six blocks" and "five requests failed" —
  // which is exactly how a picker comes to draw half a house and a watch comes
  // to sweep half a venue, both in silence.
  let lastMapComplete = true;

  async function mapLimit(items, limit, worker, shouldStop = () => false) {
    const results = [];
    let cursor = 0;
    let failed = 0;
    let stopped = false;
    const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (cursor < items.length) {
        if (shouldStop(results)) {
          stopped = true;
          return;
        }
        const index = cursor;
        cursor += 1;
        try {
          results.push(await worker(items[index], index));
        } catch (error) {
          failed += 1;
          log("mapLimit item failed", error);
        }
      }
    });
    await Promise.all(runners);
    lastMapComplete = !failed && !stopped;
    if (failed) {
      seatState.batchFailures = (seatState.batchFailures || 0) + failed;
      traceCall("batchDropped", null, { failed, of: items.length });
    }
    return results;
  }

  // Shared by fetchMetaBatch and fetchSeatStatus, which built this identically
  // twice. Not hoisted out of the call — goodsCode/placeCode/playSeq can
  // genuinely change mid-run (a round switch), and blockKeys is by definition
  // a different batch on every call, so there is nothing here that is safe to
  // memoize across calls the way headerContextCache is above. This only
  // removes the duplication; the object itself still has to be built fresh
  // every time it's asked for a different batch.
  function seatQueryParams(initData, blockKeys) {
    const params = new URLSearchParams({
      goodsCode: initData.goods.goodsCode,
      placeCode: initData.goods.placeCode,
      playSeq: initData.playSeq.playSeq,
      bizCode: initData.bizCode || "WEBBR",
    });
    for (const blockKey of blockKeys) params.append("blockKeys", blockKey);
    return params;
  }

  async function fetchMetaBatch(rawInitData, blockKeys) {
    const initData = withLivePlaySeq(rawInitData);
    const params = seatQueryParams(initData, blockKeys);
    const payload = await fetchJson(`/onestop/api/seatMeta?${params}`);
    return Array.isArray(payload) ? payload : payload?.data || [];
  }

  // seatStatus answers with one hex string per requested block, 4 seats per
  // character, MSB first, aligned to that block's seatMeta order. A set bit
  // means the seat is free right now. isExposable only means the seat is part
  // of the sellable map — a sold-out show still reports it for every seat.
  function decodeStatusMask(hexString) {
    const flags = [];
    for (const char of String(hexString || "").trim()) {
      const value = Number.parseInt(char, 16);
      if (Number.isNaN(value)) continue;
      flags.push(Boolean((value >> 3) & 1), Boolean((value >> 2) & 1), Boolean((value >> 1) & 1), Boolean(value & 1));
    }
    return flags;
  }

  function parseSeatStatus(payload) {
    const data = payload && !Array.isArray(payload) ? payload.data : payload;
    if (!Array.isArray(data)) return [];
    // Position is the only thing tying a mask to a block.
    //
    // seatStatus answers with one hex string per *requested* block and no keys,
    // so every consumer matches masks to blocks by index — fetchBlockSeats does
    // `masks[index]` against `meta[index]`, and fetchMasksFor's own docstring
    // spells out that collapsing a failure "would shift every later mask onto
    // the wrong block — silently, and it would read as seats freeing in places
    // they did not". That guard was built at the request level and then undone
    // here: filtering non-strings out compacted the array, so one null entry
    // handed every block after it its neighbour's bitmap.
    //
    // A null mask is safe (seatIsFree returns false for it). A shifted one is
    // not: it sends the run clicking seats that were never free, which is what
    // "연속 N회 거절 — 좌석맵이 보여주는 빈자리를 서버가 거부하고 있습니다" looks
    // like from the panel.
    return data.map((entry) => (typeof entry === "string" ? decodeStatusMask(entry) : null));
  }

  async function fetchSeatStatus(rawInitData, blockKeys = []) {
    const initData = withLivePlaySeq(rawInitData);
    const params = seatQueryParams(initData, blockKeys);
    try {
      const payload = await fetchJson(`/onestop/api/seatStatus?${params}`);
      seatState.statusFailures = 0;
      return payload;
    } catch (error) {
      // Counted, not swallowed. A dead endpoint used to be indistinguishable
      // from "no seat freed", so 취켓팅 could watch a broken feed for hours
      // looking perfectly healthy.
      seatState.statusFailures = (seatState.statusFailures || 0) + 1;
      seatState.lastStatusError = String(error).slice(0, 120);
      return null;
    }
  }

  // A short ring of the seat-related calls and what came back. Without it every
  // failure looked the same from outside the browser — the page shows its own
  // "좌석 선택 도중 오류" dialog whoever caused it, and the panel could only
  // repeat that. Read through NOLSniper.status().seat.trace.
  const TRACE_LIMIT = 24;
  // Parked on `window`, not in this closure. `reload_autopilot` re-runs the
  // whole IIFE, which is how every fix gets deployed — with the array declared
  // here, each deployment wiped the evidence from the attempt that motivated it.
  const trace = (window.__nolsniperTrace = window.__nolsniperTrace || []);

  function traceCall(label, request, response) {
    trace.push({
      at: new Date().toISOString().slice(11, 23),
      label,
      request: typeof request === "string" ? request.slice(0, 300) : request,
      response: typeof response === "string" ? response.slice(0, 400) : response,
    });
    while (trace.length > TRACE_LIMIT) trace.shift();
  }

  // Watch the *page's* own booking calls, not just ours. When clicking a seat by
  // hand fails the same way the autopilot does, the fault is in the session or
  // the account rather than in anything we send — and this is the only way to
  // see the answer the site got, without spending a single extra request.
  /**
   * Watch the page's own booking calls.
   *
   * `sent` carries when the request left, in both clocks. Until it did, the
   * only stamp was the answer, and the one number that decides whether an API
   * soft hold could ever be faster than the map click is the gap between our
   * pointer press and the page's own preselect leaving the browser. If that
   * gap is a millisecond there is nothing in front of the round trip to win.
   */
  function notePageSeatNet(label, status, text, sent = null) {
    const net = (window.__nolsniperLastSeatNet = window.__nolsniperLastSeatNet || {});
    // After this callback has filled `net`, wake anyone waiting on it.
    queueMicrotask(resolveSeatNetWaiters);
    const at = Date.now();
    const body = String(text || "");
    const name = String(label || "");
    if (/preselect/i.test(name)) {
      net.preselectSentAt = sent?.at ?? at;
      // Both halves of the site's own hold, on the catch's clock.
      if (sent?.perf != null) noteCatchStage("preselectSent", sent.perf);
      noteCatchStage("preselectDone");
      net.preselectAt = at;
      net.preselectOk =
        status >= 200 &&
        status < 300 &&
        (/preselectSeat"\s*:\s*true/i.test(body) ||
          /bulkPreselectSeats"\s*:\s*true/i.test(body) ||
          (!/preselectSeat"\s*:\s*false/i.test(body) && !/P40\d{3}/.test(body) && !/"errors"\s*:\s*\[/.test(body)));
      if (/preselectSeat"\s*:\s*false/i.test(body) || /P40\d{3}/.test(body) || status >= 400) {
        net.preselectOk = false;
      }
    }
    if (/^select$/i.test(name) || /select-external/i.test(name) || /\/seats\/select/i.test(name)) {
      net.selectAt = at;
      net.selectStatus = status;
      net.selectOk = status >= 200 && status < 300;
      if (status >= 400 || /P40021|CONFIRM_PRESELECTION/i.test(body)) net.selectOk = false;
      const unsel = body.match(/unselectableSeatInfoIds"\s*:\s*\[([^\]]*)\]/);
      if (unsel && unsel[1].replace(/\s/g, "")) net.selectOk = false;
    }
  }

  function installNetworkWatch() {
    // Always refresh the recorder so a script reload picks up new parsing
    // without re-wrapping fetch/XHR (which would stack wrappers forever).
    window.__nolsniperNotePageSeatNet = notePageSeatNet;
    window.__nolsniperNotePageSeatStatus = notePageSeatStatus;
    // v5+ records select/preselect outcomes. Older hooks only traced; rebuild once.
    if (window.__nolsniperNetWatchNotes) return;
    window.__nolsniperNetWatchNotes = true;
    window.__nolsniperNetWatch = true;

    const nativeFetch = window.__nolsniperNativeFetch || window.fetch;
    if (typeof nativeFetch === "function") {
      window.__nolsniperNativeFetch =
        typeof nativeFetch.bind === "function" ? nativeFetch.bind(window) : nativeFetch;
      window.fetch = async function nolsniperFetch(input, init) {
        const url = String(input?.url || input || "");
        const watched = /\/onestop\/(gql|api\/(seats|seatStatus|seatMeta))/.test(url);
        // Taken before the await, so it is when the request left rather than
        // when it came back.
        const sent = watched ? { at: Date.now(), perf: performance.now() } : null;
        const response = await window.__nolsniperNativeFetch.apply(window, arguments);
        if (!watched) return response;
        try {
          const body = String(init?.body || "").slice(0, 200);
          const text = await response.clone().text();
          const label = (body.match(/mutation\s+(\w+)/) || [])[1] || url.split("?")[0].split("/").pop();
          if (label === "seatStatus") window.__nolsniperNotePageSeatStatus?.(url, text);
          window.__nolsniperNotePageSeatNet?.(label, response.status, text, sent);
          traceCall(`page:${label}`, body, `HTTP ${response.status} ${text}`);
        } catch {
          /* opaque or already consumed */
        }
        return response;
      };
    }

    // The onestop SPA talks through axios, which is XMLHttpRequest — a fetch
    // hook alone sees none of the page's own booking calls.
    if (!window.__nolsniperXhrHooked) {
      window.__nolsniperXhrHooked = true;
      const nativeOpen = XMLHttpRequest.prototype.open;
      const nativeSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function nolsniperOpen(method, url) {
        this.__nolsniperUrl = String(url || "");
        return nativeOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function nolsniperSend(body) {
        const url = this.__nolsniperUrl || "";
        if (/\/onestop\/(gql|api\/(seats|seatStatus|seatMeta))/.test(url)) {
          // The onestop SPA talks through axios, so this is the hook that sees
          // the page's real preselect. Stamped here, before the native send.
          const sentAt = { at: Date.now(), perf: performance.now() };
          this.addEventListener("loadend", () => {
            try {
              const sent = String(body || "").slice(0, 200);
              const label = (sent.match(/mutation\s+(\w+)/) || [])[1] || url.split("?")[0].split("/").pop();
              const text = String(this.responseText || "");
              if (label === "seatStatus") window.__nolsniperNotePageSeatStatus?.(url, text);
              window.__nolsniperNotePageSeatNet?.(label, this.status, text, sentAt);
              traceCall(`page:${label}`, sent, `HTTP ${this.status} ${text.slice(0, 400)}`);
            } catch {
              /* response not readable as text */
            }
          });
        }
        return nativeSend.apply(this, arguments);
      };
    }
  }

  async function gql(query, variables) {
    const initData = getInitData();
    const response = await fetch("/onestop/gql", {
      method: "POST",
      credentials: "include",
      headers: {
        ...onestopHeaders(initData),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ query, variables }),
    });
    const name = (String(query).match(/mutation\s+(\w+)|query\s+(\w+)/) || [])[1] || "gql";
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      traceCall(name, variables, `HTTP ${response.status} ${detail}`);
      let parsed = null;
      try {
        parsed = JSON.parse(detail);
      } catch (error) {
        /* not JSON */
      }
      const blockedMs = readGatewayBlock(parsed?.errors ?? parsed, {
        status: response.status,
        headers: response.headers,
      });
      if (blockedMs >= 0) throw noteGatewayBlock(blockedMs, "/onestop/gql");
      throw new Error(`gql HTTP ${response.status}${detail ? ` · ${detail.slice(0, 160)}` : ""}`);
    }
    const payload = await response.json();
    if (payload?.errors?.length) {
      traceCall(name, variables, payload.errors);
      const blockedMs = readGatewayBlock(payload.errors);
      if (blockedMs >= 0) throw noteGatewayBlock(blockedMs, "/onestop/gql");
      throw new Error(payload.errors[0]?.message || "gql error");
    }
    traceCall(name, variables, payload?.data);
    return payload?.data;
  }

  async function preselectSeat(initData, seat) {
    const playSeq = initData.playSeq?.playSeq;
    const blockKey = lookupBlockKey(seat);
    const seatGrade = seat?.seatGrade;
    if (!playSeq || !blockKey || !seatGrade || !seat?.seatInfoId) {
      throw new Error("preselect 좌석 정보 부족");
    }
    const query = `mutation PreselectSeat($command: PreselectSeatCommand!) {
      preselectSeat(command: $command)
    }`;
    return gql(query, {
      command: {
        playSeq,
        blockKey: String(blockKey),
        seatGrade: String(seatGrade),
        seatInfoId: seat.seatInfoId,
      },
    });
  }

  async function bulkPreselectSeats(initData, seats) {
    if (!seats?.length) throw new Error("preselect 좌석 없음");
    const playSeq = initData.playSeq?.playSeq;
    const blockKey = lookupBlockKey(seats[0]);
    const seatGrade = seats[0].seatGrade;
    if (!playSeq || !blockKey || !seatGrade) {
      throw new Error("preselect 구역/등급 없음");
    }
    const query = `mutation BulkPreselectSeats($command: BulkPreselectSeatsCommand!) {
      bulkPreselectSeats(command: $command)
    }`;
    // Measured: for a single seat the bulk mutation answers P40021
    // "좌석 요청이 잘못 되었습니다" while the singular preselectSeat answers true
    // for that same seat in the same session. Bulk appears to be meant for seat
    // groups, so it is only used when there is genuinely more than one seat.
    if (seats.length === 1) return preselectSeat(initData, seats[0]);

    try {
      return await gql(query, {
        command: {
          playSeq,
          blockKey: String(blockKey),
          seatGrade: String(seatGrade),
          seatInfoIds: seats.map((seat) => seat.seatInfoId),
        },
      });
    } catch (error) {
      // Never retry a gateway block, and never fan one failure out into N more
      // requests while the gateway is already unhappy — that is what turned a
      // single refusal into the block in the first place.
      if (error?.gatewayBlockedMs >= 0) throw error;
      log("bulk preselect failed, trying one by one", error);
      for (const seat of seats) await preselectSeat(initData, seat);
      return true;
    }
  }

  // Server-side allocator. Faster than scanning + preselecting because the
  // backend picks the seats itself, and it is the only path for shows whose
  // seat map is not individually selectable.
  async function autoAssignSeats(initData, { blockKey, seatGrade, seatInfoIds = [] }) {
    const playSeq = initData.playSeq?.playSeq;
    if (!playSeq || !seatGrade) return null;
    const query = `mutation AutoAssignSeats($command: AutoAssignSeatsCommand!) {
      autoAssignSeats(command: $command) {
        seatInfoIds
        success
        errorCode
        errorMessage
      }
    }`;
    const data = await gql(query, {
      command: { playSeq, blockKey: blockKey ? String(blockKey) : null, seatGrade: String(seatGrade), seatInfoIds },
    });
    return data?.autoAssignSeats || null;
  }

  function resolveSeatType(goods) {
    if (goods?.isSportOneStop || goods?.isSportsGroup) return "SPORTS";
    if (String(goods?.kindOfGoods || "") === "01007") return "SPORTS";
    return "DEFAULT";
  }

  async function collectApiCandidates(initData, gradeOrder, blockKeys, config = {}) {
    const allBlocks = await fetchBlockKeys(initData);
    let keys = resolveBlockKeys({ block_keys: blockKeys, block_names: config.block_names || [] }, allBlocks);
    if (!keys.length) keys = allBlocks.map((block) => block.blockKey).filter(Boolean);
    if (blockKeys.length) {
      const allowed = new Set(blockKeys.map(String));
      keys = keys.filter((key) => allowed.has(String(key)));
    }
    if (!keys.length) return [];
    const enough = Math.max(40, (Number(config.quantity) || 1) * 20);
    const batches = chunk(keys, 2);
    const collected = await mapLimit(
      batches,
      SEAT_META_CONCURRENCY,
      (batch) => fetchBlockSeats(initData, batch),
      (done) => done.reduce((sum, blocks) => sum + countFree(blocks), 0) >= enough,
    );
    const blocks = collected.flat();
    seatState.lastBlocks = blocks;
    // 좌석 잡기 stops fetching the moment it has 40 free seats to choose from —
    // right for grabbing a seat now, poisonous for the watch, which reads
    // lastBlocks as its whole picture of the venue and only rebuilds it when
    // it is *empty*. So one 좌석 잡기 on a busy show left the following 취켓팅
    // sweeping the two or three blocks that happened to satisfy the quota,
    // silently, for as long as it ran.
    seatState.lastBlocksComplete = lastMapComplete;
    seatState.mapCenterX = venueCenterX(blocks);
    seatState.mapStage = stagePoint(blocks);
    if (!seatState.showCatalog?.sketch?.length) {
      const sketch = sketchFromSeatBlocks(blocks);
      if (sketch.length) {
        parkSketch(sketch);
        seatState.showCatalog = { ...(seatState.showCatalog || { fetched_at: Date.now(), blocks: [], grades: [], errors: [] }), sketch };
      }
    }
    const candidates = collectFromBlocks(blocks, config);
    return rankCandidates(candidates, gradeOrder, blockKeys, pickerOptions(config));
  }

  // Pairs each block's static seat list with its live availability mask. The two
  // endpoints answer in the requested block order, so they join positionally.
  async function fetchBlockSeats(initData, blockKeys) {
    const [meta, status] = await Promise.all([
      fetchMetaBatch(initData, blockKeys),
      fetchSeatStatus(initData, blockKeys),
    ]);
    const masks = parseSeatStatus(status);
    // Key the mask to the block, not to its position in a different response.
    //
    // seatStatus is positional against `blockKeys`, but this maps over `meta`,
    // which is a *separate* call and need not answer for every key it was
    // given. Request [A, B], get meta [B] because A had nothing: meta[0] is B
    // and masks[0] is A's, so B is handed A's bitmap under B's own name. The
    // sweep at applyBlockMask already does this correctly — it looks the block
    // up by key and only trusts position for the mask — and this is the same
    // pairing done the unsafe way.
    const maskByKey = new Map(
      blockKeys.map((key, index) => [String(key), masks[index] ?? null]),
    );
    return (meta || []).map((block, index) => {
      const key = block?.blockKey || blockKeys[index] || null;
      const keyed = key != null && maskByKey.has(String(key));
      return {
        blockKey: key,
        seats: block?.seats || [],
        mask: keyed ? maskByKey.get(String(key)) : masks[index] || null,
      };
    });
  }

  function seatIsFree(block, position) {
    if (!block.mask) return false;
    return position < block.mask.length && block.mask[position];
  }

  function countFree(blocks) {
    let total = 0;
    for (const block of blocks || []) {
      for (let index = 0; index < (block.seats || []).length; index += 1) {
        if (seatSellable(block.seats[index]) && seatIsFree(block, index)) total += 1;
      }
    }
    return total;
  }

  // Never NaN and never undefined — a NaN sort key poisons every comparison it
  // touches, and `undefined` is indistinguishable from a real 0 coordinate.
  function numOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function toCandidate(seat, blockKey) {
    return {
      seatInfoId: seat.seatInfoId,
      seatGrade: String(seat.seatGrade),
      seatGradeName: seat.seatGradeName || "",
      rowNo: seat.rowNo || "",
      seatNo: seat.seatNo || "",
      blockKey,
      seatGroupId: seat.seatGroupId || null,
      // Venue-global map coordinates, and the whole basis for the aiming
      // strategies. rowIdx/colIdx are deliberately not carried: they restart at
      // 0 in every block, so they cannot order seats across blocks.
      posLeft: numOrNull(seat.posLeft),
      posTop: numOrNull(seat.posTop),
      floor: seat.floor || "",
      label: `[${seat.seatGradeName || seat.seatGrade}] ${seat.rowNo || ""} ${seat.seatNo || ""}`.trim(),
    };
  }

  function normalizeWatchRect(value) {
    if (!value || typeof value !== "object") return null;
    const left = numOrNull(value.left);
    const top = numOrNull(value.top);
    const right = numOrNull(value.right);
    const bottom = numOrNull(value.bottom);
    if (left == null || top == null || right == null || bottom == null) return null;
    const rect = {
      left: Math.min(left, right),
      top: Math.min(top, bottom),
      right: Math.max(left, right),
      bottom: Math.max(top, bottom),
    };
    if (rect.right - rect.left < 0.5 || rect.bottom - rect.top < 0.5) return null;
    return rect;
  }

  function seatInWatchRect(seat, rect) {
    if (!rect) return true;
    const x = numOrNull(seat?.posLeft);
    const y = numOrNull(seat?.posTop);
    if (x == null || y == null) return false;
    return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  }

  function collectFromBlocks(blocks, config) {
    sweepTakenCooldowns();
    const watch = normalizeWatchRect(config?.watch_rect);
    const candidates = [];
    const anywhere = [];
    // Seats whose *position* falls in the box, whatever their status. This is
    // the only honest test of whether the box belongs to this map, and it must
    // not depend on availability — see the fallback below.
    let seatsInsideBox = 0;
    let seatsWithPosition = 0;

    for (const block of blocks) {
      for (let index = 0; index < block.seats.length; index += 1) {
        const seat = block.seats[index];
        if (watch && seat && numOrNull(seat.posLeft) !== null && numOrNull(seat.posTop) !== null) {
          seatsWithPosition += 1;
          if (seatInWatchRect(seat, watch)) seatsInsideBox += 1;
        }
        if (!seat?.isExposable || !seat?.seatGrade || !seat?.seatInfoId) continue;
        if (!seatIsFree(block, index)) continue;
        // Someone else just took this one. The bitmap can still call it free
        // for a while, so without this the next pass offers the same seat and
        // races the same person for it again.
        if (seatInCooldown(seat.seatInfoId)) continue;
        if (seatUnreachableNow(seat.seatInfoId)) continue;
        if (seatHeldByUs(seat.seatInfoId)) continue;
        if (seat.seatGroupId && config.allow_group_seats === false) continue;
        anywhere.push([seat, block.blockKey]);
        if (!seatInWatchRect(seat, watch)) continue;
        candidates.push(toCandidate(seat, block.blockKey));
      }
    }

    // Only a box containing no seats *at all* is alien — it is a rect from a
    // different show, since seat coordinates live in each show's own space with
    // no common scale (posTop spans 52-111 on one venue, 1168-1183 on another).
    //
    // A box containing seats but no free ones is the ordinary state of 취켓팅:
    // you are waiting for one to open. Testing free seats instead of any seats
    // made the watch abandon the chosen area the moment it was full and take a
    // seat somewhere else entirely.
    if (watch && seatsWithPosition && !seatsInsideBox) {
      seatState.watchRectIgnored = true;
      seatState.lastError =
        "감시 구역이 이 공연의 좌석과 맞지 않아 무시했습니다. 구역 선택에서 다시 지정하세요.";
      return anywhere.map(([seat, blockKey]) => toCandidate(seat, blockKey));
    }
    seatState.watchRectIgnored = false;
    return candidates;
  }

  // Grades with remainCount 0 can still show free bits on a stale bitmap. Soft
  // hold may light up the sidebar, then 선택 완료 answers with 좌석 선택 도중
  // 오류 / P40021. Prefer seats whose grade still has stock when we know it.
  function filterSoldOutGradeCandidates(candidates, remains) {
    if (!remains || !Object.keys(remains).length) return candidates;
    const kept = candidates.filter((seat) => {
      const name = seat.seatGradeName || seat.seatGrade;
      const n = remains[name];
      if (n === undefined || n === null) return true;
      return Number(n) > 0;
    });
    return kept.length ? kept : candidates;
  }

  async function fetchGradeRemains(initData) {
    if (!initData?.goods || !initData?.playSeq) return {};
    try {
      const params = new URLSearchParams({
        goodsCode: initData.goods.goodsCode,
        placeCode: initData.goods.placeCode,
        playSeq: initData.playSeq.playSeq,
        bizCode: initData.bizCode || "WEBBR",
      });
      const payload = await fetchJson(`/onestop/api/seats/grades?${params}`);
      const rows = payload?.grades || payload?.data || payload || [];
      const remains = {};
      if (Array.isArray(rows)) {
        for (const row of rows) {
          const name = row.seatGradeName || row.name || String(row.seatGrade || row.grade || "");
          if (!name) continue;
          remains[name] = Number(row.remainCount ?? row.remainSeatCount ?? row.remainCnt ?? 0);
        }
      }
      seatState.remainByGrade = remains;
      return remains;
    } catch {
      return seatState.remainByGrade || {};
    }
  }

  function isoDate(compact) {
    const digits = compactDate(compact);
    if (!digits) return "";
    return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`;
  }

  function normalizePlayTime(value) {
    const match = String(value || "").trim().match(/^(\d{1,2}):(\d{2})$/);
    if (!match) return null;
    return `${match[1].padStart(2, "0")}:${match[2]}`;
  }

  // NOL BookingDayPicker marks the chosen round with data-is-selected on the
  // showtime <button>, and puts HH:MM on a nested <time dateTime>.
  function readSelectedShowtime() {
    const buttons = [...document.querySelectorAll('button[data-is-selected="true"]')];
    for (const button of buttons) {
      const timeEl = button.querySelector("time");
      const raw = (timeEl?.getAttribute("dateTime") || timeEl?.textContent || "").trim();
      const playTime = normalizePlayTime(raw);
      if (!playTime) continue;
      const remainText = button.innerText || "";
      const remainByGrade = {};
      for (const match of remainText.matchAll(/([A-Z가-힣0-9]+석)\s*(\d+)/g)) {
        remainByGrade[match[1]] = Number(match[2]);
      }
      return { play_time: playTime, remain_by_grade: remainByGrade, source: "showtime-card" };
    }
    const timeOnly = document.querySelector('time[data-is-selected="true"]');
    const fallback = normalizePlayTime(timeOnly?.getAttribute("dateTime") || timeOnly?.textContent);
    if (fallback) return { play_time: fallback, remain_by_grade: {}, source: "time-chip" };
    return null;
  }

  function readSelectedPlayDate() {
    // Caption is rendered as yyyy.MM (see BookingDayPicker formatters).
    const bodyText = document.body?.innerText || "";
    const monthMatch = bodyText.match(/(\d{4})\.(\d{1,2})(?!\d)/);
    const year = monthMatch ? Number(monthMatch[1]) : null;
    const month = monthMatch ? Number(monthMatch[2]) : null;

    const selectedCell =
      document.querySelector('[role="gridcell"][aria-selected="true"]') ||
      document.querySelector('button[aria-selected="true"]') ||
      document.querySelector('[aria-selected="true"]');
    if (selectedCell && year && month) {
      const dayMatch = String(selectedCell.textContent || "")
        .trim()
        .match(/^(\d{1,2})$/);
      if (dayMatch) {
        const day = Number(dayMatch[1]);
        return `${year}${String(month).padStart(2, "0")}${String(day).padStart(2, "0")}`;
      }
    }

    // Some builds expose the active day as a blue circle whose label is only
    // the day number; pair it with the caption when aria-selected is missing.
    if (year && month) {
      const dayButtons = [...document.querySelectorAll("button, [role='gridcell']")].filter((node) =>
        /^\d{1,2}$/.test(String(node.textContent || "").trim()),
      );
      for (const node of dayButtons) {
        const selected =
          node.getAttribute("aria-selected") === "true" ||
          node.getAttribute("data-is-selected") === "true" ||
          /selected|active|primary/i.test(node.className || "");
        if (!selected) continue;
        const day = Number(String(node.textContent).trim());
        if (day >= 1 && day <= 31) {
          return `${year}${String(month).padStart(2, "0")}${String(day).padStart(2, "0")}`;
        }
      }
    }
    return null;
  }

  function readProductSelection() {
    if (!isNolProductPage()) return null;
    const showtime = readSelectedShowtime();
    const playDate = readSelectedPlayDate();
    return {
      play_date: playDate,
      play_time: showtime?.play_time || null,
      remain_by_grade: showtime?.remain_by_grade || {},
    };
  }

  function pickSchedule(schedules, { play_date, play_time, play_seq }) {
    const rows = Array.isArray(schedules) ? schedules : [];
    if (!rows.length) return null;
    if (play_seq) {
      const bySeq = rows.find((row) => String(row.playSeq || row.playSequence || "") === String(play_seq));
      if (bySeq) return bySeq;
    }
    const dateIso = isoDate(play_date);
    const onDate = dateIso
      ? rows.filter((row) => compactDate(row.playDate) === compactDate(play_date) || String(row.playDate) === dateIso)
      : rows;
    const pool = onDate.length ? onDate : rows;
    if (play_time) {
      const want = normalizePlayTime(play_time);
      const byTime = pool.find((row) => normalizePlayTime(row.playTime) === want);
      if (byTime) return byTime;
    }
    return pool[0] || null;
  }

  async function fetchProductSchedules(goodsCode, placeCode, playDate) {
    if (!goodsCode || !placeCode) return [];
    const key = `${goodsCode}:${placeCode}`;
    const iso = isoDate(playDate);
    // Empty dates 500 on NOL; always send a real yyyy-MM-dd window.
    const start = iso || isoDate(new Date().toISOString().slice(0, 10).replace(/-/g, ""));
    const end = iso || start;
    const payload = await fetchNolJson(
      `/ticket/products/api/schedules?goodsKey=${encodeURIComponent(key)}&playStartDate=${encodeURIComponent(start)}&playEndDate=${encodeURIComponent(end)}`,
    );
    return payload?.content || payload?.data?.content || payload?.schedules || [];
  }

  async function fetchShowCatalog() {
    const ctx = readShowContext();
    const catalog = {
      fetched_at: Date.now(),
      goods_code: ctx.goods_code,
      goods_name: ctx.goods_name,
      place_code: ctx.place_code,
      play_date: ctx.play_date,
      play_seq: ctx.play_seq,
      play_time: ctx.play_time || null,
      page: ctx.page,
      ready: ctx.ready,
      url: ctx.url,
      ticket_open_date: null,
      ticket_open_kst: null,
      grades: [],
      blocks: [],
      sketch: [],
      schedules: [],
      remain_by_grade: {},
      errors: [],
    };

    const html = document.documentElement?.innerHTML || "";
    const openMatch = html.match(/"ticketOpenDate":"(\d{12})"/);
    if (openMatch) {
      catalog.ticket_open_date = openMatch[1];
      const raw = openMatch[1];
      catalog.ticket_open_kst = `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)} ${raw.slice(8, 10)}:${raw.slice(10, 12)}:00`;
    }

    // Product page: mirror the date/round the user actually selected, not the
    // run's first playDate (which is often sold out / 0석).
    if (isNolProductPage() && ctx.goods_code) {
      const selection = readProductSelection() || {};
      if (selection.play_date) catalog.play_date = selection.play_date;
      if (selection.play_time) catalog.play_time = selection.play_time;

      // Schedules need a yyyy-MM-dd window. Prefer the selected day; otherwise
      // the visible calendar month (caption is yyyy.MM).
      let scheduleDate = catalog.play_date;
      if (!scheduleDate) {
        const monthMatch = (document.body?.innerText || "").match(/(\d{4})\.(\d{1,2})(?!\d)/);
        if (monthMatch) {
          scheduleDate = `${monthMatch[1]}${String(monthMatch[2]).padStart(2, "0")}01`;
        }
      }

      if (ctx.place_code && scheduleDate) {
        try {
          catalog.schedules = await fetchProductSchedules(ctx.goods_code, ctx.place_code, scheduleDate);
          const picked = pickSchedule(catalog.schedules, {
            play_date: catalog.play_date,
            play_time: catalog.play_time,
            play_seq: catalog.play_seq,
          });
          if (picked?.playSeq || picked?.playSequence) {
            catalog.play_seq = String(picked.playSeq || picked.playSequence);
          }
          if (!catalog.play_time && picked?.playTime) {
            catalog.play_time = normalizePlayTime(picked.playTime);
          }
          if (!catalog.play_date && picked?.playDate) {
            catalog.play_date = compactDate(picked.playDate);
          }
        } catch (error) {
          catalog.errors.push(`schedules: ${error}`);
        }
      }

      const playSeq = catalog.play_seq;
      if (playSeq) {
        try {
          const payload = await fetchNolJson(
            `/ticket/products/api/remaining-seats?goodsCode=${encodeURIComponent(ctx.goods_code)}&playSeq=${encodeURIComponent(playSeq)}`,
          );
          const rows = payload?.remainSeat || payload?.data?.remainSeat || [];
          catalog.grades = rows.map((row) => ({
            grade: String(row.seatGrade),
            name: row.seatGradeName || String(row.seatGrade),
            remain: Number(row.remainCnt) || 0,
            play_seq: row.playSeq || playSeq,
          }));
        } catch (error) {
          catalog.errors.push(`remaining-seats: ${error}`);
        }
      }

      // If the API lagged, fall back to the numbers already painted on the card.
      if (!catalog.grades.length && selection.remain_by_grade && Object.keys(selection.remain_by_grade).length) {
        catalog.grades = Object.entries(selection.remain_by_grade).map(([name, remain]) => ({
          grade: name,
          name,
          remain: Number(remain) || 0,
          play_seq: catalog.play_seq,
        }));
      }
      for (const row of catalog.grades) {
        catalog.remain_by_grade[row.name] = row.remain;
      }
    }

    const playSeq = catalog.play_seq || ctx.play_seq || "001";
    if (ctx.goods_code && !catalog.grades.length && !isSeatPage()) {
      try {
        const payload = await fetchNolJson(
          `/ticket/products/api/remaining-seats?goodsCode=${encodeURIComponent(ctx.goods_code)}&playSeq=${encodeURIComponent(playSeq)}`,
        );
        const rows = payload?.remainSeat || payload?.data?.remainSeat || [];
        catalog.grades = rows.map((row) => ({
          grade: String(row.seatGrade),
          name: row.seatGradeName || String(row.seatGrade),
          remain: Number(row.remainCnt) || 0,
          play_seq: row.playSeq || playSeq,
        }));
        for (const row of catalog.grades) {
          catalog.remain_by_grade[row.name] = row.remain;
        }
        if (!catalog.play_seq && catalog.grades[0]?.play_seq) {
          catalog.play_seq = catalog.grades[0].play_seq;
        }
      } catch (error) {
        catalog.errors.push(`remaining-seats: ${error}`);
      }
    }

    const initData = getInitData();
    if (initData?.goods && initData?.playSeq) {
      try {
        const blocks = await fetchBlockKeys(initData);
        await enrichCatalogSketch(initData, blocks);
        if (seatState.showCatalog?.blocks?.length) {
          catalog.blocks = seatState.showCatalog.blocks;
        }
        if (seatState.showCatalog?.sketch?.length) {
          catalog.sketch = seatState.showCatalog.sketch;
        }
        if (!catalog.play_date && initData.playSeq?.playDate) {
          catalog.play_date = compactDate(initData.playSeq.playDate);
        }
        if (!catalog.play_seq && initData.playSeq?.playSeq) {
          catalog.play_seq = initData.playSeq.playSeq;
        }
        if (!catalog.grades.length) {
          try {
            const gparams = new URLSearchParams({
              goodsCode: initData.goods.goodsCode,
              placeCode: initData.goods.placeCode,
              playSeq: initData.playSeq.playSeq,
              bizCode: initData.bizCode || "WEBBR",
            });
            const gradesPayload = await fetchJson(`/onestop/api/seats/grades?${gparams}`);
            const rows = gradesPayload?.grades || gradesPayload?.data || gradesPayload || [];
            if (Array.isArray(rows)) {
              catalog.grades = rows.map((row) => ({
                grade: String(row.seatGrade || row.grade),
                name: row.seatGradeName || row.name || String(row.seatGrade),
                remain: Number(row.remainSeatCount ?? row.remainCnt ?? 0),
                play_seq: initData.playSeq.playSeq,
              }));
            }
          } catch (error) {
            catalog.errors.push(`grades: ${error}`);
          }
        }
      } catch (error) {
        catalog.errors.push(`block-data: ${error}`);
      }
    }

    if ((!catalog.blocks || !catalog.blocks.length) && seatState.discoveredBlocks?.length) {
      catalog.blocks = seatState.discoveredBlocks.map(catalogBlockRow);
    }
    if ((!catalog.sketch || !catalog.sketch.length) && seatState.showCatalog?.sketch?.length) {
      catalog.sketch = seatState.showCatalog.sketch;
    }

    // The round the panel picked is the round — the page's calendar default
    // (the 08-05 14:00 it opens on) must not overwrite it in what we publish
    // or in the toast. The remain count stays the page's, so say whose it is.
    let hintedRound = null;
    try {
      const hint = loadShowHint();
      if (hint && hint.play_seq && String(hint.goods_code || "").toUpperCase() === String(catalog.goods_code || "").toUpperCase()) {
        hintedRound = hint;
        catalog.play_seq = String(hint.play_seq);
        if (hint.play_date) catalog.play_date = compactDate(hint.play_date);
        if (hint.play_time) catalog.play_time = normalizePlayTime(hint.play_time);
      }
    } catch (error) { /* a hint is optional */ }
    seatState.showCatalog = catalog;
    if (isNolProductPage()) {
      const total = catalog.grades.reduce((sum, row) => sum + (Number(row.remain) || 0), 0);
      const round = [catalog.play_date, catalog.play_time || catalog.play_seq, catalog.play_seq && catalog.play_time ? catalog.play_seq : ""].filter(Boolean).join(" ");
      const pageDate = compactDate((readProductSelection() || {}).play_date);
      const remainNote = hintedRound && pageDate && pageDate !== catalog.play_date ? ` (페이지 표시일 ${pageDate} 기준)` : "";
      updateOverlay(
        `예매판 동기화<br>${catalog.goods_name || catalog.goods_code || "?"}<br>${round || "회차 확인 중"} · 잔여 ${total}석${remainNote}`,
        catalog.errors.length ? "warn" : "ok",
      );
    } else {
      updateOverlay(
        `공연 정보 수집<br>${catalog.goods_name || catalog.goods_code || "?"}<br>등급 ${catalog.grades.length} · 구역 ${catalog.blocks.length}` +
          (catalog.sketch?.length ? ` · 좌석점 ${catalog.sketch.length}` : ""),
        catalog.errors.length ? "warn" : "ok",
      );
    }
    return catalog;
  }

  async function syncGrades(config) {
    const initData = getInitData();
    if (initData?.goods && initData?.playSeq) {
      const blocks = await fetchBlockKeys(initData);
      const keys = resolveBlockKeys(config, blocks);
      const blockKeys = keys.length
        ? blocks.filter((block) => keys.includes(String(block.blockKey)))
        : config.block_keys?.length
          ? blocks.filter((block) => config.block_keys.includes(block.blockKey))
          : blocks;
      const metaKeys = blockKeys.map((block) => block.blockKey).filter(Boolean);
      const availableByGrade = {};
      let total = 0;
      if (metaKeys.length) {
        const metaBlocks = (
          await mapLimit(chunk(metaKeys, 2), SEAT_META_CONCURRENCY, (batch) => fetchBlockSeats(initData, batch))
        ).flat();
        for (const block of metaBlocks) {
          for (let index = 0; index < block.seats.length; index += 1) {
            const seat = block.seats[index];
            if (!seat?.isExposable || !seatIsFree(block, index)) continue;
            const grade = seat.seatGradeName || seat.seatGrade;
            availableByGrade[grade] = (availableByGrade[grade] || 0) + 1;
            total += 1;
          }
        }
      }
      seatState.syncedSummary = { total, byGrade: availableByGrade, blocks: blockKeys };
      return seatState.syncedSummary;
    }

    const ctx = readShowContext();
    if (!ctx.goods_code || !ctx.play_seq) throw new Error("상품·회차 정보가 없습니다");
    const payload = await fetchJson(
      `${NOL_ORIGIN}/ticket/products/api/remaining-seats?goodsCode=${encodeURIComponent(ctx.goods_code)}&playSeq=${encodeURIComponent(ctx.play_seq)}`,
    );
    const remain = payload?.remainSeat || [];
    const availableByGrade = {};
    let total = 0;
    for (const row of remain) {
      availableByGrade[row.seatGradeName || row.seatGrade] = row.remainCnt;
      total += Number(row.remainCnt) || 0;
    }
    seatState.syncedSummary = { total, byGrade: availableByGrade, blocks: [] };
    return seatState.syncedSummary;
  }

  // Did the page accept the pointer events? 선택 좌석 emptying out is the signal
  // the SPA itself uses, and it is what has to be true for 다음 to work.
  // The page has to make its own round trip and re-render before 선택 좌석 fills
  // in. Roughly 1.5s of headroom: falling through now means "the page declined
  // this seat", so being impatient would discard seats that were about to work.
  // How often to look for 선택 좌석 to rise after a click, and for how long.
  //
  // The shape matters more than the ceiling. The site's own preselect round
  // trip is a few hundred milliseconds, and the old ramp went straight from a
  // 16ms frame-check to an 80ms poll after six tries — so the cart almost
  // always landed inside an 80ms gap and sat there unnoticed. Measured against
  // a simulated 220ms preselect: 44.7ms of pure notice lag, every catch,
  // between the hold existing and this loop acting on it.
  //
  // A 24ms middle band covers the window a real preselect actually lands in.
  // The 80ms tail is kept for the pathological case, and the whole budget stays
  // inside the ceiling the confirm path is built around (~1.8s).
  const SEAT_MAP_SETTLE_MS = 16;
  const SEAT_MAP_SETTLE_FAST_TRIES = 8; // ~128ms of frame-rate looking
  const SEAT_MAP_SETTLE_MID_MS = 24;
  const SEAT_MAP_SETTLE_MID_TRIES = 44; // through ~992ms, where preselects land
  const SEAT_MAP_SETTLE_TRIES = 54;
  const SEAT_MAP_SETTLE_SLOW_MS = 80;

  // React settles in about a frame, so the first look used to be ~84ms later
  // than it needed to be — on every attempt, winning ones included, and on a
  // lost race that is the delay before the next seat is even tried. Look fast
  // first, then widen: a selection still missing after ~100ms is waiting on the
  // network, not on a render, and polling it hard only costs layouts. The
  // ceiling stays about the same 1.5s.
  function settleDelayFor(attempt) {
    if (attempt < SEAT_MAP_SETTLE_FAST_TRIES) return SEAT_MAP_SETTLE_MS;
    if (attempt < SEAT_MAP_SETTLE_MID_TRIES) return SEAT_MAP_SETTLE_MID_MS;
    return SEAT_MAP_SETTLE_SLOW_MS;
  }

  function settleBudgetMs() {
    let total = 0;
    for (let attempt = 0; attempt < SEAT_MAP_SETTLE_TRIES; attempt += 1) total += settleDelayFor(attempt);
    return total;
  }

  // How many seats the page currently holds. It renders the number itself
  // ("선택 좌석 4"), which is the only reading that stays correct once more than
  // one seat is involved.
  function countFromText(text) {
    if (/선택한\s*좌석이\s*없습니다/.test(text)) return 0;
    const match = text.match(/선택\s*좌석\s*(\d+)/);
    return match ? Number(match[1]) : -1;
  }

  /**
   * Read 선택 좌석 without laying out the whole document.
   *
   * The number lives in one small box, but this read went through
   * document.body.innerText — a full-document layout, on a 21,460-seat venue,
   * on the catch loop's own thread, ten times a second and again on every poll
   * of a click it is waiting to confirm.
   *
   * Every judgement about page state reads through here, and a wrong count is
   * not a slow macro but a destructive one: `held > quantity` hands back seats
   * we are holding. So the scoped node is never simply trusted. It is found by
   * agreeing with the body read, dropped the moment it stops parsing or leaves
   * the document, and re-checked against the body read periodically — one
   * disagreement and this gives up on scoping for good.
   */
  let seatCountNode = null;
  let seatCountScoped = 0;
  let seatCountScopeBroken = false;
  let seatCountSearchedAt = 0;
  const SEAT_COUNT_REVERIFY_EVERY = 25;
  const SEAT_COUNT_SEARCH_EVERY_MS = 2000;

  // The box belongs to a page. A run may start on a different one, and giving
  // up on scoping once must not condemn the whole session to the slow read.
  function resetSeatCountScope() {
    seatCountNode = null;
    seatCountScoped = 0;
    seatCountScopeBroken = false;
    seatCountSearchedAt = 0;
    seatState.seatCountScopeBroken = false;
  }

  function findSeatCountNode(expected) {
    let best = null;
    for (const node of document.querySelectorAll("div,section,aside,p,span")) {
      const text = node.innerText || "";
      // The tightest box that carries the number, not the panel containing it.
      if (!text || text.length > 120) continue;
      if (countFromText(text) !== expected) continue;
      if (!best || text.length < best.len) best = { node, len: text.length };
    }
    return best?.node || null;
  }

  function selectedSeatCount() {
    if (seatCountNode) {
      const connected = seatCountNode.isConnected !== false;
      const scoped = connected ? countFromText(seatCountNode.innerText || "") : -1;
      if (scoped >= 0) {
        seatCountScoped += 1;
        if (seatCountScoped % SEAT_COUNT_REVERIFY_EVERY !== 0) return scoped;
        // Periodic audit. React can swap the box for one that renders a stale
        // number, and a scoped read that has silently stopped tracking is worse
        // than the slow read it replaced.
        const truth = countFromText(pageTextWithoutOverlay());
        if (truth === scoped) return scoped;
        seatCountNode = null;
        seatCountScopeBroken = true;
        seatState.seatCountScopeBroken = true;
        return truth;
      }
      seatCountNode = null;
    }

    const count = countFromText(pageTextWithoutOverlay());
    if (
      count >= 0 &&
      !seatCountScopeBroken &&
      Date.now() - seatCountSearchedAt > SEAT_COUNT_SEARCH_EVERY_MS
    ) {
      seatCountSearchedAt = Date.now();
      seatCountNode = findSeatCountNode(count);
      seatCountScoped = 0;
    }
    return count;
  }

  // Wait for the page's own count to rise by `added`.
  //
  // This replaces a boolean "is 선택 좌석 still empty" check, which could only
  // ever detect the *first* selection. From the second attempt onwards the
  // panel was already non-empty, so a perfectly good selection was reported as
  // declined — and the loop moved on and clicked another seat. Four seats piled
  // up on a 매수 1 order while every attempt was recorded as a failure, until
  // the page refused with 선택 가능한 매수를 초과했어요.
  async function pageRegisteredSelection(before, added) {
    if (before < 0) return false; // count not readable; caller falls back
    for (let attempt = 0; attempt < SEAT_MAP_SETTLE_TRIES; attempt += 1) {
      await sleep(settleDelayFor(attempt));
      // Success first, deliberately. A modal can be on screen for reasons that
      // have nothing to do with this seat, and no overlay test may be allowed
      // to mask a selection the page actually registered.
      const now = selectedSeatCount();
      if (now >= before + added) return true;
      // The page has already answered — stop waiting on a count that will never
      // arrive. Polling the full 1.5s here is the difference between losing one
      // seat and losing the next one too, and during an open that is the whole
      // game.
      //
      // The structural test is the one that matters. NOL's real conflict modal
      // is an nds-e-dialog__overlay whose text neither phrase pattern claims —
      // that is exactly why blockingOverlayNodes() was written — and until it
      // was consulted here, the one modal it exists for was the one that paid
      // the full timeout on every single attempt.
      if (seatTakenDialogVisible() || seatErrorDialogVisible()) return false;
      if (blockingOverlayAnswered()) return false;
    }
    return false;
  }

  // Hand back everything the page is holding, via its own 전체삭제 control.
  function clearSelectedSeats() {
    const button = [...document.querySelectorAll("button,a")].find(
      (el) => (el.textContent || "").replace(/\s+/g, "") === "전체삭제",
    );
    if (!button) return false;
    button.click();
    return true;
  }

  // There is no API shortcut here, and adding one made things worse.
  //
  // Holding a seat over the API is faster in principle — no waiting for the map
  // to draw. In practice the server took the hold while React never learned of
  // it, so the cart stayed empty and the hold had to be handed back before
  // clicking the seat the ordinary way. That release is a network call that can
  // fail silently, and even when it succeeds it races the click that follows:
  // click a seat we are still holding and the server answers 이미 선점된
  // 좌석입니다. The macro was generating its own 이선좌.
  //
  // A rendered circle, clicked once, is the whole strategy.

  async function selectSeats(initData, seats, { dryRun = false, autoAssign = false } = {}) {
    if (dryRun) return { dryRun: true, seats };
    if (!autoAssign) {
      // Map-click only. Background preselect + POST /seats/select can lock the
      // seat on the server while leaving 선택 좌석 empty — then advance presses
      // 선택 완료 and the site answers 좌석 선택 도중 오류가 발생했습니다. A real
      // pointer on a rendered circle is the only path that updates React state.
      const countBefore = selectedSeatCount();
      let clicked = 0;
      for (const seat of seats) {
        if (!clickSeatOnMap(seat.seatInfoId, { countBefore })) continue;
        // The first press is the one the clock stops on. Stamping before the
        // loop would have timed an attempt that found no circle to click at
        // all as though it had pressed one.
        if (!clicked) noteCatchStage("click");
        clicked += 1;
      }

      if (clicked !== seats.length) {
        traceCall(
          "selectSeats",
          seats.map((seat) => seat.seatInfoId),
          { viaSeatMap: true, reason: "not-on-map", clicked, want: seats.length, build: AUTOPILOT_BUILD },
        );
        return {
          unselectableSeatInfoIds: seats.map((seat) => String(seat.seatInfoId)),
          viaSeatMap: true,
          reason: "not-on-map",
        };
      }

      let registered = false;
      if (countBefore >= 0) {
        registered = await pageRegisteredSelection(countBefore, seats.length);
      } else {
        // Sidebar count unreadable — wait until we see a positive cart.
        for (let attempt = 0; attempt < SEAT_MAP_SETTLE_TRIES; attempt += 1) {
          await sleep(settleDelayFor(attempt));
          if (selectedSeatCount() >= seats.length) {
            registered = true;
            break;
          }
        }
      }

      if (registered) {
        noteCatchStage("cart");
        seatState.wonVia = "click";
        seats.forEach((seat) => seatState.heldSeatIds.add(String(seat.seatInfoId)));
        traceCall(
          "selectSeats",
          seats.map((seat) => seat.seatInfoId),
          { viaSeatMap: true, reason: "map-ok", build: AUTOPILOT_BUILD },
        );
        return { viaSeatMap: true, seats };
      }

      // Lost the race rather than been refused. Clear the modal immediately —
      // nothing on the map is clickable while it is up — and say so, because
      // the caller must treat this as "next seat now", not as a fault.
      const lostRace = seatTakenDialogVisible();
      if (lostRace) {
        dismissSeatTakenDialog();
        if (countBefore >= 0 && selectedSeatCount() > countBefore) clearSelectedSeats();
        traceCall(
          "selectSeats",
          seats.map((seat) => seat.seatInfoId),
          { viaSeatMap: true, reason: "taken", build: AUTOPILOT_BUILD },
        );
        return {
          unselectableSeatInfoIds: seats.map((seat) => String(seat.seatInfoId)),
          viaSeatMap: true,
          reason: "taken",
        };
      }

      // Asked and declined. Do not fall through to the API — that is what used
      // to produce false locks and the empty-cart error modal.
      if (countBefore >= 0 && selectedSeatCount() > countBefore) clearSelectedSeats();
      else if (pageHasSelectedSeats()) clearSelectedSeats();
      traceCall(
        "selectSeats",
        seats.map((seat) => seat.seatInfoId),
        { viaSeatMap: true, reason: "map-declined", build: AUTOPILOT_BUILD },
      );
      return {
        unselectableSeatInfoIds: seats.map((seat) => String(seat.seatInfoId)),
        viaSeatMap: true,
        reason: "map-declined",
      };
    }

    // autoAssign only — the page has no individual circles to click.
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const body = {
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
      sessionId: initData.sessionId,
      seatType: resolveSeatType(goods),
      autoAssign: Boolean(autoAssign),
      seats: seats.map((seat) => ({ seatGrade: seat.seatGrade, seatInfoId: seat.seatInfoId })),
    };
    const path = goods?.isInterlocking ? "/onestop/api/seats/select-external" : "/onestop/api/seats/select";
    const response = await fetch(path, {
      method: "POST",
      credentials: "include",
      headers: { ...onestopHeaders(initData), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      traceCall("seats/select", body, `HTTP ${response.status} ${detail}`);
      let parsed = null;
      try {
        parsed = JSON.parse(detail);
      } catch {
        /* not JSON — fall through to the generic error */
      }
      // The preselect above put a hold on these seats. The select just refused
      // them, so that hold is now dead weight counting against the account's
      // ticket allowance — leave enough of them behind and every later attempt
      // fails with 예매 가능 매수를 초과하였습니다, forever.
      await releasePreselected(seats.map((seat) => seat.seatInfoId));

      const blocked = readUnselectable(parsed);
      if (blocked.length) {
        // P40021 "좌석 요청이 잘못 되었습니다" comes back as a 400, but it names the
        // seats the server will not sell. That is an answer, not a failure:
        // reported as one, the caller drops those seats and tries the next
        // ones. Thrown instead, it lands in the catch below, which puts the
        // same dead seats back on the queue and spends every attempt on them.
        return { unselectableSeatInfoIds: blocked, httpStatus: response.status, detail: parsed };
      }
      const error = new Error(
        `select HTTP ${response.status}${detail ? ` · ${detail.slice(0, 160)}` : ""}`,
      );
      error.serverMessage = readServerMessage(parsed);
      throw error;
    }
    const payload = await response.json();
    traceCall("seats/select", body, payload);
    const blocked = readUnselectable(payload);
    if (blocked.length) {
      await releasePreselected(seats.map((seat) => seat.seatInfoId));
      return { ...payload, unselectableSeatInfoIds: blocked };
    }
    seats.forEach((seat) => seatState.heldSeatIds.delete(String(seat.seatInfoId)));
    return payload;
  }

  // Hand a hold back. The seat map has this as bulkDeselectPreSelectSeats; the
  // autopilot never called it, so a lost race used to cost an allowance slot
  // permanently instead of just an attempt.
  /**
   * Hand seats back to the server.
   *
   * Returns whether the account is actually clear of them. Callers used to
   * ignore this and clear seatState.heldSeatIds regardless, which destroyed the
   * only record that a failed BulkDeselectSeats had left seats held — so no
   * later sweep retried, and the seats sat against the account's allowance
   * until the server hold expired. That is the 예매 가능 매수를 초과 failure
   * arriving on a later, unrelated run.
   */
  async function releasePreselected(seatInfoIds) {
    const ids = [...new Set((seatInfoIds || []).map(String))].filter(Boolean);
    // Nothing to release is success, not failure. Returning false here made it
    // impossible for a caller to tell the two apart even if it checked.
    if (!ids.length) return true;
    const query = `mutation BulkDeselectSeats($command: BulkDeselectSeatsCommand!) {
      bulkDeselectSeats(command: $command)
    }`;
    try {
      await gql(query, { command: { seatInfoIds: ids } });
      ids.forEach((id) => seatState.heldSeatIds.delete(id));
      return true;
    } catch (error) {
      // Was console-only, so a leaked allowance had no trace anywhere.
      seatState.releaseFailures = (seatState.releaseFailures || 0) + 1;
      seatState.lastReleaseError = String(error).slice(0, 120);
      log("deselect failed", error);
      return false;
    }
  }

  // The gateway throttles by account and answers FORBIDDEN with a countdown:
  //
  //   {"errorCode":"GATEWAY_ABUSE_BLOCKED","abuseStage":"BLOCKED",
  //    "retryAfterMs":165470,"classification":"FORBIDDEN"}
  //
  // Every call fails while it holds, and preselect failing is what makes the
  // select report 좌석 요청이 잘못 되었습니다 — the seat was never held, so asking
  // for it is genuinely invalid. Retrying through a block can only extend it.
  //
  // This used to be read only out of a GraphQL error envelope, on the one
  // endpoint that carries one. Everything else — seatMeta, seatStatus,
  // block-data, grades, and the queue API — threw a generic HTTP error, so a
  // block there was invisible and the loop kept asking. seatStatus alone is
  // about four requests a second for as long as a watch runs, which makes it
  // the likeliest thing to be throttled and the worst thing to be blind to.
  //
  // A block therefore has to be recognisable in every shape it can arrive in.
  const BLOCK_FALLBACK_MS = 165000;

  function blockFieldsFrom(node) {
    if (!node || typeof node !== "object") return null;
    const code = String(node.errorCode || node.code || "");
    if (code.includes("ABUSE") || node.abuseStage === "BLOCKED") {
      return Math.max(0, Number(node.retryAfterMs) || 0) || BLOCK_FALLBACK_MS;
    }
    return null;
  }

  /**
   * How long we are blocked for, or -1 if we are not.
   *
   * `payload` may be a GraphQL error array, a parsed REST error body, or a
   * bare string answer from the queue API. `status` and `headers` cover the
   * case where the server says only 403/429 with a Retry-After and no body we
   * can read.
   */
  function readGatewayBlock(payload, { status = 0, headers = null } = {}) {
    // The queue API's whole vocabulary is one string; "BL" is 비정상 예매 차단.
    if (payload === "BL") return BLOCK_FALLBACK_MS;

    const nodes = Array.isArray(payload) ? payload : [payload];
    for (const node of nodes) {
      // GraphQL puts it under extensions; REST puts the same fields at the top.
      const found = blockFieldsFrom(node?.extensions) ?? blockFieldsFrom(node);
      if (found !== null) return found;
    }

    if (status === 403 || status === 429) {
      const retryAfter = Number(headers?.get?.("Retry-After"));
      // Retry-After is seconds when it is a number at all.
      return Number.isFinite(retryAfter) && retryAfter > 0
        ? retryAfter * 1000
        : BLOCK_FALLBACK_MS;
    }
    return -1;
  }

  /**
   * Record a block from wherever it came, and stop everything.
   *
   * The endpoint is kept because the one question this session could not answer
   * from the repo was *which* call had been blocked — the queue or the seat
   * path. Next time it will say so.
   */
  function noteGatewayBlock(retryAfterMs, endpoint) {
    // Monotonic, not wall clock. A cooldown recorded while the device clock was
    // set forward used to survive being set back as a lockout hours in the
    // future, and every loop below reads this — so one shifted clock froze
    // 취켓팅 at 접속 차단 중 with nothing actually blocking it.
    const until = nowMs() + retryAfterMs;
    if (until > (seatState.blockedUntil || 0)) {
      seatState.blockedUntil = until;
      seatState.blockedEndpoint = String(endpoint || "");
    }
    traceCall("blocked", endpoint, { retryAfterMs });
    return gatewayBlockError(retryAfterMs, endpoint);
  }

  // How long a block has left, 0 when clear. Read every tick, not only at the
  // start of a run: a block that arrives mid-watch used to be invisible until
  // the next 감시 시작, so 취켓팅 polled straight through a lockout for as long
  // as it was left running — and retrying through one can only extend it.
  function gatewayBlockRemainingMs() {
    return Math.max(0, (seatState.blockedUntil || 0) - nowMs());
  }

  function gatewayBlockError(retryAfterMs, endpoint = "") {
    const seconds = Math.ceil(retryAfterMs / 1000);
    const error = new Error(`GATEWAY_ABUSE_BLOCKED retryAfterMs=${retryAfterMs}`);
    error.gatewayBlockedMs = retryAfterMs;
    error.blockedEndpoint = String(endpoint || "");
    error.serverMessage =
      `접속이 일시 차단되었습니다 (요청이 너무 잦음). ${seconds}초 후에 다시 시도하세요. ` +
      `차단 중에는 어떤 좌석도 잡을 수 없고, 계속 시도하면 차단이 길어집니다.`;
    return error;
  }

  function readServerMessage(payload) {
    let node = payload;
    for (let depth = 0; node && typeof node === "object" && depth < 5; depth += 1) {
      if (typeof node.message === "string" && node.message.trim()) return node.message.trim();
      node = node.data;
    }
    return "";
  }

  // Retrying these never works. The allowance one is recoverable, but only by
  // handing back the holds we are still sitting on — not by trying again.
  const TERMINAL_SELECT_ERRORS = [
    { pattern: /예매\s*가능\s*매수|매수를?\s*초과|초과하였습니다/, kind: "quota" },
    { pattern: /로그인|세션이?\s*만료|인증이?\s*필요/, kind: "auth" },
  ];

  function terminalSelectError(error) {
    if (error?.gatewayBlockedMs >= 0) return "blocked";
    const text = `${error?.serverMessage || ""} ${String(error || "")}`;
    return TERMINAL_SELECT_ERRORS.find((entry) => entry.pattern.test(text))?.kind || "";
  }

  // The field sits at a different depth depending on the endpoint and whether
  // the call succeeded: top level on a 200, but nested under data.data on the
  // 400. Look for it wherever it is rather than guessing one shape.
  function readUnselectable(payload) {
    const found = [];
    let node = payload;
    for (let depth = 0; node && typeof node === "object" && depth < 5; depth += 1) {
      if (Array.isArray(node.unselectableSeatInfoIds)) {
        found.push(...node.unselectableSeatInfoIds.map(String));
      }
      node = node.data;
    }
    return [...new Set(found)];
  }

  // Non-reserved products (입장권, 자유석, 비지정석) have no seat map: the seat
  // step is skipped entirely and only a quantity is chosen on the price step.
  async function runGeneralAdmission(config, initData, { probe = false } = {}) {
    const quantity = Math.max(1, Number(config.quantity) || 1);
    if (probe) {
      seatState.running = false;
      updateOverlay(`비지정석 상품 — 좌석 선택 없음<br>매수 ${quantity}장으로 결제 단계 진행`, "ok");
      return { generalAdmission: true, quantity };
    }
    updateOverlay(`비지정석 상품 — 매수 ${quantity}장 진행`, "info");
    await advanceAfterSeatLock(config);
    seatState.locked = true;
    seatState.running = false;
    notifyDiscord(`NOL Sniper 비지정석 진행 ${quantity}장`);
    return { generalAdmission: true, quantity };
  }

  // Compares the newest availability bitmap against the previous one and
  // returns candidates for seats that flipped to free.
  //
  // Catch mode is long-lived, so it must not hammer the session: one
  // seatStatus call (max 2 blockKeys) per tick, and never a full seatMeta
  // sweep or a page reload.
  // Which blocks the drawn 감시 구역 actually covers.
  //
  // Without this the watch polls every block in the venue and then throws away
  // everything outside the rect — paying a 43-block sweep to look at two. The
  // rect is in the same space as posLeft/posTop, so a block is watched when any
  // of its seats falls inside it.
  function blocksInWatchRect(blocks, rect) {
    if (!rect) return null;
    const keys = [];
    for (const block of blocks || []) {
      const key = String(block.blockKey || "");
      if (!key) continue;
      for (const seat of block.seats || []) {
        if (seatInWatchRect(seat, rect)) {
          keys.push(key);
          break;
        }
      }
    }
    return keys.length ? keys : null;
  }

  // How long the map takes to agree with the bitmap.
  //
  // The whole watch turns on this number and nobody has measured it. We refuse
  // to click a seat the page draws as disabled — correctly, since clicking one
  // raises 좌석 요청이 잘못되었습니다 — so between "the bitmap says free" and "the
  // seat is clickable" there is a wait we neither trigger nor observe. If it is
  // ~50ms the page is fine and there is nothing to fix; if it is seconds, that
  // gap *is* the race we keep losing.
  const domAgreeWatch = new Map();

  function noteBitmapSawFree(seatInfoId) {
    const id = String(seatInfoId);
    if (domAgreeWatch.has(id)) return;
    domAgreeWatch.set(id, performance.now());
  }

  function checkDomAgreement() {
    if (!domAgreeWatch.size) return;
    // One index for the whole sweep. Per-seat lookups each rebuilt their own
    // view of the map when no observer was attached, so watching five seats
    // meant five full walks of the venue per tick — pure instrumentation cost
    // on the loop whose latency is the thing being instrumented.
    const rendered = liveSeatIndex();
    for (const [id, sawAt] of domAgreeWatch) {
      const node = rendered.get(String(id));
      if (!node || node.isConnected === false) continue;
      if (seatNodeDisabled(node)) {
        // Still drawn as taken. Give up on measuring after a while so a seat
        // that never flips does not sit in the map for the whole run.
        if (performance.now() - sawAt > 15000) domAgreeWatch.delete(id);
        continue;
      }
      const waited = Math.round(performance.now() - sawAt);
      seatState.lastDomAgreedMs = waited;
      seatState.domAgreedSamples = (seatState.domAgreedSamples || 0) + 1;
      seatState.domAgreedWorstMs = Math.max(seatState.domAgreedWorstMs || 0, waited);
      traceCall("domAgreed", id, { ms: waited });
      domAgreeWatch.delete(id);
    }
  }

  // How many seatStatus calls to have in the air at once.
  //
  // Six, because that is what the site's own page does: opening a 구역 fires six
  // seatStatus requests inside 13ms — measured from a recorded session. A width
  // the gateway sees from its own client is a defensible one; seventeen at once,
  // which a whole-venue burst would otherwise reach, is a signature nothing has
  // shown to be normal.
  const SWEEP_CONCURRENCY = 6;

  /**
   * Fetch every block's bitmap, several requests at a time, in key order.
   *
   * This awaited each request in turn. With the ordinary one-request budget that
   * costs nothing, but a burst across a 34-block venue became 17 round trips
   * back to back — about 490ms for work the network can do in one.
   *
   * Two things are load-bearing here:
   *
   * - allSettled, not all: one refused block must not lose the other 33.
   * - Alignment. The caller matches masks to keys positionally, so every pair
   *   contributes exactly as many slots as it had keys whether it succeeded or
   *   not. Collapsing the failures instead would shift every later mask onto the
   *   wrong block — silently, and it would read as seats freeing in places they
   *   did not.
   */
  async function fetchMasksFor(initData, batch) {
    const pairs = chunk(batch, 2);
    const masks = [];
    for (let at = 0; at < pairs.length; at += SWEEP_CONCURRENCY) {
      const wave = pairs.slice(at, at + SWEEP_CONCURRENCY);
      const settled = await Promise.allSettled(
        wave.map((pair) => fetchSeatStatus(initData, pair)),
      );
      settled.forEach((result, index) => {
        const parsed =
          result.status === "fulfilled" ? parseSeatStatus(result.value) : [];
        for (let slot = 0; slot < wave[index].length; slot += 1) {
          masks.push(parsed[slot] || null);
        }
      });
    }
    return masks;
  }

  // The watch's tick. A configured speed may only ever slow it down; 0 or
  // absent means "use the floor".
  function catchPollMs(config) {
    const asked = Number(config?.speed_ms || config?.poll_ms || 0);
    return Math.max(CATCH_MIN_POLL_MS, asked > 0 ? asked : CATCH_MIN_POLL_MS);
  }

  /**
   * How long to wait before reading the venue again, given what the last sweep
   * actually cost. See CATCH_FAST_POLL_MS for why this exists.
   *
   * Deliberately narrow. It only ever *shortens*, only when `sweepTicks` is 1 —
   * one tick already covering every watched block, so there is no next slice
   * waiting on this wait — and never when the user asked for a slower speed,
   * because a configured interval is a request, not a starting point.
   */
  function catchIdlePollMs(configuredMs, { requests = 0, sweepTicks = 0, sweepMs = 0 } = {}) {
    if (sweepTicks !== 1 || requests <= 0 || !(sweepMs > 0)) return configuredMs;
    // A user who asked to go slower gets to go slower.
    if (configuredMs > CATCH_MIN_POLL_MS) return configuredMs;
    const rateFloor = Math.ceil((requests * 1000) / CATCH_MAX_REQUESTS_PER_SEC);
    return Math.max(
      CATCH_FAST_POLL_MS,
      rateFloor,
      Math.min(configuredMs, Math.round(sweepMs)),
    );
  }

  // How many requests a quiet tick may spend. With a usable trigger this stays
  // at one and the burst does the work; without one, there is no burst, so the
  // steady rate has to be enough to keep the lap short by itself.
  function steadyRequestsPerTick(requests, pollMs) {
    if (seatState.watchTrigger?.usable) return CATCH_MAX_REQUESTS_PER_TICK;
    const ticksForTarget = Math.max(1, Math.floor(CATCH_TARGET_LAP_MS / Math.max(1, pollMs)));
    const needed = Math.ceil(requests / ticksForTarget);
    return Math.min(CATCH_UNTRIGGERED_REQUESTS_PER_TICK, Math.max(1, needed));
  }

  async function pollFreedSeats(initData, blockKeys, config, { burst = false } = {}) {
    if (!blockKeys.length) return [];
    // Empty *or* partial. A picture built by a 좌석 잡기 that stopped early
    // covers whatever satisfied its quota, and taking that for the venue is
    // how the watch ends up blind to most of the house with nothing to say.
    if (!seatState.lastBlocks?.length || seatState.lastBlocksComplete === false) {
      const collected = [];
      let missed = 0;
      for (const batch of chunk(blockKeys, 2)) {
        if (seatState.stopRequested) break;
        try {
          collected.push(...(await fetchBlockSeats(initData, batch)));
        } catch (error) {
          // One failed batch used to abort the whole build, leaving lastBlocks
          // empty and the tick fruitless; swallowing it would leave the watch
          // permanently short. Keep what arrived, and come back for the rest.
          missed += 1;
          log("block build batch failed", error);
        }
      }
      seatState.lastBlocks = collected;
      seatState.lastBlocksComplete = missed === 0;
      if (missed) seatState.batchFailures = (seatState.batchFailures || 0) + missed;
      seatState.mapCenterX = venueCenterX(collected);
      seatState.mapStage = stagePoint(collected);
      seatState.catchCursor = 0;
      return collectFromBlocks(collected, config);
    }

    const byKey = new Map(seatState.lastBlocks.map((block) => [String(block.blockKey), block]));

    // Watch only the blocks worth watching.
    //
    // This ignored its blockKeys argument and re-derived from every block in
    // the venue, then polled two of them per tick on a rotating cursor — so a
    // 43-block stadium took 22 ticks, nearly nine seconds, to come back round
    // to any given block. A seat freeing just behind the cursor sat unnoticed
    // for a whole sweep, which is how a macro loses a race it is otherwise
    // fast enough to win. The 감시 구역 was applied as a filter *after* fetching,
    // so drawing one narrowed the results but never the work.
    const wanted = new Set((blockKeys || []).map(String));
    // Skip blocks with nothing on sale this round. They cannot produce a
    // cancellation and each one costs a request every sweep — on 26012673 that
    // was 4 blocks of 11, better than a third of every lap spent on seats
    // nobody can buy. A block whose seats we have not fetched yet is kept:
    // unknown is not the same as dead.
    const all = seatState.lastBlocks
      .filter((block) => {
        const seats = block.seats || [];
        return !seats.length || seats.some(seatSellable);
      })
      .map((block) => block.blockKey)
      .filter(Boolean);
    const keys = wanted.size ? all.filter((key) => wanted.has(String(key))) : all;
    if (!keys.length) return [];

    // A fixed request budget. seatStatus takes two blocks per call, and the
    // gateway answers GATEWAY_ABUSE_BLOCKED with a ~165s lockout if pushed —
    // during which nothing can be caught at all. Sweeping a whole stadium
    // faster genuinely costs more requests, so the budget is what is held
    // constant and the sweep time follows from how much is being watched.
    // A burst spends the whole sweep at once. That is affordable precisely
    // because the trigger means we spent almost nothing while the venue was
    // quiet — the budget is an average, and this is where it gets spent.
    const requests = Math.ceil(keys.length / 2);
    const perTick = burst
      ? requests
      : Math.min(steadyRequestsPerTick(requests, catchPollMs(config)), requests);
    const take = perTick * 2;
    const cursor = keys.length <= take ? 0 : seatState.catchCursor % keys.length;
    const batch = keys.length <= take ? keys : keys.slice(cursor, cursor + take);
    seatState.catchCursor = keys.length <= take ? 0 : (cursor + batch.length) % keys.length;
    seatState.catchSweepTicks = Math.max(1, Math.ceil(keys.length / take));
    seatState.catchWatchedBlocks = keys.length;
    // Which blocks hold a mask we have actually refreshed. Scoping stopped
    // polling the rest, and their masks are frozen at whatever they were when
    // last fetched — counting those as "free" is how 빈 좌석 N석 came to include
    // seats taken minutes ago, which the watch then chased until its retry
    // budget ran out.
    seatState.polledBlocks = new Set(keys.map(String));

    const freed = [];
    const masks = await fetchMasksFor(initData, batch);
    batch.forEach((key, index) => {
      freed.push(...applyBlockMask(byKey.get(String(key)), masks[index] || null, config));
    });
    if (freed.length) log("catch: freed seats", freed.map((seat) => seat.label));
    return freed;
  }

  /**
   * Fold one block's fresh availability bitmap in, and report what just opened.
   *
   * Split out of pollFreedSeats so the page's own seatStatus traffic can go
   * through exactly the same path. A seat that frees is a seat that frees; it
   * must not be recognised differently depending on who asked.
   */
  function applyBlockMask(block, mask, config) {
    if (!block || !mask) return [];
    const previous = block.mask;
    block.mask = mask;
    // Nothing to compare against yet: a first sighting is not an opening.
    if (!previous) return [];
    // Nor is the first reading of this run. The mask we are holding was left by
    // the previous run, and everything that freed in between would otherwise
    // arrive at once as a burst of openings that are long gone.
    const key = String(block.blockKey);
    if (seatState.runBaseline && !seatState.runBaseline.has(key)) {
      seatState.runBaseline.add(key);
      return [];
    }
    const freed = [];
    const rect = normalizeWatchRect(config.watch_rect);
    for (let pos = 0; pos < Math.min(previous.length, mask.length); pos += 1) {
      if (!mask[pos] || previous[pos]) continue;
      const seat = block.seats[pos];
      if (!seat?.isExposable || !seat?.seatGrade || !seat?.seatInfoId) continue;
      // The freed path had no filtering at all, so a seat we were already
      // holding walked straight back in while both collectors correctly skipped
      // it. It deliberately does *not* consult the lost-race cooldown: a 0->1
      // transition is direct evidence the seat is free now, and someone
      // abandoning a cart inside those 30s is the exact thing this watches for.
      if (seatUnreachableNow(seat.seatInfoId)) continue;
      if (seatHeldByUs(seat.seatInfoId)) continue;
      if (seat.seatGroupId && config.allow_group_seats === false) continue;
      const candidate = toCandidate(seat, block.blockKey);
      if (!seatInWatchRect(candidate, rect)) continue;
      // When this seat actually opened, as opposed to when the loop got round
      // to looking at it. The page's own traffic is folded in through here too,
      // from a network callback that can land a whole tick before the loop
      // wakes — timing that gap from the loop would hide it entirely.
      candidate.freedAtPerf = performance.now();
      noteBitmapSawFree(candidate.seatInfoId);
      freed.push(candidate);
    }
    return freed;
  }

  /**
   * Read the page's own seatStatus responses.
   *
   * The 예매 창 fetches availability for its own drawing. Every one of those is
   * an observation we were throwing away while paying for our own — and it
   * arrives without costing a request, so it cannot contribute to the gateway
   * lockout that caps how fast we are allowed to poll.
   *
   * Whether it fires at all, and how often, is a property of the site rather
   * than something to assume: `pageStatusSeen` counts them so a single watch
   * settles it.
   */
  function notePageSeatStatus(url, text) {
    if (!seatState.lastBlocks?.length) return;
    let keys;
    try {
      keys = new URL(url, location.origin).searchParams.getAll("blockKeys");
    } catch (error) {
      return;
    }
    if (!keys.length) return;
    let masks;
    try {
      masks = parseSeatStatus(JSON.parse(text));
    } catch (error) {
      return;
    }
    if (!masks?.length) return;

    seatState.pageStatusSeen = (seatState.pageStatusSeen || 0) + 1;
    const byKey = new Map(seatState.lastBlocks.map((block) => [String(block.blockKey), block]));
    const config = loadSeatConfig();
    const freed = [];
    keys.forEach((key, index) => {
      freed.push(...applyBlockMask(byKey.get(String(key)), masks[index] || null, config));
    });
    if (!freed.length) return;
    // Handed to the loop rather than acted on here: this runs inside a network
    // callback, and clicking from there would race whatever the loop is doing.
    seatState.pageFreed.push(...freed);
    seatState.pageStatusFreed = (seatState.pageStatusFreed || 0) + freed.length;
    log("catch: page traffic showed freed seats", freed.map((seat) => seat.label));
  }

  // What the panel needs, and nothing else. Spreading seatState sent the whole
  // decoded seat map — hundreds of seat objects plus their masks — across the
  // bridge every 400ms, to be JSON-encoded, diffed and written to disk each
  // time, while a seat race was in progress.
  async function runDiagnose() {
    const report = { at: new Date().toISOString().slice(11, 23) };
    const initData = getInitData();
    report.session = initData?.sessionId ? "ok" : "missing";
    if (!initData?.sessionId) {
      traceCall("diagnose", null, report);
      return report;
    }

    const config = loadSeatConfig();
    const circles = collectSeatCircles();
    report.circles = circles.length;

    // How the rendered map classifies seats, which is the question behind
    // "is seatNodeDisabled over-matching every seat".
    const classes = new Map();
    for (const node of circles) {
      const key = String(node.getAttribute("class") || "(none)");
      classes.set(key, (classes.get(key) || 0) + 1);
    }
    report.classes = [...classes.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([cls, count]) => ({ cls: cls.slice(0, 90), count, disabledByOurTest: /disabled|sold|unavailable/i.test(cls) }));

    let candidates = [];
    try {
      candidates = await collectApiCandidates(
        initData,
        (config.grade_order || []).map(String),
        (config.block_keys || []).map(String),
        config,
      );
    } catch (error) {
      report.candidateError = String(error).slice(0, 160);
    }
    const ranked = rankCandidates(
      candidates,
      (config.grade_order || []).map(String),
      (config.block_keys || []).map(String),
      pickerOptions(config),
    );
    report.freeByBitmap = ranked.length;

    // Cross-check the bitmap against the rendered class for the same seats:
    // if a seat the API says is free carries the same class as one it says
    // is sold, the class cannot be what decides selectability.
    report.sample = ranked.slice(0, 3).map((seat) => {
      const node = seatNodeFor(seat.seatInfoId);
      return {
        id: seat.seatInfoId,
        label: seat.label,
        rendered: node ? String(node.getAttribute("class") || "").slice(0, 90) : "(not rendered)",
        wouldSkip: node ? seatNodeDisabled(node) : null,
      };
    });

    const target = ranked[0];
    if (!target) {
      report.result = "no-free-seat";
      traceCall("diagnose", null, report);
      return report;
    }

    report.target = target.label;
    report.before = selectedSeatCount();
    report.clicked = clickSeatOnMap(target.seatInfoId);
    await sleep(1500);
    report.after = selectedSeatCount();
    report.result =
      report.after > report.before ? "SELECTED" : report.clicked ? "clicked-but-no-change" : "not-clicked";
    traceCall("diagnose", target.seatInfoId, report);
    updateOverlay(`진단: ${report.result}<br>${report.target}`, report.after > report.before ? "ok" : "warn");
    return report;
  }

  // ---- The soft-hold spike -------------------------------------------------
  //
  // One question, answered on a live map instead of argued about:
  //
  //   Does a bare GraphQL preselectSeat reach the page's own cart?
  //
  // It matters because the server does not refuse the mutation — it answers
  // true — and it is tempting to conclude the hold is therefore usable. It is
  // not the same claim. 선택 완료 is refused unless the SPA's own state says a
  // seat is selected, and that state is set by the page's handler when *its*
  // request resolves. A hold the page never learned about is the empty-cart
  // failure: 좌석 선택 도중 오류 / P40021, with the seat locked on the server
  // and counting against the allowance.
  //
  // So this holds one seat, watches for every kind of proof the page could
  // give, and hands it straight back. It never presses 선택 완료 — the whole
  // point is to find out whether pressing it would be safe, and a probe that
  // takes that risk to answer the question has answered nothing.
  //
  // It also captures, in the same pass, how the circle is wired to React: the
  // handler's own source and the fiber props around it name the store the page
  // uses. If the cart never moves, that is the next place to look, and getting
  // it here costs no second round trip on a live show.
  const SOFT_HOLD_PROBE_WATCH_MS = 2500;
  const SOFT_HOLD_PROBE_SAMPLE_MS = 25;

  /**
   * How the seat circle is bound to React, by name only.
   *
   * Handler sources are truncated and no prop *values* are read beyond the
   * seat's own identifiers — this goes into a report the user may paste, and a
   * fiber carries session material.
   */
  function describeSeatBinding(node) {
    if (!node) return null;
    const fiberKey = Object.keys(node).find(
      (key) => key.startsWith("__reactFiber") || key.startsWith("__reactInternalInstance"),
    );
    if (!fiberKey) return { found: false, why: "no react fiber on the circle" };
    const chain = [];
    let fiber = node[fiberKey];
    for (let depth = 0; depth < 8 && fiber; depth += 1) {
      const props = fiber.memoizedProps || fiber.pendingProps || {};
      const handlers = Object.keys(props).filter(
        (key) => /^on[A-Z]/.test(key) && typeof props[key] === "function",
      );
      chain.push({
        depth,
        name:
          typeof fiber.type === "string"
            ? fiber.type
            : fiber.type?.displayName || fiber.type?.name || "(anonymous)",
        props: Object.keys(props).slice(0, 24),
        handlers,
        // Minified, but it names the action or store the press dispatches into,
        // which is the whole reason to look.
        source: handlers.length
          ? String(props[handlers[0]]).replace(/\s+/g, " ").slice(0, 300)
          : null,
      });
      fiber = fiber.return;
    }
    return { found: true, chain };
  }

  async function probeSoftHold() {
    const report = {
      at: new Date().toISOString().slice(11, 23),
      build: AUTOPILOT_BUILD,
      verdict: "",
      cartUpdated: null,
    };

    // Refusals, in the order they would bite.
    if (!isSeatPage()) {
      report.verdict = "좌석맵이 아니어서 확인할 수 없습니다.";
      return report;
    }
    const blockedFor = gatewayBlockRemainingMs();
    if (blockedFor > 0) {
      // Never probe through a block: the whole cost of one is that nothing can
      // be caught while it lasts, and another request can extend it.
      report.verdict = `접속 차단 중 — ${Math.ceil(blockedFor / 1000)}초 후에 다시 시도하세요.`;
      return report;
    }
    if (seatState.running || seatState.locked || seatState.confirmStarted) {
      report.verdict = "감시가 도는 중에는 확인할 수 없습니다. [전부 정지] 후 다시 눌러 주세요.";
      return report;
    }
    const initData = getInitData();
    if (!initData?.sessionId || !initData?.goods || !initData?.playSeq) {
      report.verdict = "예매 세션이 없습니다. [예매하기]로 좌석맵에 다시 들어오세요.";
      return report;
    }
    const cartBefore = selectedSeatCount();
    if (cartBefore > 0) {
      // A cart that already holds something cannot prove a rise, and clearing
      // it for a probe would throw away a seat the user may be holding.
      report.verdict = `이미 ${cartBefore}석이 선택돼 있습니다. [전체삭제] 후 다시 눌러 주세요.`;
      return report;
    }
    report.cartReadable = cartBefore >= 0;

    // A rendered seat, so the circle's own isSelected prop can be watched as a
    // second, finer proof than the sidebar number.
    const config = loadSeatConfig();
    const dom = collectDomCandidates([], [], { ...config, watch_rect: null });
    const target = clickableAmong(dom)[0] || dom[0] || null;
    if (!target) {
      report.verdict =
        "화면에 잡을 수 있는 좌석이 없습니다. 구역을 열고 좌석이 보이는 상태에서 다시 눌러 주세요.";
      report.domCircles = seatState.domCircleCount || 0;
      return report;
    }
    const node = seatNodeFor(target.seatInfoId);
    report.seat = { label: target.label, blockKey: target.blockKey, rendered: Boolean(node) };
    report.binding = describeSeatBinding(node);

    updateOverlay(`가선점 확인 중 — ${target.label}<br>선택 완료는 누르지 않습니다`, "info");

    // --- the one mutation ---------------------------------------------------
    const startedPerf = performance.now();
    let answered = null;
    try {
      // Singular, deliberately: measured, the bulk mutation answers P40021 for
      // a single seat while this one answers true for the same seat in the
      // same session.
      answered = await preselectSeat(initData, target);
      report.preselect = { ok: answered?.preselectSeat === true, raw: answered ?? null };
    } catch (error) {
      if (error?.gatewayBlockedMs >= 0) {
        report.verdict = "게이트웨이 차단 — 즉시 중단했습니다. 다시 시도하지 마세요.";
        report.preselect = { ok: false, blocked: true, error: String(error).slice(0, 200) };
        return report;
      }
      report.preselect = { ok: false, error: String(error).slice(0, 200) };
    }
    report.preselectMs = Math.round(performance.now() - startedPerf);

    // --- did the page notice? ----------------------------------------------
    //
    // Everything from here is inside a finally that hands the seat back. The
    // request has already been sent, so from this point a throw anywhere —
    // a sidebar read, a fiber walk — would otherwise leave a seat held on the
    // server, counting against the account's allowance until it expires. That
    // arrives later as 예매 가능 매수를 초과하였습니다 on an unrelated run, which
    // is a miserable thing to have to trace back to a probe.
    try {
      const samples = [];
      let cartRoseAt = null;
      let selectedRoseAt = null;
      const deadline = performance.now() + SOFT_HOLD_PROBE_WATCH_MS;
      while (performance.now() < deadline) {
        const cartNow = selectedSeatCount();
        const rendered = node ? seatRenderProps(node) : null;
        const isSelected = rendered ? Boolean(rendered.isSelected) : null;
        const since = Math.round(performance.now() - startedPerf);
        samples.push({ ms: since, cart: cartNow, isSelected });
        if (cartRoseAt === null && cartNow > Math.max(cartBefore, 0)) cartRoseAt = since;
        if (selectedRoseAt === null && isSelected === true) selectedRoseAt = since;
        // A dialog is an answer too, and a loud one.
        if (seatErrorDialogVisible() || seatTakenDialogVisible()) {
          report.dialog = unknownBlockingDialogText() || "좌석 관련 안내창";
          break;
        }
        if (cartRoseAt !== null && selectedRoseAt !== null) break;
        await sleep(SOFT_HOLD_PROBE_SAMPLE_MS);
      }
      // Thinned: the shape is the evidence, 100 rows of it is not.
      report.samples = samples.filter((row, at) => at % 4 === 0 || row.cart > 0 || row.isSelected);
      report.cartRoseAtMs = cartRoseAt;
      report.seatMarkedSelectedAtMs = selectedRoseAt;
      report.cartUpdated = cartRoseAt !== null;
      report.cartAfter = selectedSeatCount();
    } finally {
      report.released = await releasePreselected([target.seatInfoId]);
      try {
        if (selectedSeatCount() > Math.max(cartBefore, 0)) {
          report.clearedCart = clearSelectedSeats();
        }
        dismissSeatErrorDialog();
        dismissSeatTakenDialog();
      } catch (error) {
        report.cleanupError = String(error).slice(0, 160);
      }
    }

    report.verdict = !report.preselect?.ok
      ? "가선점 자체가 거절됐습니다 — API 경로는 이 세션에서 쓸 수 없습니다."
      : report.cartUpdated
        ? "가선점이 예매 창 장바구니에 반영됐습니다 — API 경로를 쓸 수 있습니다."
        : "가선점은 성공했지만 예매 창은 모릅니다 — 이 상태로 [선택 완료]를 누르면 P40021입니다.";
    updateOverlay(
      `가선점 확인 완료<br>${report.cartUpdated ? "장바구니 반영됨" : "장바구니 변화 없음"}` +
        `<br>좌석은 반납했습니다`,
      report.cartUpdated ? "ok" : "warn",
    );
    traceCall("probeSoftHold", target.seatInfoId, {
      preselectOk: report.preselect?.ok,
      preselectMs: report.preselectMs,
      cartUpdated: report.cartUpdated,
      cartRoseAtMs: cartRoseAt,
      seatMarkedSelectedAtMs: selectedRoseAt,
      released: report.released,
    });
    seatState.lastSoftHoldProbe = report;
    return report;
  }

  // ---- Can we do entry over the API from here? -----------------------------
  //
  // The queue endpoint sends CORS headers only to https://tickets.interpark.com
  // (measured — see waitingApiUsableHere). From the NOL product page the burst
  // is impossible, so entry falls back to clicking 예매하기, and before a show
  // opens that button does not exist. Hence the folk remedy of moving the
  // machine's clock forward until it appears, which invalidates the session's
  // own time-checked tokens and logs you out.
  //
  // There is a tickets.interpark.com page for the same show, and it answers
  // 200. If the 예매 창 can sit on it without being bounced back to NOL, and if
  // the login session reaches it, then the existing burst works from there with
  // no clock trickery and no button at all.
  //
  // Those are two facts about a live session, not something to reason out. This
  // spends exactly one /waiting request to settle both.
  async function probeQueueOrigin() {
    const report = {
      at: new Date().toISOString().slice(11, 23),
      build: AUTOPILOT_BUILD,
      href: String(location.href).slice(0, 200),
      origin: location.origin,
      onAllowedOrigin: waitingApiUsableHere(),
    };

    const blockedFor = gatewayBlockRemainingMs();
    if (blockedFor > 0) {
      report.verdict = `접속 차단 중 — ${Math.ceil(blockedFor / 1000)}초 후에 다시 시도하세요.`;
      seatState.lastQueueOriginProbe = report;
      return report;
    }
    if (!report.onAllowedOrigin) {
      // Bounced. The parking page redirected us somewhere the burst cannot run.
      report.verdict =
        `이 창은 ${location.origin}에 있습니다 — 대기열 API를 읽을 수 없는 출처입니다.`;
      seatState.lastQueueOriginProbe = report;
      return report;
    }

    const arm = loadArmConfig();
    report.armPresent = Boolean(arm?.goods_code);
    if (!arm?.goods_code || !arm?.play_date) {
      report.verdict = "공연 정보가 이 출처에 아직 없습니다 — 조작판에서 다시 보내 주세요.";
      seatState.lastQueueOriginProbe = report;
      return report;
    }
    report.asked = { goods: arm.goods_code, playDate: arm.play_date, playSeq: arm.play_seq };

    // Exactly one. This is a live endpoint that answers abuse with a ~165s
    // lockout, and one answer is all the question needs.
    const startedPerf = performance.now();
    try {
      const answer = await fetchWaitingUrl(arm);
      report.ms = Math.round(performance.now() - startedPerf);
      report.answer = describeWaitingAnswer(answer);
      report.readable = true;
      report.verdict =
        answer === "NP"
          ? "출처는 통과 — 다만 선예매 인증이 필요합니다 (NP)."
          : answer === "BL"
            ? "차단 상태입니다 (BL) — 중단하세요."
            : "대기열 API를 읽을 수 있습니다 — 이 페이지에서 API 진입이 가능합니다.";
    } catch (error) {
      report.ms = Math.round(performance.now() - startedPerf);
      report.readable = false;
      report.error = String(error).slice(0, 200);
      // The two failures mean opposite things, and telling them apart is most
      // of the value of asking at all.
      report.unreachable = isUnreachableError(error);
      report.verdict = report.unreachable
        ? "출처는 맞지만 요청 자체가 막혔습니다 — API 진입은 불가능합니다."
        : /401|로그인|Unauthorized/i.test(report.error)
          ? "출처는 통과 — 다만 이 페이지에 로그인 세션이 없습니다."
          : "대기열 API가 오류를 돌려줬습니다 — 아래 내용을 확인하세요.";
    }
    traceCall("probeQueueOrigin", report.origin, {
      readable: report.readable,
      answer: report.answer,
      error: report.error,
    });
    seatState.lastQueueOriginProbe = report;
    return report;
  }

  /**
   * What the entry would do, right now, without doing any of it.
   *
   * The one thing this app could never answer before an open was "is it going
   * to work?". 테스트 실행 enters for real, so on a show that has not opened the
   * only way to rehearse was to shift the device clock forward — which broke
   * the server clock and 취켓팅 and taught nothing about the button anyway,
   * because a forward-shifted clock does not make NOL's backend open.
   *
   * So this reports instead of acting. It never clicks, never navigates, never
   * enters, and is safe to run on a show that opens next week.
   */
  async function probeEntry() {
    const arm = loadArmConfig();
    const target = arm ? armTargetUnix(arm) : NaN;
    const button = findBookButton();
    const report = {
      at: new Date().toISOString().slice(11, 23),
      build: AUTOPILOT_BUILD,
      href: String(location.href).slice(0, 200),
      origin: location.origin,
      page: isNolProductPage()
        ? "nol"
        : isGoodsPage()
          ? "goods"
          : isSeatPage()
            ? "seat"
            : isGatesPage()
              ? "gates"
              : isWaitingPage()
                ? "waiting"
                : "other",
      // Which route the fire would take from here. This is the whole question:
      // the queue API and the 예매하기 button are different races with
      // different failure modes, and which one runs is decided by the origin.
      route: waitingApiUsableHere() ? "waiting-api" : "dom-click",
      button: {
        found: Boolean(button),
        visible: Boolean(button) && isVisible(button),
        pressable: bookButtonPressable(button),
        text: button ? (button.value || button.textContent || "").trim().slice(0, 40) : "",
        testId: button ? String(button.getAttribute("data-testid") || "") : "",
      },
      clock: {
        quality: clockState.quality,
        offsetMs: Math.round((clockState.offsetSeconds || 0) * 1000),
        jumpMs: Math.round(clockJumpSeconds() * 1000),
        note: clockState.note,
      },
      arm: {
        present: Boolean(arm?.goods_code),
        goodsCode: String(arm?.goods_code || ""),
        playDate: String(arm?.play_date || ""),
        playSeq: String(arm?.play_seq || ""),
        entryOffsetMs: Number(arm?.entry_offset_ms) || 0,
        targetServerUnix: Number(arm?.target_server_unix) || 0,
        fireAtServerUnix: Number.isFinite(target) ? target : 0,
        secondsAway: Number.isFinite(target) ? Math.round(target - serverTimeUnix()) : null,
      },
      blockedMs: gatewayBlockRemainingMs(),
    };

    // One request, and only where it can succeed. Asking from an origin the
    // endpoint refuses proves nothing we do not already know, and this endpoint
    // answers repetition with a ~165s lockout.
    if (waitingApiUsableHere() && arm?.goods_code && arm?.play_date && !report.blockedMs) {
      const startedPerf = performance.now();
      try {
        const answer = await fetchWaitingUrl(arm);
        report.queue = {
          readable: true,
          ms: Math.round(performance.now() - startedPerf),
          answer: describeWaitingAnswer(answer),
        };
      } catch (error) {
        report.queue = {
          readable: false,
          ms: Math.round(performance.now() - startedPerf),
          error: String(error).slice(0, 160),
          unreachable: isUnreachableError(error),
        };
      }
    }

    traceCall("probeEntry", report.route, {
      page: report.page,
      buttonFound: report.button.found,
      pressable: report.button.pressable,
    });
    seatState.lastEntryProbe = report;
    updateOverlay(
      `진입 점검 · ${report.route === "waiting-api" ? "대기열 API" : "예매하기 클릭"}<br>` +
        (report.button.found
          ? report.button.pressable
            ? "예매하기 버튼 활성"
            : "예매하기 버튼 비활성 (오픈 전이면 정상)"
          : "예매하기 버튼 없음"),
      report.button.found ? "info" : "warn",
    );
    return report;
  }

  function seatStatusSummary() {
    return {
      seat: {
        mode: currentMode(),
        runMode: seatState.runMode || "",
        scheduleTrace: seatState.scheduleTrace || (() => { try { return JSON.parse(sessionStorage.getItem("nolsniper_schedule_trace") || "null"); } catch (error) { return null; } })(),
        quietWatch: Boolean(seatState.quietWatch),
        catchFocusBlock: seatState.catchFocusBlock || "",
        fastClicks: seatState.fastClicks || 0,
        focusTicks: seatState.focusTicks || 0,
        focusPollerHz: focusPoller.active ? focusPollerHz() : 0,
        focusPollerInFlight: focusPoller.inFlight,
        focusTickWorkMs: seatState.focusTickWorkMs || 0,
        focusPollerSeq: focusPoller.seq,
        lastCatchLatency: seatState.lastCatchLatency || null,
        lastSeatPos: seatState.lastSeatPos || null,
        catchTimings: (seatState.catchTimings || []).slice(-3),
        mapCenterX: seatState.mapCenterX ?? null,
        mapStage: seatState.mapStage || null,
        lastOrder: seatState.lastOrder || null,
        lastBlocksN: (seatState.lastBlocks || []).length,
        startupTiming: seatState.startupTiming || null,
        enterNow: armState.enterNow || null,
        running: seatState.running,
        locked: seatState.locked,
        attempts: seatState.attempts,
        lastError: seatState.lastError,
        lastSeat: seatState.lastSeat,
        seatOrder: seatState.lastOrder || [],
        stagePoint: seatState.lastStagePoint || null,
        message: seatState.message,
        awaitingPayment: seatState.awaitingPayment,
        stopRequested: seatState.stopRequested,
        haltedByUser: seatState.haltedByUser,
        skippedByMap: seatState.skippedByMap || 0,
        seatErrorDialogs: seatState.seatErrorDialogs || 0,
        lastExit: seatState.lastExit || "",
        pageSelected: selectedSeatCount(),
        consecutiveRejects: seatState.consecutiveRejects || 0,
        captcha: { ...captchaReport },
        blockedForMs: gatewayBlockRemainingMs(),
        blockedEndpoint: seatState.blockedEndpoint || "",
        trace: trace.slice(-TRACE_LIMIT),
        // Which build the page is actually running. Without it there is no way
        // to tell a reload that silently failed from a command that did nothing.
        build: AUTOPILOT_BUILD,
        traceLen: trace.length,
        clickableNow: seatState.clickableNow || 0,
        statusFailures: seatState.statusFailures || 0,
        // Requests that were dropped rather than retried. A venue that looks
        // smaller than it is has to say so.
        batchFailures: seatState.batchFailures || 0,
        blocksComplete: seatState.lastBlocksComplete !== false,
        watchRectIgnored: Boolean(seatState.watchRectIgnored),
        // Racing other buyers is normal and should read as normal. Without
        // these a busy open looks identical to a stuck macro.
        takenConflicts: seatState.takenConflicts || 0,
        // Declines that were not a lost race. Incremented since the fix that
        // made a decline survive its tick, but published only now — the panel
        // line that reads it would otherwise never have fired, which is the
        // same computed-but-invisible failure this app keeps repeating.
        unreachableSkips: seatState.unreachableSkips || 0,
        // A union, not a sum: a seat can sit in both maps and this is a count
        // of seats the user reads, not of entries.
        releaseFailures: seatState.releaseFailures || 0,
        cooldownSeats: new Set([...seatState.takenUntil.keys(),
                                ...seatState.unreachableUntil.keys()]).size,
        aimMisses: seatState.aimMisses || 0,
        blockEntered: seatState.blockEntered || "",
        blockEntryMisses: seatState.blockEntryMisses || 0,
        blockEntryHypothesis: seatState.blockEntryHypothesis || "",
        unknownDialog: seatState.unknownDialog || "",
        overlaysDismissed: seatState.overlaysDismissed || 0,
        catchLiveTries: seatState.catchLiveTries || 0,
        // Whether overhearing the page is worth anything is a fact about the
        // site, not something to assume: these settle it in one watch.
        pageStatusSeen: seatState.pageStatusSeen || 0,
        pageStatusFreed: seatState.pageStatusFreed || 0,
        lastFreedVia: seatState.lastFreedVia || "",
        watchedBlocks: seatState.catchWatchedBlocks || 0,
        catchLatencyMs: seatState.lastCatchLatencyMs || 0,
        // detect -> click -> cart -> 선택 완료, the four segments the race is
        // decided in, as medians over this sitting.
        catchTiming: catchTimingSummary(),
        catchTimingLine: catchTimingLine(),
        // The last answer to "does a bare API hold reach the cart?", so the
        // spike's result survives in the state file instead of a screenshot.
        softHoldProbe: seatState.lastSoftHoldProbe || null,
        // Whether entry can be done over the API from wherever the 예매 창 is.
        queueOriginProbe: seatState.lastQueueOriginProbe || null,
        // What the entry *would* do, asked without doing it — 진입 점검.
        entryProbe: seatState.lastEntryProbe || null,
        softHoldWaitMs: seatState.lastSoftHoldWaitMs ?? null,
        // Where the watch is standing, and how often it had to go back.
        parkedBlock: seatState.parkedBlock || "",
        reparks: seatState.reparks || 0,
        domAgreedMs: seatState.lastDomAgreedMs || 0,
        domAgreedWorstMs: seatState.domAgreedWorstMs || 0,
        domAgreedSamples: seatState.domAgreedSamples || 0,
        wonVia: seatState.wonVia || "",
        sweepTicks: seatState.catchSweepTicks || 0,
        observedTickMs: seatState.observedTickMs || 0,
        domScans: seatState.domScans || 0,
        domScanMs: Math.round(seatState.domScanMs || 0),
        domScanWorstMs: Math.round(seatState.domScanWorstMs || 0),
        mapMoves: seatState.mapMoves || {},
        triggerUsable: seatState.watchTrigger?.usable === true,
        triggerNote: String(seatState.watchTrigger?.note || ""),
        triggerBursts: seatState.triggerBursts || 0,
        heldSeats: seatState.heldSeatIds.size,
        freeSeats: freeSeatCount(),
        freeByGrade: freeSeatsByGrade(),
        blocks: (seatState.lastBlocks || []).length,
        domCircleCount: seatState.domCircleCount || 0,
        // Only ever set by 미리보기, and already trimmed to a preview.
        lastProbe: seatState.lastProbe,
      },
      arm: { ...armState },
      clock: { ...clockState },
    };
  }

  // Cheap identity for "which seats are currently free". Only used to notice
  // that the map moved, so the first and last ids plus the count are enough.
  /**
   * Identity of the current free-seat pool, for the catchLiveTries brake.
   *
   * Length with the two end ids could not see a pool that swapped a seat in the
   * middle — same length, same ends, different seats. The brake resets only
   * when this string changes, so a pool that was genuinely moving read as
   * static and the watch stayed switched off in front of it.
   *
   * Order-independent: the same seats ranked differently are the same pool, and
   * re-running the attempts because the ranking shifted would be noise.
   */
  function liveSignature(live) {
    if (!live.length) return "0";
    let digest = 0;
    for (const seat of live) {
      const id = String(seat.seatInfoId);
      for (let at = 0; at < id.length; at += 1) {
        digest = (digest + id.charCodeAt(at) * (at + 1)) % 2147483647;
      }
    }
    return `${live.length}:${digest}`;
  }

  /**
   * Free seats per grade, counted from the bitmap we already hold.
   *
   * The site does not always publish these. Measured on 26012217: its own
   * /onestop/api/seats/grades answered remainCount 0 for every grade with
   * "isVisibleSeatCount": false — a truthful "we do not say" that the panel
   * rendered as "0석" while 710 seats were in fact free and the macro was busy
   * booking one of them.
   *
   * seatMeta gives each seat its grade name and seatStatus gives the free bits.
   * Both are already fetched and decoded, so this needs no extra request and is
   * right whatever the server chooses to publish.
   */
  function freeSeatsByGrade() {
    const blocks = seatState.polledBlocks?.size
      ? (seatState.lastBlocks || []).filter((block) =>
          seatState.polledBlocks.has(String(block.blockKey)),
        )
      : seatState.lastBlocks || [];
    const counts = {};
    for (const block of blocks) {
      const mask = block.mask;
      if (!mask) continue;
      const seats = block.seats || [];
      for (let pos = 0; pos < Math.min(mask.length, seats.length); pos += 1) {
        if (!mask[pos]) continue;
        const seat = seats[pos];
        if (!seat?.isExposable) continue;
        const name = String(seat.seatGradeName || seat.seatGrade || "").trim();
        if (!name) continue;
        counts[name] = (counts[name] || 0) + 1;
      }
    }
    return counts;
  }

  // What collectFromBlocks requires before a seat is even a candidate. Kept in
  // one place because every count that disagreed with it became a number on the
  // panel that no run could ever act on.
  function seatSellable(seat) {
    return Boolean(seat?.isExposable && seat?.seatGrade && seat?.seatInfoId);
  }

  function freeSeatCount() {
    const blocks = seatState.polledBlocks?.size
      ? (seatState.lastBlocks || []).filter((block) =>
          seatState.polledBlocks.has(String(block.blockKey)),
        )
      : seatState.lastBlocks || [];
    // Only seats that are actually on sale. Counting raw mask bits included
    // whole blocks that are not selling this round — 26012673 has 622 such
    // seats across 1F/2F D and E — so the panel reported hundreds of "빈 좌석"
    // that no run could ever take, and then explained the contradiction with a
    // grade filter that does not exist.
    return blocks.reduce((total, block) => {
      const seats = block.seats || [];
      let free = 0;
      for (let index = 0; index < seats.length; index += 1) {
        if (seatSellable(seats[index]) && seatIsFree(block, index)) free += 1;
      }
      return total + free;
    }, 0);
  }

  // Why nothing is happening, in words. The old text reported a block cursor
  // that is always 0 on a two-block venue and said nothing about whether any
  // seat was even a candidate.
  function catchStatusText(live, free, pollMs, liveExhausted, watchRect = null) {
    const lines = [`취소표 감시 중 · ${pollMs}ms 간격`];
    if (!free) {
      lines.push("빈 좌석 0석 — 취소표가 나오면 즉시 잡습니다");
    } else if (!live.length) {
      // This used to read "내 조건에 맞는 등급이 없음" and tell you to widen a
      // grade selection. There is no grade selection: the panel sends an empty
      // grade_order and rankGrade drops nothing, so the message named a filter
      // that had been removed and pointed at a control that does not exist.
      // The reasons a free seat is not a candidate are these.
      const cooling = seatState.takenUntil.size;
      if (watchRect) {
        lines.push(`빈 좌석 ${free}석 · 모두 <b>감시 구역 밖</b>`);
        lines.push("[범위 정하기]에서 넓게 다시 그어 보세요");
      } else if (cooling) {
        lines.push(`빈 좌석 ${free}석 · 방금 남이 가져간 자리 ${cooling}석`);
        lines.push("잠시 뒤 다시 시도합니다");
      } else {
        lines.push(`빈 좌석 ${free}석 · 아직 잡을 수 있는 자리가 아닙니다`);
      }
    } else if (liveExhausted) {
      lines.push(`후보 ${live.length}석 · ${CATCH_LIVE_TRIES}회 모두 남이 먼저 가져감`);
      lines.push("좌석이 바뀌면 자동으로 다시 시도합니다");
    } else {
      lines.push(`빈 좌석 ${free}석 · 후보 ${live.length}석`);
    }
    return lines.join("<br>");
  }

  const QUIET_WATCH_TEXT = "잔여석 0석 · 실시간 취소표 대기 중 (30ms 초고속 감시)";
  async function runSeatAutopilot(config, { probe = false, catchMode = false, userInitiated = false, quiet = false } = {}) {
    // Pressing a button in the panel is the only thing that lifts a stop.
    // Every exit from this function records why. Without it a run that stopped
    // early looked identical to one that never started: no attempts, no
    // message, nothing in the panel to act on.
    // Claim a fresh generation, which retires any run already in flight.
    //
    // Generations were only bumped on script reload and by stopAll, so starting
    // a run did not stop the previous one: the auto-seat toggle fires a 좌석 잡기
    // on arriving at the seat map, and pressing 감시 시작 then started a second
    // loop beside it. Both drive the same seatState — observed live as the
    // 좌석 잡기 run reaching its 80-attempt cap and setting running = false out
    // from under the watch, which looked exactly like a stall.
    const runGen = (window.__nolsniperRunGen = (window.__nolsniperRunGen || 0) + 1);
    seatState.lastExit = "started";
    if (userInitiated) {
      seatState.haltedByUser = false;
      // A press of 감시 시작 is the full watch, parked in its 구역. The quiet
      // (sold-out) variant is only ever adopted by the landing itself, and a
      // run that ended some other way must not hand its quietness on.
      seatState.quietWatch = false;
    } else if (seatState.haltedByUser) {
      seatState.lastExit = "haltedByUser";
      return;
    }

    // Starting during a gateway block cannot succeed and risks extending it.
    const blockedFor = gatewayBlockRemainingMs();
    if (blockedFor > 0 && !probe) {
      const seconds = Math.ceil(blockedFor / 1000);
      seatState.lastError = `접속 차단 중 — ${seconds}초 후에 다시 시도하세요.`;
      updateOverlay(`접속 차단 중<br>${seconds}초 남음`, "error");
      return;
    }

    if (seatState.locked) {
      // Why a second 취켓팅 caught nothing.
      //
      // Catching a seat sets locked, and stopAll deliberately leaves it set so
      // stopping does not throw away a seat you won. But nothing else cleared
      // it either, so from the first catch onwards every press of 감시 시작
      // arrived here and returned — no watch, no error, no sign. A press is an
      // instruction; a stale flag must not outrank it.
      //
      // The page's own cart is the authority, not our flag: 0 means it is
      // holding nothing, -1 means it cannot be read (which is the common case
      // on a seat page — the live capture showed pageSelected -1 beside
      // locked true).
      const heldOnPage = selectedSeatCount();
      if (userInitiated && heldOnPage <= 0) {
        log("clearing a stale seat lock for a user-initiated run", { heldOnPage });
        traceCall("staleLock", heldOnPage, "cleared");
        seatState.locked = false;
        seatState.awaitingPayment = false;
        seatState.heldSeatIds.clear();
        updateOverlay("이전에 잡은 좌석 기록을 지우고 다시 감시합니다", "info");
      } else if (emptyPriceStepVisible() || seatSelectionEmpty()) {
        recoverEmptyPriceStep();
        seatState.running = false;
        return;
      } else if (bookingNoticeVisible() || !seatState.awaitingPayment) {
        updateOverlay("선점된 좌석 — 안내 확인 후 결제 단계로 이동합니다", "info");
        const advanced = await advanceAfterSeatLock(config);
        if (advanced?.noSeat || advanced?.recovered) seatState.locked = false;
        seatState.running = false;
        return;
      } else {
        // Genuinely holding seats. Refusing is right — a second watch would
        // take another and the site answers 선택 가능한 매수를 초과했어요 — but
        // it must say so on the panel, not only in a toast that fades.
        seatState.lastExit = "alreadyHolding";
        seatState.lastError =
          `이미 좌석 ${heldOnPage}석을 잡고 있습니다. 예매 창에서 결제를 마치거나 ` +
          `좌석을 비운 뒤 다시 [감시 시작]을 누르세요.`;
        updateOverlay("이미 좌석을 선점했습니다. 결제 화면을 확인하세요.", "ok");
        return;
      }
    }
    if (seatState.running) {
      seatState.stopRequested = true;
      await sleep(200);
    }
    if (!config.enabled) {
      seatState.lastExit = "config.enabled=false";
      seatState.lastError = "좌석 잡기가 꺼져 있습니다 (설정 enabled=false).";
      return;
    }

    const initData = getInitData();
    if (!initData?.sessionId || !initData?.goods || !initData?.playSeq) {
      // Reported as an error, not just an overlay: this aborts the whole run,
      // and it used to leave the panel showing nothing at all.
      const missing = [
        !initData?.sessionId && "sessionId",
        !initData?.goods && "goods",
        !initData?.playSeq && "playSeq",
      ].filter(Boolean);
      seatState.lastExit = `noSession:${missing.join(",")}`;
      seatState.lastError =
        `예매 세션을 읽지 못했습니다 (${missing.join(", ")} 없음). ` +
        `예매 창에서 [예매하기]를 눌러 좌석맵에 다시 들어오세요.`;
      updateOverlay(`예매 세션 없음<br>${missing.join(", ")}`, "error");
      return;
    }
    if (initData.goods.reservedSeat === false) {
      return runGeneralAdmission(config, initData, { probe });
    }

    // Decided solely by the caller. It used to also read config.mode, a value
    // persisted from whichever button was pressed last, so arriving on a seat
    // map could silently start 취켓팅 when 좌석 잡기 was what you wanted.
    const isCatch = catchMode;
    const gradeOrder = (config.grade_order || DEFAULT_SEAT_CONFIG.grade_order).map(String);
    const blockKeys = (config.block_keys || []).map(String);
    // 취켓팅 watches until told to stop. 좌석 잡기 honours the panel's 최대 시도 —
    // it used to be clamped to 8 whatever you typed, and since each attempt now
    // moves on to a different seat, 8 is nothing on a busy show.
    const maxAttempts = isCatch
      ? Number.MAX_SAFE_INTEGER
      : Math.min(Math.max(Number(config.max_attempts) || 8, 1), 200);
    // The watch paces itself. A configured speed may only ever slow it down,
    // and a 0 or absent value means "use the budget" — the panel used to send
    // 400 unconditionally, which overrode the budget and left the sweep at its
    // old speed however fast the autopilot meant to go.
    const pollMs = isCatch
      ? catchPollMs(config)
      : Number(config.speed_ms || config.poll_ms || 100);
    const quantity = Math.max(1, Number(config.quantity) || 1);
    // Reads the sweep shape the last poll recorded, so the wait tracks what the
    // venue actually costs rather than what it cost when the run started — a
    // 감시 구역 can be redrawn mid-run, and scoping changes the request count.
    const idlePollMs = (configuredMs) =>
      isCatch
        ? catchIdlePollMs(configuredMs, {
            requests: Math.ceil((seatState.catchWatchedBlocks || 0) / 2),
            sweepTicks: seatState.catchSweepTicks || 0,
            sweepMs: seatState.observedSweepMs || 0,
          })
        : configuredMs;

    seatState.running = true;
    seatState.stopRequested = false;
    seatState.confirmStarted = false;
    seatState.attempts = 0;
    seatState.lastError = "";
    seatState.discoveredBlocks = null;
    // Where the time between landing and the first click goes, in ms from run
    // start. Read from the panel's status to audit the fastest path.
    seatState.startupTiming = { startedAt: Date.now(), mode: isCatch ? "catch" : "grab" };
    seatState.runMode = isCatch ? "catch" : "grab";
    const markStartup = (name) => {
      if (seatState.startupTiming && !(name in seatState.startupTiming)) {
        seatState.startupTiming[name] = Date.now() - seatState.startupTiming.startedAt;
      }
    };
    seatState.markStartup = markStartup;
    seatState.statusFailures = 0;
    // Re-establish the diff baseline for this run without going blind.
    //
    // lastBlocks survives a run, so a second 취켓팅 diffed its first poll
    // against the masks the *previous* run left behind, and every seat that
    // freed in the gap came back as "just freed" — minutes stale and long since
    // taken. But clearing the masks to fix that was worse: seatIsFree() reads
    // false for a null mask, so the whole venue looked sold out until the sweep
    // refilled it two blocks at a time, and the run had nothing to work with.
    //
    // The masks stay. What resets is which blocks this run has *seen*: the
    // first reading of each block re-establishes its baseline and reports
    // nothing, and every reading after that diffs normally.
    seatState.runBaseline = new Set();
    resetSeatCountScope();
    seatState.catchCursor = 0;
    seatState.catchLiveTries = 0;
    seatState.catchLiveSignature = "";
    seatState.pageFreed.length = 0;
    seatState.pageStatusSeen = 0;
    seatState.pageStatusFreed = 0;
    seatState.observedTickMs = 0;
    seatState.observedSweepMs = 0;
    seatState.mapMoves = {};
    // Where we stand is a fact about the page, not about the run, but a fresh
    // run must verify it rather than inherit a claim from the last one.
    seatState.parkedBlock = "";
    seatState.parkedCheckedAt = 0;
    seatState.parkFailures = 0;
    seatState.reparks = 0;
    seatState.centerReachTried = "";
    seatState.triggerActedAt = 0;
    seatState.triggerBursts = 0;
    seatState.domScans = 0;
    seatState.domScanMs = 0;
    seatState.domScanWorstMs = 0;
    seatState.consecutiveRejects = 0;
    seatState.skippedByMap = 0;
    // The focus block is re-measured on this run's first tick, never inherited:
    // the previous run's block may be closed, and a stale one aims the poller
    // (and its "구역 N 고정" line) at a 구역 nobody is looking at. Same for the
    // per-run counters the status publishes.
    seatState.catchFocusBlock = "";
    seatState.catchFocusCheckedAt = 0;
    seatState.focusTicks = 0;
    seatState.focusTickWorkMs = 0;
    seatState.gradeRemainsAllZero = false;
    seatState.fastClickedId = "";
    seatState.fastClickedAt = 0;
    // Fresh per run, so 무작위 aims somewhere else next time while staying
    // stable for retries within this one.
    seatState.shuffleSeed = (Math.random() * 0x7fffffff) | 0;
    // A leftover error modal from a previous attempt blocks the whole map, and
    // nothing clears it while idle. Pressing a button is a delegation of
    // control, so clearing it here cannot surprise someone working by hand.
    dismissSeatErrorDialog();
    // Previous invisible-hand holds poison the session: the next real confirm
    // then answers P40021 CONFIRM_PRESELECTION_INVALID. Clear the page cart and
    // any remembered API holds before we click.
    if (!probe) {
      if (selectedSeatCount() > 0) {
        clearSelectedSeats();
        // Page sets an in-flight flag (j.current) during 전체삭제. 선택 완료
        // pressed while that flag is set → seat_requestPending / 선택 도중 오류.
        await sleep(700);
      }
      const stranded = [...seatState.heldSeatIds];
      if (stranded.length && !(await releasePreselected(stranded))) {
        seatState.lastError =
          `좌석 ${stranded.length}석을 반납하지 못했습니다 — 예매 창에서 [전체삭제]를 눌러 주세요.`;
      }
    }
    updateOverlay(
      probe
        ? `좌석 프로브… · ${AUTOPILOT_BUILD}`
        : isCatch
          ? (quiet || seatState.quietWatch ? QUIET_WATCH_TEXT : `취켓팅 감시 중… · ${AUTOPILOT_BUILD}`)
          : `좌석 스캔 중… 맵 클릭만 · ${AUTOPILOT_BUILD}`,
      "info",
    );

    if (captchaPresent()) {
      const cleared = await waitForCaptchaClear();
      if (!cleared && captchaPresent()) {
        updateOverlay("인증 화면이 열려 있어 좌석을 스캔할 수 없습니다.", "error");
        seatState.running = false;
        return;
      }
    }

    // A refund notice only means a seat is already held if one actually is.
    // This used to set `locked` and hand off to the checkout path on the notice
    // alone, so a run could end having taken nothing while believing it had.
    if (!probe && refundNoticeVisible() && pageHasSelectedSeats()) {
      seatState.locked = true;
      await advanceAfterSeatLock(config);
      seatState.running = false;
      return;
    }

    markStartup("captchaAndNotices");
    await waitForSeatMapReady({ allowRefundConfirm: !probe });
    markStartup("mapReady");

    async function refreshCandidates() {
      const remains = await fetchGradeRemains(initData);
      // Every grade at 0: sold out, known from one small request. Do not go
      // on to fetch 20 blocks / 3,000 seats to learn the same thing — that
      // wait is what kept a sold-out landing in "공연 정보 수집" for 10s+.
      const remainValues = Object.values(remains || {});
      seatState.gradeRemainsAllZero = remainValues.length > 0 && remainValues.every((n) => Number(n) <= 0);
      if (seatState.gradeRemainsAllZero && !isCatch) return [];
      const dom = filterSoldOutGradeCandidates(
        collectDomCandidates(gradeOrder, blockKeys, config),
        remains,
      );
      const completeDom = dom.filter((seat) => seat.blockKey && seat.seatGrade && seat.seatInfoId);
      try {
        const api = filterSoldOutGradeCandidates(
          await collectApiCandidates(initData, gradeOrder, blockKeys, config),
          remains,
        );
        if (api.length) {
          const soldOut = Object.entries(remains)
            .filter(([, n]) => Number(n) <= 0)
            .map(([name]) => name);
          updateOverlay(
            `잔여 ${api.length}석 인식` +
              (soldOut.length ? `<br>잔여0 제외: ${soldOut.join(", ")}` : "") +
              `<br>${AUTOPILOT_BUILD}`,
            "info",
          );
          return api;
        }
      } catch (error) {
        seatState.lastError = String(error);
        log("collectApiCandidates", error);
      }
      if (completeDom.length) {
        updateOverlay(`화면에서 좌석 ${completeDom.length}석 인식`, "info");
        return completeDom;
      }
      return dom;
    }

    // Ask the server to allocate instead of scanning. Cheaper than a full
    // seatMeta sweep on huge venues and the only route on maps that expose no
    // individually selectable seats.
    async function tryAutoAssign(pool) {
      const seed = pool.find((seat) => seat.blockKey && seat.seatGrade);
      if (!seed) return null;
      try {
        const result = await autoAssignSeats(initData, {
          blockKey: seed.blockKey,
          seatGrade: seed.seatGrade,
          seatInfoIds: pool
            .filter((seat) => seat.blockKey === seed.blockKey && seat.seatGrade === seed.seatGrade)
            .slice(0, quantity * 8)
            .map((seat) => seat.seatInfoId),
        });
        if (!result?.success || !result.seatInfoIds?.length) return null;
        const wanted = new Set(result.seatInfoIds.map(String));
        return pool.filter((seat) => wanted.has(String(seat.seatInfoId)));
      } catch (error) {
        seatState.lastError = String(error);
        return null;
      }
    }

    let candidates = await refreshCandidates();
    markStartup("candidates");
    seatState.startupTiming.candidateCount = candidates.length;
    if (probe) {
      seatState.running = false;
      // What a real run would do, without doing it. A seat that is on screen is
      // taken by clicking it and letting the page make its own request — the
      // POST below is only the fallback for a seat the map has not rendered, so
      // saying "POST /seats/select" unconditionally described a request the run
      // would not actually send.
      const wouldPick = selectSeatUnit(candidates, quantity, config.adjacent !== false);
      const onScreen = wouldPick.filter((seat) => seatNodeFor(seat.seatInfoId)).length;
      const wouldDo =
        !wouldPick.length
          ? "잡을 좌석 없음"
          : onScreen === wouldPick.length
            ? `좌석맵에서 ${wouldPick.length}석 클릭 (예매 창이 직접 요청)`
            : `${onScreen}석은 클릭, ${wouldPick.length - onScreen}석은 API 요청`;
      const wouldSend = wouldPick.length
        ? {
            via: wouldDo,
            url: initData.goods?.isInterlocking
              ? "/onestop/api/seats/select-external"
              : "/onestop/api/seats/select",
            method: "POST",
            fallbackOnly: onScreen === wouldPick.length,
            body: {
              goodsCode: initData.goods.goodsCode,
              placeCode: initData.goods.placeCode,
              playSeq: initData.playSeq.playSeq,
              sessionId: initData.sessionId,
              seatType: resolveSeatType(initData.goods),
              autoAssign: false,
              seats: wouldPick.map((seat) => ({ seatGrade: seat.seatGrade, seatInfoId: seat.seatInfoId })),
            },
          }
        : null;
      const result = {
        count: candidates.length,
        top: candidates.slice(0, 8),
        wouldPick,
        wouldSend,
        byGrade: candidates.reduce((acc, seat) => {
          const key = seat.seatGradeName || seat.seatGrade;
          acc[key] = (acc[key] || 0) + 1;
          return acc;
        }, {}),
      };
      seatState.lastProbe = result;
      const preview = wouldPick.map((seat) => seat.label).join(" / ") || "없음";
      updateOverlay(
        `프로브 — 잠금 없음<br>후보 ${candidates.length}석<br>선택 예정: ${preview}`,
        candidates.length ? "ok" : "warn",
      );
      return result;
    }

    let statusBlockKeys = blockKeys.slice();
    seatState.stopRequested = false;
    seatState.catchCursor = 0;

    // Drop any block cache from an earlier run before 취켓팅 starts.
    //
    // pollFreedSeats re-derives its poll list from seatState.lastBlocks after
    // the first tick and ignores the zone argument from then on. Because the
    // cache was never cleared, a 좌석 잡기 run that had loaded the whole venue
    // left the watch sweeping every block — the chosen zones still filtered at
    // ranking, so results were right, but a sweep cost ceil(blocks/2) x 400ms
    // when two zones should be a single request per tick.
    if (isCatch && statusBlockKeys.length) {
      const wanted = new Set(statusBlockKeys.map(String));
      const cached = seatState.lastBlocks || [];
      const scoped = cached.filter((block) => wanted.has(String(block.blockKey)));
      if (scoped.length !== cached.length) {
        seatState.lastBlocks = scoped;
        seatState.catchCursor = 0;
      }
    }
    // Be standing in the 구역 before a seat frees in it.
    //
    // This is what loses the race. Detection is fast now, but a freed seat that
    // is not drawn cannot be clicked: the map only mounts what is in the
    // viewport, so the run opens the 구역 (389ms), fits it (250ms) and waits a
    // further poll tick before it can even try — the better part of a second
    // spent arriving somewhere it could have been standing all along. Anyone
    // already in that block clicks in a frame.
    // A quiet watch (sold-out landing) never touches the viewport, not even
    // the one preparatory park; freed seats in unopened blocks are taken via
    // the API select fallback.
    if (isCatch && !quiet && !seatState.quietWatch) await parkInWatchedBlock(config, statusBlockKeys, { force: true });

    while (seatState.attempts < maxAttempts && !seatState.locked && !seatState.stopRequested) {
      if (runWasSuperseded(runGen)) {
        seatState.lastExit = "superseded";
        // Leave `running` to the run that replaced us.
        return;
      }

      // Before anything else this tick. A block makes every request fail and
      // every extra one can lengthen it, so there is nothing to gain by
      // continuing and something to lose. This used to be checked once, before
      // the first tick, so a block arriving mid-watch went unnoticed until the
      // next 감시 시작 and the loop polled through the whole lockout.
      const blockedNow = gatewayBlockRemainingMs();
      if (blockedNow > 0) {
        const seconds = Math.ceil(blockedNow / 1000);
        const where = seatState.blockedEndpoint ? ` · ${seatState.blockedEndpoint}` : "";
        seatState.lastExit = "blocked";
        seatState.lastError =
          `접속 차단 중 — ${seconds}초 후에 다시 시도하세요.${where}`;
        updateOverlay(`접속 차단 중<br>${seconds}초 남음${where}`, "error");
        break;
      }

      // Nothing on the map is reachable through a modal, and nothing else in
      // this loop was clearing one. Measured live: every computed click landed
      // on DIV.nds-e-dialog__overlay, a 1320x956 backdrop, and the run sat
      // behind it indefinitely — including the 취켓팅 wait below, which is where
      // a sold-out show spends all of its time. Cheap because the query only
      // runs when something modal is actually present.
      if (blockingOverlayNodes().length) dismissBlockingDialogs();
      // Cheap, and only does anything while a seat is pending agreement.
      checkDomAgreement();
      // The order is already full. Whatever this loop believes about its own
      // attempts, the page is holding what was asked for, and one more click
      // would be the one the site rejects with 선택 가능한 매수를 초과했어요.
      const held = selectedSeatCount();
      if (held > quantity) {
        // More than the order can take — leftovers from an earlier run, or a
        // selection made by hand. Advancing with these would be refused with
        // 선택 가능한 매수를 초과했어요, so hand them all back and start clean.
        updateOverlay(`좌석 ${held}석이 이미 선택돼 있습니다 (매수 ${quantity})<br>모두 해제합니다`, "warn");
        if (!clearSelectedSeats()) {
          seatState.lastExit = "overSelected";
          seatState.lastError =
            `이미 ${held}석이 선택돼 있습니다 (매수 ${quantity}). ` +
            `예매 창에서 [전체삭제]를 눌러 비운 뒤 다시 시도하세요.`;
          break;
        }
        await sleep(400);
        continue;
      }
      if (held === quantity && held > 0) {
        seatState.locked = true;
        seatState.lastSeat = seatState.lastSeat || `${held}석`;
        updateOverlay(`좌석 ${held}석 선택됨<br>안내 확인 중`, "ok");
        await advanceAfterSeatLock(config);
        seatState.running = false;
        return;
      }

      dismissBookingNotices();
      if (captchaPresent()) await waitForCaptchaClear();

      if (isCatch) {
        if (!statusBlockKeys.length) {
          try {
            statusBlockKeys = (await fetchBlockKeys(initData)).map((block) => block.blockKey).filter(Boolean);
          } catch (error) {
            seatState.lastError = String(error);
            statusBlockKeys = [];
          }
          if (!statusBlockKeys.length && seatState.lastBlocks?.length) {
            statusBlockKeys = seatState.lastBlocks.map((block) => block.blockKey).filter(Boolean);
          }
        }
        // Narrow to the drawn area once the seat data exists. The first tick
        // has to fetch the venue to know where anything is; every tick after
        // that watches only what was asked for.
        const watchRect = normalizeWatchRect(config.watch_rect);
        const sweepKeys =
          (watchRect && blocksInWatchRect(seatState.lastBlocks || [], watchRect)) || statusBlockKeys;
        // Hyper-focus. Once a block is open on screen the watch locks onto it:
        // one block, one request, every CATCH_FOCUS_POLL_MS, no leaving and
        // re-entering (that is the flicker), no cursor sweeping the venue.
        // Re-checked every 2s so a block the user opens by hand is followed.
        if (nowMs() - (seatState.catchFocusCheckedAt || 0) > 2000) {
          seatState.catchFocusCheckedAt = nowMs();
          const open = currentOpenBlock() || seatState.parkedBlock || "";
          seatState.catchFocusBlock =
            open && sweepKeys.map(String).includes(String(open)) ? String(open) : "";
        }
        const focused = Boolean(seatState.catchFocusBlock);
        const scoped = focused ? [seatState.catchFocusBlock] : sweepKeys;
        if (focused && seatState.lastBlocks?.length) startFocusPoller(initData, seatState.catchFocusBlock, config, runGen, gradeOrder, blockKeys);
        else if (!focused) stopFocusPoller();
        // Anything the 예매 창's own traffic already showed opening is taken
        // first. It cost us no request, so it is not subject to the budget
        // that paces the sweep, and it is as fresh as the page itself — which
        // on the block the user is looking at beats waiting for our cursor to
        // come round to it.
        const tickStartedPerf = performance.now();
        const overheard = seatState.pageFreed.splice(0);
        const burst = triggerFired();
        if (burst) seatState.triggerBursts = (seatState.triggerBursts || 0) + 1;
        // While the focus poller runs, detections arrive through the overheard
        // channel every 30ms; the loop must not await a second request on top.
        const pollerLive = focused && focusPoller.active && focusPoller.key === String(seatState.catchFocusBlock);
        const freed = overheard.length
          ? overheard
          : pollerLive ? [] : await pollFreedSeats(initData, scoped, config, { burst });
        if (overheard.length) seatState.lastFreedVia = "page";
        else if (freed.length) seatState.lastFreedVia = "poll";
        // What a tick actually costs, rather than what the sleep alone says.
        // Smoothed, because one slow request should not rewrite the estimate.
        if (!overheard.length) {
          const worked = performance.now() - tickStartedPerf;
          const spent = worked + pollMs;
          seatState.observedTickMs = seatState.observedTickMs
            ? Math.round(seatState.observedTickMs * 0.7 + spent * 0.3)
            : Math.round(spent);
          // The sweep alone, without the wait that follows it. observedTickMs
          // folds pollMs in by design — it answers "how long is a tick" — so it
          // can never say whether the wait itself is the slow part. This can,
          // and catchIdlePollMs is the caller that needs it.
          seatState.observedSweepMs = seatState.observedSweepMs
            ? Math.round(seatState.observedSweepMs * 0.7 + worked * 0.3)
            : Math.round(worked);
        }
        const fresh = seatState.polledBlocks?.size
          ? (seatState.lastBlocks || []).filter((block) =>
              seatState.polledBlocks.has(String(block.blockKey)),
            )
          : seatState.lastBlocks || [];
        const live = rankCandidates(
          collectFromBlocks(fresh, config),
          gradeOrder,
          blockKeys,
          pickerOptions(config, { isCatch: true }),
        );
        // The cap stops us hammering seats the map still shows as free but the
        // server has already sold. It must not become permanent: the map moves
        // constantly, so as soon as the free set actually changes those are
        // different seats and deserve a fresh run of attempts. Without this
        // reset the watcher sits at the cap forever, ignoring a map full of
        // free seats and waiting only for a 0->1 flip that may never come.
        const signature = liveSignature(live);
        if (signature !== seatState.catchLiveSignature) {
          seatState.catchLiveSignature = signature;
          seatState.catchLiveTries = 0;
        }
        const liveExhausted = (seatState.catchLiveTries || 0) >= CATCH_LIVE_TRIES;

        if (freed.length) {
          // From "a seat opened" to "we clicked it" — the only latency that
          // decides whether the seat is ours. Taken from the seat itself, so
          // the clock starts when the bitmap flipped rather than when this loop
          // came round to reading it.
          const detectedAt = freed.reduce(
            (earliest, seat) => Math.min(earliest, seat.freedAtPerf ?? Infinity),
            Infinity,
          );
          seatState.freedAtPerf = Number.isFinite(detectedAt) ? detectedAt : performance.now();
          startCatchTiming(seatState.freedAtPerf);
          candidates = rankCandidates(freed, gradeOrder, blockKeys, pickerOptions(config, { isCatch: true }));
          seatState.catchLiveTries = 0;
          // Focused: the seat is drawn right here — press it now, before the
          // general path ranks, aims and moves. clickSeatOnMap remembers the
          // press so the path below does not press it a second time.
          if (focused && quantity === 1) {
            const top = candidates[0];
            if (top && seatNodeFor(top.seatInfoId)) {
              noteCatchStage("click");
              if (clickSeatOnMap(top.seatInfoId, { countBefore: selectedSeatCount() })) {
                seatState.fastClickedId = String(top.seatInfoId);
                seatState.fastClickedAt = nowMs();
                seatState.fastClicks = (seatState.fastClicks || 0) + 1;
              }
            }
          }
        } else if (live.length && !liveExhausted) {
          candidates = live;
        } else {
          seatState.attempts += 1;
          // With nothing free the line is the one frozen sentence — no counters
          // ticking, nothing for the eye to chase.
          updateOverlayIfChanged(
            freeSeatCount() === 0
              ? QUIET_WATCH_TEXT + (focused ? ` · 구역 ${seatState.catchFocusBlock} 고정` : "")
              : catchStatusText(live, freeSeatCount(), focused ? CATCH_FOCUS_POLL_MS : pollMs, liveExhausted, watchRect)
                + (focused ? ` · 구역 ${seatState.catchFocusBlock} 고정` : ""),
            "info",
          );
          // Nothing is in play, so this is the one moment the travel is free.
          // No repark on idle ticks — the only park is the one before the loop.
          if (focused) {
            seatState.focusTicks = (seatState.focusTicks || 0) + 1;
            const worked = Math.round(performance.now() - tickStartedPerf);
            seatState.focusTickWorkMs = seatState.focusTickWorkMs ? Math.round(seatState.focusTickWorkMs * 0.7 + worked * 0.3) : worked;
          }
          // Period, not gap: with the poller doing the fetching, the loop only
          // drains the channel — a strict 30ms period keeps the press instant.
          await sleep(focused ? Math.max(0, CATCH_FOCUS_POLL_MS - (performance.now() - tickStartedPerf)) : idlePollMs(pollMs));
          continue;
        }
      } else if (!candidates.length) {
        candidates = await refreshCandidates();
      }

      if (!candidates.length) {
        // A sold-out show has nothing for 좌석 잡기 to find, ever. Grinding out
        // the full 최대 시도 against it and then reporting 선점 실패 (80회) reads
        // as a broken macro when the truth is that the venue is full — and it
        // is 취켓팅, not this, that waits for a cancellation.
        if (!isCatch && ((seatState.lastBlocks || []).length && freeSeatCount() === 0 || seatState.gradeRemainsAllZero)) {
          // Sold out is the normal state of a hot show, not a fault. Ending the
          // run here with an error made bootRoute restart a grab every tick —
          // overlay warn/error, mode error↔grabbing, the "발광" the user saw.
          // Hand this run to a quiet watch instead: same document, no park, no
          // map move, in-memory 30ms polling until a seat flips 0→1.
          seatState.lastExit = "soldOut";
          seatState.lastError = "";
          seatState.userCatch = true;
          seatState.quietWatch = true;
          updateOverlayIfChanged(QUIET_WATCH_TEXT, "info");
          return runSeatAutopilot(config, { catchMode: true, quiet: true });
        }
        const reason = seatState.lastError || `후보 없음 · 화면 좌석 ${seatState.domCircleCount || 0}개`;
        updateOverlay(`좌석맵 대기 (${seatState.attempts}) · ${reason}`, "info");
        seatState.attempts += 1;
        if (!isCatch && seatState.attempts >= 2 && seatState.lastError) break;
        await sleep(pollMs);
        continue;
      }

      // Prefer seats that are actually on screen — and never fall back to
      // off-screen API selection. Taking a seat over the API can lock it on the
      // server while leaving 선택 좌석 empty, which then produces
      // 좌석 선택 도중 오류가 발생했습니다 when 선택 완료 is pressed.
      //
      // The map virtualises: of a venue's seats only the ones in the current
      // viewport exist in the DOM. If nothing clickable is rendered, wait and
      // rescan — do not pick a ranked seat the user cannot click.
      // Exception: auto_assign has no circles to click; it uses the API path.
      let clickable = clickableAmong(candidates);
      seatState.clickableNow = clickable.length;
      // The best seat we want may be off-screen in a more central block. The map
      // boots showing one block, so clickableAmong only ever offers that block's
      // seats — and on a round where the centre has sold up front that block is
      // a wing, which is the "무조건 오른쪽" the user saw. Once per run (grab
      // only, never mid-catch), if the best *rendered* seat sits far down the
      // ranked list while the top of the list is in another block, move there
      // before settling for the wing.
      if (
        !isCatch && !config.auto_assign && clickable.length && candidates.length
        && !seatState.centerReachTried
      ) {
        const bestClickableIdx = candidates.indexOf(clickable[0]);
        const openBlk = String(clickable[0].blockKey || "");
        const wantBlk = String((candidates[0] || {}).blockKey || "");
        const tolerance = Math.max(OPEN_BLOCK_KEEP_RANK, Math.ceil(candidates.length * 0.05));
        if (wantBlk && wantBlk !== openBlk && bestClickableIdx > tolerance) {
          seatState.centerReachTried = wantBlk;
          updateOverlay(`더 가운데 구역으로 이동합니다… · ${AUTOPILOT_BUILD}`, "info");
          clickable = [];  // fall into the move path below, which aims at candidates[0]
        }
      }
      // 취켓팅 never moves the map: every leave/enter/fit/pan below repainted
      // the canvas each tick while the watch chased a candidate in another
      // block (measured: the viewport jerked continuously). A freed seat the
      // map has not drawn is taken through the API select fallback instead.
      if (!clickable.length && !config.auto_assign && !isCatch) {
        // Nothing we want is drawn. On a big venue that is normal: the map
        // mounts only what is in the viewport, and a stadium's first screen is
        // a picture with no seats at all until a 구역 is opened.
        //
        // Order matters. Opening a block and fitting it to the viewport are
        // both verified against a live venue; the panning fallback below rests
        // on a transform lookup that found nothing on the same page. So the
        // verified path leads and panning is the last resort. What each of
        // these actually costs is recorded by noteMapMove — the settle budgets
        // they run against are ceilings someone chose, not measurements.
        watchMapPointer();
        // A block entered a moment ago counts as open even before its circles
        // are drawn: currentOpenBlock() reads the circles, so during the
        // ~1s the SPA takes to draw them it answered null and the loop left the
        // block and entered it again — five times over, 24s to the first click
        // on a map that should have taken one. Measured 2026-09-04 13:02.
        const justParked = seatState.parkedBlock
          && nowMs() - (seatState.parkedCheckedAt || 0) < BLOCK_DRAW_GRACE_MS;
        const openBlock = currentOpenBlock() || (justParked ? String(seatState.parkedBlock) : null);
        // Distance decides which seat, but only among the ones we can afford to
        // reach. Stepping out of a 구역 and into another is the most expensive
        // thing this loop does; simply fitting the one already open costs a
        // fraction of it. So a candidate in the open block wins over a nearer
        // one elsewhere — by the time we had travelled, the nearer seat would
        // most likely be gone anyway.
        const aim = aimForCandidates(candidates, openBlock);
        const wantBlock = aim?.blockKey ? String(aim.blockKey) : "";

        if (wantBlock) {
          const block = (seatState.discoveredBlocks || []).find(
            (candidate) => String(candidate.blockKey) === wantBlock,
          );
          if (block) {
            if (openBlock === wantBlock) {
              // Right 구역, seat not mounted: fit it before anything else. The
              // map mounts by viewport, so this is the cheapest way to bring
              // the rest of the block into the DOM — and it used to run only
              // after our *own* entry, never for a block the user opened.
              await noteMapMove("fitBlock", wantBlock, () => fitBlockToView());
              // Reaching the seat used to end in `candidates = []; continue`,
              // which threw the ranking away and went back round the loop for
              // another poll before clicking anything. Having just paid the
              // travel, take the seat in this pass.
              clickable = await waitForClickable(candidates, 800);
            } else {
              // Wrong 구역, or none open. Step out if we are inside one, then
              // open the block the seat actually lives in.
              if (openBlock) {
                const left = await noteMapMove("leaveBlock", openBlock, () => leaveBlockToVenue());
                if (!left.ok) {
                  // Stuck inside; work with what is reachable here rather than
                  // spinning on seats we cannot get to.
                  seatState.blockEntryMisses = (seatState.blockEntryMisses || 0) + 1;
                  candidates = candidates.filter(
                    (seat) => String(seat.blockKey) === String(openBlock),
                  );
                  continue;
                }
              }
              updateOverlay(
                `구역 ${block.selfDefineBlock || block.blockKey} 여는 중…<br>${AUTOPILOT_BUILD}`,
                "info",
              );
              const entered = await noteMapMove("enterBlock", block.blockKey, () =>
                enterBlockForSeats(block),
              );
              if (entered.ok) {
                await noteMapMove("fitBlock", block.blockKey, () => fitBlockToView());
                // Chasing a seat moved us; record it, or the next idle check
                // fits a block it is already standing in.
                seatState.parkedBlock = String(block.blockKey);
                seatState.parkedCheckedAt = nowMs();
                if (seatState.markStartup) seatState.markStartup("blockEntered");
                clickable = await waitForClickable(candidates, 1500);
              }
            }
          }
        }

        // The travel worked and the seat is on screen. Fall through and click
        // it in this pass rather than starting the tick over.
        if (clickable.length) {
          seatState.clickableNow = clickable.length;
        } else {
          // Still not drawn. Pan the map to it as a last resort — this rests on
          // a transform lookup that found nothing on one real venue, which is
          // why it runs after the verified block-entry path above.
          const moved = aim
            ? await noteMapMove("aim", aim.seatInfoId, () => ensureSeatRendered(aim.seatInfoId, aim))
            : { ok: false, via: "no-candidate" };

          if (moved.ok) {
            clickable = clickableAmong(candidates);
            seatState.clickableNow = clickable.length;
          }

          if (!clickable.length) {
            // Could not reach it. Drop this one and try the next rather than
            // stalling on a seat the viewport will not produce.
            if (aim && (moved.timedOut || moved.via === "centred-but-absent")) {
              seatState.aimMisses = (seatState.aimMisses || 0) + 1;
              candidates = candidates.filter((seat) => seat.seatInfoId !== aim.seatInfoId);
              continue;
            }

            const why =
              moved.via === "user-dragging"
                ? "맵을 조작하는 중 — 손을 떼면 이어서 진행합니다"
                : moved.via === "no-transform"
                  ? "맵을 확대해 주세요 (이 화면은 자동 이동이 안 됩니다)"
                  : "좌석이 그려지길 기다리는 중";
            updateOverlay(
              `${why}<br>잡을 자리 ${candidates.length}석 · 화면 ${seatState.domCircleCount || 0}개<br>${AUTOPILOT_BUILD}`,
              "info",
            );
            await sleep(pollMs);
            candidates = [];
            continue;
          }
        }
      }
      const pool = clickable.length ? clickable : candidates;

      let group = config.auto_assign ? await tryAutoAssign(pool) : null;
      const viaAutoAssign = Boolean(group?.length);
      if (!viaAutoAssign) group = selectSeatUnit(pool, quantity, config.adjacent !== false);
      // Where the seat we are about to take sits — every hold path shares this.
      if (group[0]) seatState.lastSeatPos = { x: group[0].posLeft, y: group[0].posTop, block: group[0].blockKey };
      if (!group.length) {
        candidates = [];
        await sleep(pollMs);
        continue;
      }

      // Drop seats the map has drawn as unsellable before spending an attempt
      // on them. The bitmap and the rendered map disagree often enough that
      // this was most of the retries — and each one raised the page's own
      // 좌석 요청이 잘못되었습니다 dialog.
      if (!viaAutoAssign) {
        const refused = group.filter((seat) => {
          const node = seatNodeFor(seat.seatInfoId);
          return node && seatNodeDisabled(node);
        });
        if (refused.length) {
          const bad = new Set(refused.map((seat) => String(seat.seatInfoId)));
          candidates = candidates.filter((seat) => !bad.has(String(seat.seatInfoId)));
          seatState.skippedByMap = (seatState.skippedByMap || 0) + refused.length;
          continue;
        }
      }

      seatState.attempts += 1;
      if (seatState.freedAtPerf) {
        seatState.lastCatchLatencyMs = Math.round(performance.now() - seatState.freedAtPerf);
        seatState.freedAtPerf = 0;
      }
      const label = group.map((seat) => seat.label).join(" / ");
      updateOverlay(
        viaAutoAssign
          ? `자동배정 ${label}<br>시도 ${seatState.attempts}`
          : `맵 클릭 ${label}<br>시도 ${seatState.attempts} · ${AUTOPILOT_BUILD}`,
        "info",
      );

      try {
        const result = await selectSeats(initData, group, { autoAssign: viaAutoAssign });
        const blocked = result?.unselectableSeatInfoIds || [];
        if (blocked.length) finishCatchTiming(result?.reason || "declined");
        if (blocked.length && result?.reason === "taken") {
          // Someone else got there first. That is not a failed attempt by this
          // macro and must not spend one: during a busy open conflicts are the
          // common case, and counting them would end the run exactly when it
          // most needs to keep going. Cooled down rather than blacklisted —
          // holds expire and carts are abandoned, so the seat may come back.
          blocked.forEach(markSeatTaken);
          const lost = new Set(blocked);
          candidates = candidates.filter(
            (seat) =>
              !lost.has(String(seat.seatInfoId)) &&
              !group.some((picked) => picked.seatInfoId === seat.seatInfoId),
          );
          seatState.attempts = Math.max(0, seatState.attempts - 1);
          updateOverlay(
            `${label} 이미 선점됨 — 다음 자리로<br>` +
              `남은 후보 ${candidates.length}석 · 경합 ${seatState.takenConflicts || 0}회`,
            "warn",
          );
          // No sleep: the next seat is being raced for right now too.
          continue;
        }
        if (blocked.length) {
          // Someone else holds these, or the map would not hand them over.
          //
          // The cooldown is the load-bearing half. In 취켓팅 `candidates` is
          // rebuilt from freed/live at the top of every pass, so the filter
          // below is discarded before anything reads it — on its own it left
          // the same seat being re-attempted every tick, forever, at roughly
          // 1.5s a go, until eight of them tripped the catchLiveTries brake and
          // the watch went silent in front of a map full of free seats.
          // seatState.takenUntil is the only thing here that outlives a tick.
          blocked.forEach(markSeatUnreachable);
          const taken = new Set(blocked);
          candidates = candidates.filter(
            (seat) =>
              !taken.has(String(seat.seatInfoId)) &&
              !group.some((picked) => picked.seatInfoId === seat.seatInfoId),
          );
          // Deliberately not lastError: losing a seat to someone faster is a
          // normal outcome, and the empty-candidate branch treats a set
          // lastError as a reason to stop retrying altogether.
          updateOverlay(
            `${label} 선점 실패 (이미 나간 좌석)<br>남은 후보 ${candidates.length}석 · 시도 ${seatState.attempts}`,
            "warn",
          );
          if (isCatch) seatState.catchLiveTries = (seatState.catchLiveTries || 0) + 1;
          seatState.consecutiveRejects = (seatState.consecutiveRejects || 0) + 1;
          if (!isCatch && seatState.consecutiveRejects >= REJECT_STREAK_LIMIT) {
            // Every seat the map offers is being refused. That is a disagreement
            // between the bitmap and the server, not bad luck, and grinding out
            // the remaining attempts only makes the log longer.
            seatState.lastError =
              `연속 ${seatState.consecutiveRejects}회 거절 — 좌석맵과 서버가 어긋나 있습니다. ` +
              `좌석맵을 새로고침하거나 다른 구역·등급으로 바꿔 보세요.`;
            break;
          }
          await sleep(config.retry_ms);
          continue;
        }
        // A seat is taken. The hunt is over — unconditionally.
        //
        // This used to go back round the loop whenever advanceAfterSeatLock
        // could not confirm the price step, which is a routine timing miss.
        // The seat was already selected, so the next pass selected a *second*
        // one, and 매수 1 with two seats held is exactly the 좌석 요청이 잘못되었습니다
        // that kept appearing. Getting to checkout is a separate job from
        // finding a seat, and failing at it is never a reason to take another.
        seatState.consecutiveRejects = 0;
        seatState.locked = true;
        seatState.lastSeat = label;
        seatState.lastSeatPos = group[0] ? { x: group[0].posLeft, y: group[0].posTop, block: group[0].blockKey } : null;
        seatState.running = false;
        updateOverlay(
          viaAutoAssign
            ? `선점 ${label}<br>선택 완료…`
            : `맵 클릭 ${label}<br>선택 완료… · ${AUTOPILOT_BUILD}`,
          "ok",
        );
        notifyDiscord(`NOL Sniper 선점 ${label}`);

        const advanced = await advanceAfterSeatLock(config);
        if (advanced?.takenConflict) {
          // Lost at the last step. Put the seat on cooldown and keep hunting —
          // the whole point of the run is to come back with a seat, and the
          // attempt was spent on someone else's speed, not on a fault of ours.
          group.forEach((seat) => markSeatTaken(seat.seatInfoId));
          const lost = new Set(group.map((seat) => String(seat.seatInfoId)));
          candidates = candidates.filter((seat) => !lost.has(String(seat.seatInfoId)));
          seatState.attempts = Math.max(0, seatState.attempts - 1);
          seatState.locked = false;
          seatState.lastSeat = "";
          seatState.running = true;
          updateOverlay(
            `${label} 이미 선점됨 — 다음 자리로<br>` +
              `남은 후보 ${candidates.length}석 · 경합 ${seatState.takenConflicts || 0}회`,
            "warn",
          );
          continue;
        }
        if (advanced?.userContinues || advanced?.reserved) {
          // The seat is ours and the rest is the user's to finish. This flag was
          // declared, read twice, and never once assigned true — so the guard it
          // exists for could not fire, and pressing 감시 시작 while genuinely
          // holding seats silently re-ran advanceAfterSeatLock instead of saying
          // "you already have seats, go and pay". The stale-lock path above
          // clears it again when the page turns out to be holding nothing.
          seatState.awaitingPayment = true;
          seatState.running = false;
          return;
        }
        if (advanced?.confirmFailed || advanced?.awaitingManualConfirm) {
          seatState.running = false;
          return;
        }
        if (advanced?.noSeat || advanced?.recovered) {
          seatState.lastError =
            `${label} 맵 클릭 후 예매 창에 좌석이 표시되지 않습니다. ` +
            `예매 창을 확인하세요. 자동으로 다른 좌석을 잡지는 않습니다.`;
          updateOverlay(`${label} — 예매 창을 확인하세요`, "warn");
        }
        return;
      } catch (error) {
        const terminal = terminalSelectError(error);
        if (terminal) {
          // Retrying cannot clear either of these, and the loop used to spin on
          // them until it was stopped by hand.
          const held = [...seatState.heldSeatIds];
          if (held.length) await releasePreselected(held);
          if (terminal === "blocked") {
            seatState.blockedUntil = nowMs() + (error.gatewayBlockedMs || 0);
            seatState.lastError = error.serverMessage;
          } else if (terminal === "quota") {
            seatState.lastError =
              `예매 가능 매수를 초과했습니다 — 잡고 있던 좌석 ${held.length}석을 반납했습니다. ` +
              `이미 예매한 표가 있으면 취소 후 다시 시도하세요.`;
          } else {
            seatState.lastError = "로그인이 풀렸습니다 — 예매 창에서 다시 로그인하세요.";
          }
          break;
        }
        seatState.lastError = error?.serverMessage || String(error);
        if (isCatch) seatState.catchLiveTries = (seatState.catchLiveTries || 0) + 1;
        candidates = candidates
          .filter((seat) => !group.some((picked) => picked.seatInfoId === seat.seatInfoId))
          .concat(group);
        await sleep(config.retry_ms);
      }
    }

    if (!seatState.locked && !seatState.confirmStarted && !pageHasSelectedSeats()) {
      // Nothing was taken, so anything still held is a leak.
      //
      // The extra two conditions matter: a confirm already under way, or a seat
      // sitting in 선택 좌석, means the hold is real. Releasing on `locked`
      // alone deleted good soft-holds whenever the confirm path had bailed
      // early — which is how a successful PreselectSeat ended in
      // BulkDeselectSeats with no /seats/select in between.
      const stranded = [...seatState.heldSeatIds];
      if (stranded.length) await releasePreselected(stranded);
      updateOverlay(
        `선점 실패 (${seatState.attempts}회)${seatState.lastError ? `<br>${seatState.lastError}` : ""}`,
        "error",
      );
    }
    // A run that was superseded must not clear the flag the newer run owns.
    if (!runWasSuperseded(runGen)) seatState.running = false;
  }

  function compactDate(value) {
    if (!value) return null;
    const digits = String(value).replace(/\D/g, "");
    return digits.length >= 8 ? digits.slice(0, 8) : null;
  }

  // Is there a session on this page? true / false / null (cannot tell).
  // A visible top-bar "로그인" link with no "로그아웃"/"마이" beside it means
  // logged out. Checked on every poll so the panel warns before the open.
  function loginState() {
    try {
      const bar = document.querySelector("header, nav, [class*='Header'], [class*='header'], [class*='gnb']");
      const text = ((bar || document.body)?.innerText || "").replace(/\s+/g, " ");
      if (!text) return null;
      if (/로그아웃|마이페이지|마이 페이지|MY페이지|내 예매/.test(text)) return true;
      if (/(^| )로그인( |$)/.test(text) || /로그인\/회원가입/.test(text)) return false;
      return null;
    } catch { return null; }
  }
  function readShowContext() {
    const context = {
      goods_code: null,
      play_date: null,
      play_seq: null,
      play_time: null,
      goods_name: null,
      place_code: null,
      ticket_open: null,
      ready: false,
      logged_in: loginState(),
      url: location.href,
      page: "other",
    };

    if (isNolProductPage()) context.page = "nol";
    else if (isGoodsPage()) context.page = "goods";
    else if (isSeatPage()) context.page = "seat";
    else if (isGatesPage()) context.page = "gates";
    else if (isWaitingPage()) context.page = "waiting";

    const nolMatch = location.pathname.match(/\/ticket\/products\/([A-Z0-9]+)/i);
    if (nolMatch) context.goods_code = nolMatch[1].toUpperCase();
    const goodsMatch = location.pathname.match(/\/goods\/([A-Z0-9]+)/i);
    if (goodsMatch) context.goods_code = goodsMatch[1].toUpperCase();

    // Through the correction, or the panel is told the round the page loaded
    // with rather than the one on screen. Changing 일정 in place leaves
    // initData holding the old playSeq, so without this the panel cannot even
    // detect the change — and everything it keeps keyed to a round (the watch
    // rect, the sketch, the block list) silently points at one that is gone.
    const initData = withLivePlaySeq(getInitData());
    // The URL names the show on product/goods pages. The stored booking
    // session (initData) is the *previous* show there — measured 2026-09-04
    // 13:05: parked on goods/26012552, the panel showed 디어 에반 핸슨 because
    // sessionStorage still held that session. Only pages inside a session
    // (seat, schedule, pay) take their identity from it.
    if (initData?.goods?.goodsCode && !nolMatch && !goodsMatch) context.goods_code = initData.goods.goodsCode;
    // The round only belongs to this page when the stored session is *for* the
    // show the page is showing. On a product/goods page the URL names the show
    // and initData is the previous booking session — its playSeq is another
    // show's round, and copying it printed a stale 회차 (measured 2026-09-04:
    // 디어 에반 핸슨 shown as 회차 004 from an earlier session). Match the code.
    const initGoods = initData?.goods?.goodsCode ? String(initData.goods.goodsCode).toUpperCase() : "";
    const roundIsOurs = !!initData && (
      (!nolMatch && !goodsMatch) || (initGoods && initGoods === String(context.goods_code || "").toUpperCase())
    );
    if (initData?.goods?.goodsName) context.goods_name = initData.goods.goodsName;
    if (initData?.goods?.placeCode) context.place_code = initData.goods.placeCode;
    if (roundIsOurs && initData?.playSeq?.playSeq) context.play_seq = initData.playSeq.playSeq;
    if (roundIsOurs && initData?.playSeq?.playDate) context.play_date = compactDate(initData.playSeq.playDate);
    if (roundIsOurs && initData?.playSeq?.playTime) context.play_time = normalizePlayTime(initData.playSeq.playTime);

    const payload = flightPayload();
    if (!context.goods_name) context.goods_name = payloadString(payload, "goodsName");
    if (!context.place_code) context.place_code = payloadString(payload, "placeCode", /\d+/);
    // Never fall back to playStartDate, on any page. It is the run's first
    // night, not a round: on the goods page it became the panel's play_date
    // and then the arm's, and the schedule step went hunting for 20260804 in a
    // September calendar (측정: 엘리자벳). A missing date stays missing until a
    // real round supplies it.
    // The round the panel is aimed at, when the page itself names none: the
    // overlay then reads the picked 회차 (029) rather than the page default (001).
    try {
      const hint = loadShowHint();
      const hintGoods = String(hint?.goods_code || "").toUpperCase();
      if (hint && hintGoods && hintGoods === String(context.goods_code || "").toUpperCase()) {
        // A picked round overrides the page's own default selection entirely,
        // or the toast mixes the page's date with the panel's seq (measured:
        // "20260805 026" for a pick of 20260905/026).
        if (hint.play_seq) {
          context.play_seq = String(hint.play_seq);
          if (hint.play_date) context.play_date = compactDate(hint.play_date);
          if (hint.play_time) context.play_time = normalizePlayTime(hint.play_time);
        }
      }
    } catch (error) { /* a hint is optional */ }
    if (!context.play_seq) context.play_seq = payloadString(payload, "playSeq", /\d{3}/);
    if (!context.ticket_open) context.ticket_open = payloadString(payload, "bookingOpenTime");

    const title = document.querySelector("h1, h2")?.textContent?.trim();
    if (!context.goods_name && title && title.length < 80) context.goods_name = title;

    if (context.page === "nol") {
      const selection = readProductSelection();
      if (selection?.play_date) context.play_date = selection.play_date;
      if (selection?.play_time) context.play_time = selection.play_time;
      // Prefer the last catalog round once schedules have resolved playSeq.
      if (seatState.showCatalog?.goods_code === context.goods_code) {
        if (!context.play_seq && seatState.showCatalog.play_seq) {
          context.play_seq = seatState.showCatalog.play_seq;
        }
        if (!context.play_date && seatState.showCatalog.play_date) {
          context.play_date = seatState.showCatalog.play_date;
        }
        if (!context.play_time && seatState.showCatalog.play_time) {
          context.play_time = seatState.showCatalog.play_time;
        }
      }
    }

    // A NOL product page carries no playSeq at all — the round list only exists
    // in the ticketfront API, which the panel fetches from goods_code. Requiring
    // it here would leave `ready` false on every show.
    context.ready = context.page === "nol"
      ? Boolean(context.goods_code)
      : Boolean(context.goods_code && context.play_date && context.play_seq);
    return context;
  }

  // NOL is a Next.js App Router site: its data is not `__NEXT_DATA__` but an RSC
  // flight payload pushed into `self.__next_f` as JS string literals, so every
  // quote in the DOM reads as \" and a plain /"goodsName":"…"/ never matches.
  let flightCache = { html: null, text: "" };

  function flightPayload() {
    const html = document.documentElement?.innerHTML || "";
    if (flightCache.html === html) return flightCache.text;
    // Unescaping the whole document once is far cheaper than a tolerant regex
    // per field, and leaves the payload in ordinary JSON shape.
    const text = html.includes('\\"') ? html.replace(/\\"/g, '"') : html;
    flightCache = { html, text };
    return text;
  }

  function payloadString(payload, key, shape) {
    const pattern = shape
      ? new RegExp(`"${key}":"(${shape.source})"`)
      : new RegExp(`"${key}":"([^"]+)"`);
    const match = payload.match(pattern);
    return match ? match[1] : null;
  }

  function shouldAutoSeatsAfterEntry() {
    const arm = loadArmConfig();
    const seat = loadSeatConfig();
    // The arm is this entry's own statement; the seat config's copy is the
    // panel's default. OR-ing them made the checkbox impossible to turn off —
    // measured 2026-09-04 12:08: arm said false, a seat was still held.
    if (arm && Object.prototype.hasOwnProperty.call(arm, "auto_seats_after_entry")) {
      return Boolean(arm.auto_seats_after_entry);
    }
    return Boolean(seat.auto_seats_after_entry);
  }

  let catalogInflight = false;
  let lastCatalogTry = 0;
  let lastProductCatalogKey = "";

  async function ensureSeatCatalog() {
    if (!isSeatPage() || catalogInflight) return seatState.showCatalog;
    // Only skip the work when what we hold belongs to the round on screen. This
    // used to short-circuit on the mere existence of a catalog, which is what
    // let a previous round's blocks survive indefinitely.
    const seen = sampledRoundKey();
    const current = !seen || seen === seatState.blocksKey;
    if (current && seatState.discoveredBlocks?.length && seatState.showCatalog?.sketch?.length) {
      return attachCatalogBlocks(seatState.discoveredBlocks);
    }
    const now = Date.now();
    if (now - lastCatalogTry < 2500) return seatState.showCatalog;
    lastCatalogTry = now;
    catalogInflight = true;
    try {
      if (seatState.discoveredBlocks?.length) {
        return await enrichCatalogSketch(getInitData(), seatState.discoveredBlocks);
      }
      return await fetchShowCatalog();
    } catch (error) {
      log("ensureSeatCatalog failed", error);
      return seatState.showCatalog;
    } finally {
      catalogInflight = false;
    }
  }

  // Keep the 조작판 seat table in lockstep with the product booking panel:
  // whenever the user picks another day or round, refresh remains.
  async function ensureProductCatalog() {
    if (!isNolProductPage() || catalogInflight) return seatState.showCatalog;
    const selection = readProductSelection() || {};
    const key = `${selection.play_date || ""}|${selection.play_time || ""}`;
    const now = Date.now();
    const stale = now - lastCatalogTry > 2000;
    if (key === lastProductCatalogKey && !stale) return seatState.showCatalog;
    lastCatalogTry = now;
    catalogInflight = true;
    try {
      const catalog = await fetchShowCatalog();
      lastProductCatalogKey = `${catalog.play_date || ""}|${catalog.play_time || catalog.play_seq || ""}`;
      return catalog;
    } catch (error) {
      log("ensureProductCatalog failed", error);
      return seatState.showCatalog;
    } finally {
      catalogInflight = false;
    }
  }

  const PENDING_ROUND_KEY = "nolsniper_pending_round_v1";
  // How long an entry's round choice stays worth acting on. Long enough for a
  // queue to let you through, short enough that yesterday's press cannot drive
  // today's calendar.
  const PENDING_ROUND_TTL_MS = 30 * 60 * 1000;

  function rememberPendingRound(arm) {
    try {
      // localStorage, not sessionStorage: measured, attempt 9 — the chain from
      // the goods page through /waiting?key= into /onestop loses the session
      // store, so the round the user chose was gone by the time the seat map
      // asked for it and the map kept whatever round the site defaulted to.
      // Staleness is handled by the TTL and by matching goods_code on read.
      localStorage.setItem(PENDING_ROUND_KEY, JSON.stringify({
        goods_code: String(arm.goods_code || ""),
        play_date: String(arm.play_date || ""),
        play_seq: String(arm.play_seq || ""),
        play_time: String(arm.play_time || ""),
        at: Date.now(),
      }));
    } catch {
      /* a session with no storage still enters; it just cannot auto-pick */
    }
  }

  function takePendingRound() {
    try {
      const raw = localStorage.getItem(PENDING_ROUND_KEY);
      if (!raw) return null;
      const value = JSON.parse(raw);
      if (!value || Date.now() - Number(value.at || 0) > PENDING_ROUND_TTL_MS) {
        localStorage.removeItem(PENDING_ROUND_KEY);
        return null;
      }
      // Another show's leftover must never drive this one's calendar.
      const here = String((getInitData()?.goods?.goodsCode) || "");
      if (here && value.goods_code && here !== String(value.goods_code)) return null;
      return value;
    } catch {
      return null;
    }
  }

  function clearPendingRound() {
    try {
      localStorage.removeItem(PENDING_ROUND_KEY);
    } catch {
      /* nothing to clear */
    }
  }

  function bootRoute() {
    const arm = loadArmConfig();
    const seat = loadSeatConfig();

    // The round the entry was for, carried across the navigation that entry
    // performs. A multi-round show always lands here and cannot be made to skip
    // it, so finishing the choice is part of entering, not an extra feature.
    if (onSchedulePage()) {
      // onestop wipes our localStorage hand-off during its boot (see the seat
      // branch below); the arm survives and names the same round.
      const hint = loadShowHint() || {};
      const armGoods = String(arm?.goods_code || hint.goods_code || "");
      const armPlace = String(arm?.place_code || hint.place_code || "");
      const armSeq = String(arm?.play_seq || hint.play_seq || "");
      // The seq is the truth; correct its date from the show's own schedule so
      // a stale cross-origin arm date can never send the calendar to the wrong
      // day (measured: arm said seq 007 but date 20261017 while 007 is 10/25).
      const canonical = roundBySeq(armGoods, armPlace, armSeq, arm?.biz_code || hint.biz_code);
      const armedRound = armSeq
        ? { goods_code: armGoods, play_seq: armSeq,
            play_date: (canonical && canonical.play_date) || (arm && arm.play_date) || hint.play_date || "",
            play_time: (canonical && canonical.play_time) || (arm && arm.play_time) || hint.play_time || "" }
        : (arm && arm.play_date ? { goods_code: arm.goods_code, play_date: arm.play_date, play_seq: arm.play_seq, play_time: arm.play_time } : null);
      // The arm is the panel's freshest statement of the round; the pending
      // key can be a leftover from an earlier entry (measured: the chooser was
      // handed 10/17 while the arm said 10/25). Arm first, key as fallback.
      const pending = (armedRound && armedRound.play_date) ? armedRound : takePendingRound();
      // Never two choosers at once, and never the same lost cause twice: bootRoute
      // fires on every URL/DOM change, and re-launching the chooser for a date
      // the calendar does not have is what flapped between 회차 맞추는 중 and the
      // error forever (측정: 엘리자벳, 20260804 vs 20260904).
      const wantKey = pending ? String(pending.play_date || "") : "";
      if (pending && (seatState.scheduleChoosing || seatState.scheduleGaveUp === wantKey)) return;
      if (pending) {
        seatState.scheduleChoosing = true;
        clearScheduleNotice();
        updateOverlay("회차 선택 중…", "info");
        void chooseRoundOnSchedule(pending)
          .finally(() => { seatState.scheduleChoosing = false; })
          .then((result) => {
            if (result.chose) {
              clearPendingRound();
              void sleep(1200).then(dismissEntryNotice);
              updateOverlay("회차 선택 완료", "ok");
            } else {
              armState.lastError = result.reason || "회차를 고르지 못했습니다";
              // The calendar simply does not have this date: stop, do not loop.
              if (/찾지 못했습니다|예매할 수 없습니다/.test(armState.lastError)) seatState.scheduleGaveUp = wantKey;
              updateOverlay(armState.lastError, "warn");
            }
          })
          .catch((error) => {
            armState.lastError = `회차 선택 실패: ${String(error).slice(0, 90)}`;
            log("chooseRoundOnSchedule rejected", error);
          });
        return;
      }
    }

    if ((isNolProductPage() || isGoodsPage()) && arm?.enabled && !arm.fired) {
      // Not awaited on purpose — bootRoute must return. But a rejection here
      // used to be an unhandled promise rejection and nothing more.
      void runArmScheduler(arm).catch((error) => {
        armState.lastError = `예약 시작 실패: ${String(error).slice(0, 90)}`;
        log("runArmScheduler rejected", error);
      });
      return;
    }

    if (isNolProductPage()) {
      void ensureProductCatalog();
      return;
    }

    if (isSeatPage()) {
      // Landing here does not mean the right round was reached: a resumed
      // booking session keeps whichever round it was last used for. Settle that
      // before anything starts choosing seats on the wrong night.
      // The arm, not just the hand-off key: onestop clears our pending entry
      // from localStorage somewhere in its boot — measured, the key is gone by
      // the time the seat map renders while nolsniper_arm_v1 survives intact.
      // The arm already says which round was chosen, so read it there.
      const armedRound = arm && arm.play_date
        ? { goods_code: arm.goods_code, play_date: arm.play_date,
            play_seq: arm.play_seq, play_time: arm.play_time }
        : null;
      const pendingHere = takePendingRound() || armedRound;
      if (pendingHere && !seatState.locked && !seatState.confirmStarted) {
        void ensureArmedRound(pendingHere)
          .then((result) => {
            if (result.ok) {
              clearPendingRound();
              // A schedule-step complaint that the header now contradicts.
              if (result.matched) armState.lastError = "";
              if (result.changed) updateOverlay("회차 변경 완료", "ok");
              // The round is settled; now the seats. This branch used to
              // `return` without ever reaching the auto-run below, so with an
              // arm present the grab never started after entry — measured
              // 2026-09-04 12:30: 회차 변경 완료, then nothing, held 0.
              const autoRun = seat.enabled && shouldAutoSeatsAfterEntry();
              if (autoRun && !result.otherShow && !seatState.locked && !seatState.running
                  && !seatState.haltedByUser && !location.search.includes("step=price")) {
                void runSeatAutopilot(seat, { catchMode: false }).catch((error) => {
                  seatState.lastError = `좌석 잡기 실패: ${String(error).slice(0, 90)}`;
                  seatState.running = false;
                  log("runSeatAutopilot rejected", error);
                });
              }
            } else {
              armState.lastError = result.reason || "회차를 맞추지 못했습니다";
              // Seats must not be grabbed on a round the user did not choose.
              // This is the stop, not a warning next to business as usual.
              armState.roundMismatch = {
                wanted: pendingHere.play_date,
                shown: (result.shown || {}).play_date || "",
                captcha: !!result.captcha,
              };
              seatState.haltedByUser = true;
              updateOverlay(armState.lastError, "warn");
            }
          })
          .catch((error) => log("ensureArmedRound rejected", error));
        return;
      }

      // bootRoute fires on every URL change. While a seat is locked / confirm is
      // in progress it must stay completely passive — otherwise auto_seats_after_entry
      // re-enters runSeatAutopilot during the post-hold delay and a second
      // advanceAfterSeatLock clicks 선택 완료 again (→ 좌석 선택 도중 오류).
      if (seatState.haltedByUser) {
        void ensureSeatCatalog();
        return;
      }
      if (seatState.locked || seatState.confirmStarted || seatState.running) {
        void ensureSeatCatalog();
        return;
      }

      if (location.search.includes("step=price")) {
        // User continues from here; do not auto-fill under them.
        void ensureSeatCatalog();
        return;
      }
      void ensureSeatCatalog();
      if (seatState.userCatch && !seatState.haltedByUser) {
        // Resume the watch the user asked for; a grab would flip the mode.
        void runSeatAutopilot(seat, { catchMode: true, quiet: Boolean(seatState.quietWatch) }).catch((error) => {
          seatState.lastError = `취켓팅 재개 실패: ${String(error).slice(0, 90)}`;
          seatState.running = false;
        });
        return;
      }
      const autoRun = seat.enabled && shouldAutoSeatsAfterEntry();
      if (autoRun) {
        void runSeatAutopilot(seat, { catchMode: false }).catch((error) => {
          seatState.lastError = `좌석 잡기 실패: ${String(error).slice(0, 90)}`;
          seatState.running = false;
          log("runSeatAutopilot rejected", error);
        });
      }
    }
  }

  function boot() {
    const seat = loadSeatConfig();
    saveSeatConfig(seat);
    window.NOLSniper = {
      build: AUTOPILOT_BUILD,
      seatConfig: seat,
      armConfig: loadArmConfig,
      loadSeatConfig,
      saveSeatConfig,
      loadArmConfig,
      saveArmConfig,
      syncServerClock,
      serverTimeUnix,
      downsampleSketch,
      sketchFromSeatBlocks,
      seatingBlocks,
      race: {
        SEAT_TAKEN_DIALOG,
        SEAT_ERROR_DIALOG,
        seatTakenDialogVisible,
        seatErrorDialogVisible,
        unknownBlockingDialogText,
        markSeatTaken,
        markSeatUnreachable,
        seatHeldByUs,
        UNREACHABLE_COOLDOWN_MS,
        liveSignature,
        blockingOverlayAnswered,
        pageRegisteredSelection,
        waitForSoftHoldIdle,
        catchTimingSummary,
        catchTimingLine,
        startCatchTiming,
        noteCatchStage,
        finishCatchTiming,
        settleDelayFor,
        settleBudgetMs,
        SEAT_MAP_SETTLE_MS,
        SEAT_MAP_SETTLE_TRIES,
        collectFromBlocks,
        collectDomCandidates,
        selectedSeatCount,
        resetSeatCountScope,
        readGatewayBlock,
        noteGatewayBlock,
        gatewayBlockRemainingMs,
        BLOCK_FALLBACK_MS,
        WAITING_ENDPOINT,
        acquireWaitingUrl,
        waitingApiUsableHere,
        isUnreachableError,
        WAITING_UNREACHABLE_STREAK,
        seatInCooldown,
        seatUnreachableNow,
        sweepTakenCooldowns,
        state: seatState,
        armState,
        TAKEN_COOLDOWN_MS,
        calibrateVenueToScreen,
        blockClickPoint,
        blockAbsoluteExtent,
        overlayFit,
        recoverFailedConfirm,
        isVisible,
        isVisibleDialog,
        dismissAnyBlockingOverlay,
        describeBlockingOverlay,
        waitForCaptchaClear,
        captchaPresent,
        currentOpenBlock,
        blockKeyForSeatId,
        currentPlaySeqFromDom,
        withLivePlaySeq,
        adoptBlocksKey,
        stagePoint,
        ENTRY_LEAD_MS,
        maybeReenter,
        resetReentryState,
        REENTRY_SPACING_MS,
        REENTRY_LIMIT,
        armEntryStartUnix,
        waitingIntervalAt,
        WAITING_POLL_SHAPE,
        describeWaitingAnswer,
        noteWaitingAttempt,
        WAITING_LOG_LIMIT,
        LINE_UP_PATH,
        RANK_PATH,
        RANK_POLL_MS,
        RANK_SESSION_GRACE_MS,
        SECURE_URL_BURST_MS,
        SIGNATURE_MAX_AGE_MS,
        PREMINT_LEAD_MS,
        decideLineUp,
        decideRank,
        queueKeyFrom,
        preconnectQueueHost,
        rememberQueueHost,
        QUEUE_HOST_KEY,
        sampledRoundKey,
        pollFreedSeats,
        liveSeatIndex,
        rebuildSeatIndex,
        seatIndex,
        mapAreaControls,
        leaveBlockToVenue,
        ensureSeatRendered,
        applyBlockMask,
        clickableAmong,
        parkInWatchedBlock,
        probeSoftHold,
        probeQueueOrigin,
        probeEntry,
        armTargetUnix,
        findBookButton,
        bookButtonPressable,
        unlockBookButton,
        enterFromNolPage,
        confirmBookModal,
        noteClickAttempt,
        CLICK_LOG_LIMIT,
        ENTRY_CLICK_LEAD_MS,
        ENTRY_CLICK_POLL_MS,
        ENTRY_CLICK_WINDOW_MS,
        ENTRY_FORCE_AFTER_MS,
        nowMs,
        anchorClock,
        clockJumpSeconds,
        clockJumped,
        CLOCK_JUMP_TOLERANCE_S,
        clockState,
        describeSeatBinding,
        preselectSeat,
        bulkPreselectSeats,
        clickSeatOnMap,
        seatNodeFor,
        checkDomAgreement,
        noteBitmapSawFree,
        fetchMasksFor,
        SWEEP_CONCURRENCY,
        freeSeatsByGrade,
        triggerFired,
        aimForCandidates,
        noteMapMove,
        notePageSeatStatus,
        onestopHeaders,
        seatQueryParams,
        releasePreselected,
      },
      /**
       * Take the panel's whole-venue trigger.
       *
       * The remaining-seat feed answers "did anything free anywhere?" in one
       * request (~132ms measured) where sweeping a 34-block venue takes 17 and
       * about 4.4 seconds. It is served from another origin with no
       * Access-Control-Allow-Origin, so the page cannot read it and the panel
       * looks on its behalf.
       *
       * Data, never a command: it must not restart a run or reload anything.
       */
      setWatchTrigger: (trigger) => {
        if (trigger && typeof trigger === "object") seatState.watchTrigger = trigger;
      },
      /**
       * Enter right now, for a show that is already open.
       *
       * The scheduled path waits for 티켓 오픈; a show that opened yesterday has
       * nothing to wait for, and a greyed 대기 시작 is not an answer. This runs
       * the same two calls immediately and navigates on the URL they return.
       */
      enterNow: async (override) => {
        const arm = { ...loadArmConfig(), ...(override || {}) };
        // The host discards this return value, so everything the panel needs
        // to show goes through armState (→ status().arm) as well.
        const refuse = (result) => {
          armState.lastError = result.reason;
          armState.enterNow = { at: Date.now(), ...result };
          updateOverlay(result.reason, "warn");
          return result;
        };
        if (!arm.goods_code || !arm.play_seq) {
          return refuse({ ok: false, reason: "공연과 회차를 먼저 고르세요" });
        }
        if (loginState() === false) {
          return refuse({ ok: false, reason: "[로그인 필요 — 세션이 없습니다] 예매 창에서 로그인한 뒤 다시 누르세요." });
        }
        if (!secureUrlUsableHere()) {
          return refuse({
            ok: false,
            reason: `예매 창이 ${GATE_ORIGIN} 에 있어야 합니다 (현재 ${location.origin})`,
            needsParking: true,
            parkUrl: `${GATE_ORIGIN}/goods/${arm.goods_code}`,
          });
        }
        rememberPendingRound(arm);
        armState.lastError = "";
        armState.enterNow = { at: Date.now(), ok: null, reason: "" };
        try {
          const result = await enterViaSecureUrlWithRetries(arm, { windowMs: 8000 });
          if (!result) {
            return refuse({ ok: false, reason: armState.lastError || "대기열 URL을 받지 못했습니다" });
          }
          armState.enterNow = { at: Date.now(), ok: true, route: "secure-url" };
          return { ok: true, route: "secure-url", waitingUrl: result.waitingUrl };
        } catch (error) {
          const reason = String(error && error.message ? error.message : error);
          return refuse({
            ok: false,
            reason,
            notOpenYet: !!(error && error.notOpenYet),
            blocked: !!(error && error.blocked),
          });
        }
      },
      auditBlocks: () => auditBlocks(),
      sketchCache: { parkSketch, parkedSketchFor, restoreParkedSketch, currentSketchKey, sketchKeyFits },
      syncGrades: () => syncGrades(loadSeatConfig()),
      fetchShowCatalog,
      readShowCatalog: () => {
        // Notice a 일정 change here, because this is the one thing that runs
        // continuously. Nothing else did: ensureSeatCatalog returns early once
        // a catalog exists, and the invalidation lived only on run paths — so
        // changing the date while simply browsing was never detected at all and
        // the panel went on describing the previous round.
        try {
          const seen = sampledRoundKey();
          if (seen && adoptBlocksKey(seen)) void ensureSeatCatalog();
        } catch (error) {
          /* a poll must never throw */
        }
        if (seatState.discoveredBlocks?.length && !seatState.showCatalog?.blocks?.length) {
          attachCatalogBlocks(seatState.discoveredBlocks);
        }
        // A page that belongs to no show — NOL home, 오픈 예정, ticket home —
        // publishes no catalog, so the panel can let go of the previous show
        // instead of showing its rounds under a page that has none.
        if (!onShowPage()) return null;
        let catalog = restoreParkedSketch(seatState.showCatalog);
        // The 일정 picker's source. Attached here rather than on its own host
        // command so both hosts publish it with no change to either.
        //
        // Note the catalog may not exist yet: on the parked goods page there is
        // no seat map and so no seat catalog at all, which is exactly when the
        // picker most needs filling. Attaching to a null object published
        // nothing and left the dropdown empty — measured, attempt 1.
        try {
          const arm = loadArmConfig() || {};
          const context = readShowContext() || {};
          // The arm first, deliberately. `restoreParkedSketch` hands back a
          // catalog persisted in localStorage, which on a freshly parked page
          // still describes the *previous* show — measured, attempt 2: it filled
          // the picker with another show's single round and entry then landed on
          // 일정 선택. The arm is the one statement of which show is being
          // entered, so it outranks anything left over.
          // Trust the hint only for the show the page actually names.
          const hint = loadShowHint() || {};
          const pageGoods = String(context.goods_code || "");
          const hintFits = hint.goods_code && (!pageGoods || String(hint.goods_code) === pageGoods);
          const goods = String(arm.goods_code || context.goods_code || (hintFits ? hint.goods_code : "") || catalog?.goods_code || "");
          const place = String(arm.place_code || context.place_code || (hintFits ? hint.place_code : "") || catalog?.place_code
                               || placeCodeFromPage() || "");
          if (catalog && goods && String(catalog.goods_code || goods) !== goods) {
            // Left over from another show; its blocks and sketch are not ours.
            catalog = { goods_code: goods, place_code: place };
          }
          const schedule = scheduleFor(goods, place, arm.biz_code);
          if (schedule) {
            if (!catalog) catalog = { goods_code: goods, place_code: place };
            catalog.rounds = schedule.rounds;
            catalog.ticket_open_date = schedule.ticket_open_date;
            if (schedule.goods_name && !catalog.goods_name) catalog.goods_name = schedule.goods_name;
            if (schedule.place_name && !catalog.place_name) catalog.place_name = schedule.place_name;
          }
        } catch (error) {
          /* a poll must never throw */
        }
        return catalog;
      },
      status: seatStatusSummary,
      runEntry: () => runArmScheduler(loadArmConfig()),
      runSeats: () => runSeatAutopilot(loadSeatConfig(), { userInitiated: true }),
      runCatch: () => {
        // The user chose the watch: from here bootRoute resumes a catch, never
        // a grab, until 전부 정지 (measured: auto-seat restarted a grab run in
        // the gap after a lost race and the mode flapped watching↔grabbing).
        seatState.userCatch = true;
        return runSeatAutopilot(loadSeatConfig(), { catchMode: true, userInitiated: true });
      },
      probeSeats: () => runSeatAutopilot(loadSeatConfig(), { probe: true, userInitiated: true }),
      // The spike. Holds one seat over the API, watches whether the 예매 창
      // notices, hands it straight back, and never presses 선택 완료.
      probeSoftHold: () => probeSoftHold(),
      // One /waiting request from wherever the 예매 창 is, to settle whether
      // API entry is possible from this page. Never enters, never navigates.
      probeQueueOrigin: () => probeQueueOrigin(),
      // 진입 점검 — what the entry would do, reported without doing any of it.
      // Safe on a show that has not opened, which is the whole point: it is
      // what replaces shifting the device clock to see the button light up.
      probeEntry: () => probeEntry(),
      // Return every seat this page holds — the map's selection and the API
      // preselects — and drop the lock so a run can start again. Used by the
      // panel's release command and by rehearsals that must not keep a seat.
      // Verification hooks. forgetHold: drop the local lock but keep the
      // server hold (the seat reads 0 to everyone); releaseOnly: give that
      // seat back on the server while the watch keeps running — a synthetic
      // 0→1 on a real map, so the whole path can be timed end to end.
      forgetHold() {
        const held = [...seatState.heldSeatIds];
        seatState.syntheticHold = held;
        seatState.heldSeatIds.clear();
        seatState.locked = false; seatState.confirmStarted = false; seatState.awaitingPayment = false;
        seatState.lastCatchLatency = null;
        return { forgot: held.length, ids: held };
      },
      async releaseOnly() {
        const ids = seatState.syntheticHold || [];
        seatState.syntheticHold = [];
        const ok = ids.length ? await releasePreselected(ids) : true;
        seatState.syntheticReleasedAt = performance.now();
        return { ok, released: ids.length };
      },
      async releaseHeld() {
        stopFocusPoller();
        abortSeatNetWaiters();
        window.__nolsniperRunGen = (window.__nolsniperRunGen || 0) + 1;
        seatState.running = false;
        seatState.stopRequested = true;
        const held = [...seatState.heldSeatIds];
        let cleared = 0;
        if (selectedSeatCount() > 0) { clearSelectedSeats(); cleared = selectedSeatCount(); }
        let ok = true;
        if (held.length) ok = await releasePreselected(held);
        seatState.locked = false;
        seatState.awaitingPayment = false;
        seatState.confirmStarted = false;
        seatState.heldSeatIds.clear();
        updateOverlay(ok ? `좌석 반납 완료 (${held.length}석)` : "좌석 반납 실패 — 예매 창에서 [전체삭제]를 눌러주세요", ok ? "ok" : "warn");
        return { ok, released: held.length, clearedOnMap: cleared };
      },
      stopAll() {
        seatState.userCatch = false;
        seatState.quietWatch = false;
        stopFocusPoller();
        // Nothing focused, nothing waiting: the next run measures its own
        // 구역, and a press parked on a network answer exits now, not in 2.5s.
        seatState.catchFocusBlock = "";
        seatState.catchFocusCheckedAt = 0;
        abortSeatNetWaiters();
        window.__nolsniperRunGen = (window.__nolsniperRunGen || 0) + 1;
        seatState.running = false;
        seatState.stopRequested = true;
        // Sticky: bootRoute must not restart it on the next URL change.
        seatState.haltedByUser = true;
        armState.running = false;
        // A rank poll in flight belongs to a run that is over.
        armState.entryGen = (armState.entryGen || 0) + 1;
        // Stopping with holds outstanding would silently eat the allowance —
        // both the API-level ones and whatever the seat map is showing.
        if (!seatState.locked && selectedSeatCount() > 0) clearSelectedSeats();
        const held = [...seatState.heldSeatIds];
        if (held.length && !seatState.locked) {
          releasePreselected(held).then((ok) =>
            updateOverlay(
              ok ? `정지했습니다 · 좌석 ${held.length}석 반납` : "정지했습니다 · 좌석 반납 실패",
              "warn",
            ),
          );
        } else {
          updateOverlay("정지했습니다.", "warn");
        }
        return true;
      },
      // One instrumented click on one seat, then stop. Everything it learns goes
      // to the persistent trace, so a failure can be read out of the state file
      // instead of reproduced by hand from a screenshot.
      async diagnose() {
        try {
          // The block audit runs first: a short block list explains a wrong
          // picker map on its own, and it needs no seat to click.
          try {
            traceCall("blockAudit", null, await auditBlocks());
          } catch (error) {
            traceCall("blockAudit", null, { fatal: String(error).slice(0, 300) });
          }
          return await runDiagnose();
        } catch (error) {
          // An unhandled rejection here is invisible from outside the browser,
          // which is exactly the blindness this command exists to remove.
          traceCall("diagnose", null, { fatal: String(error).slice(0, 300) });
          return { fatal: String(error) };
        }
      },
      clearTrace() {
        trace.length = 0;
        return true;
      },
      readShowContext,
      // Pure seat-picking helpers, exposed for out-of-browser verification and
      // for inspecting why a given seat was or was not chosen.
      picker: {
        normalizeGradeToken,
        rankGrade,
        rankCandidates,
        groupCandidates,
        selectSeatUnit,
        venueCenterX,
        stagePoint,
        floorRank,
        resolveSeatType,
        decodeStatusMask,
        parseSeatStatus,
        // Exposed for the alignment tests: "no information" must read as "do
        // not try", never as "free".
        seatIsFree,
        seatSellable,
        blockToStandIn,
        steadyRequestsPerTick,
        catchPollMs,
        readUnselectable,
        readGatewayBlock,
        BLOCK_FALLBACK_MS,
        looksLikeBookingContext,
        toCandidate,
        numOrNull,
        SEAT_STRATEGIES,
        selectedSeatCount,
        pageHasSelectedSeats,
        seatSelectionEmpty,
        pageHasSelectedSeats,
        liveSignature,
        catchStatusText,
        firePointerSelect,
        collectFromBlocks,
        countFree,
        CATCH_MIN_POLL_MS,
        CATCH_MAX_REQUESTS_PER_TICK,
        CATCH_LIVE_TRIES,
        CATCH_FAST_POLL_MS,
        CATCH_MAX_REQUESTS_PER_SEC,
        catchIdlePollMs,
      },
      // Exposed so the "never auto-pay" guarantee can be asserted in tests.
      guards: {
        COMMIT_BUTTON,
        ADVANCE_BUTTON,
        isBookingNoticeConfirm,
        isBookingNoticeCopy,
        BOOKING_MODAL_COPY,
      },
      // Detection only — the solver is gone. isCaptchaPageCopy is deliberately
      // narrow: it matches 문자를 입력해주세요 rather than 보안문자, because our own
      // toast contains that word and would otherwise match itself.
      captcha: { isCaptchaPageCopy },
      /**
       * The panel has written config; re-decide what this page should do.
       *
       * localStorage is per-origin and the booking flow crosses two. The script
       * boots on a fresh origin, bootRoute() reads a config that is not there
       * yet, and nothing re-runs it — the 400ms watcher only fires on a URL
       * change, which has just happened. So config arriving a moment after a
       * navigation was never read, and landing on the seat map after the queue
       * could leave auto_seats_after_entry evaluated against nothing.
       *
       * Refuses while a run owns the page: re-routing under a run in progress
       * is how a second selection gets made on top of a held seat.
       */
      configApplied() {
        if (seatState.running || seatState.locked || seatState.confirmStarted) return false;
        if (armState.running) return false;
        bootRoute();
        return true;
      },
      resetArm() {
        const arm = loadArmConfig();
        if (!arm) return null;
        const next = { ...arm, fired: false };
        saveArmConfig(next);
        armState.fired = false;
        armState.reentryTries = 0;
        return next;
      },
      // Live internals for the out-of-browser recovery tests (stop → restart
      // must leave nothing behind). Read-only by convention; not a public API.
      __test: {
        seatState, armState, focusPoller,
        startFocusPoller, stopFocusPoller, focusPollerAlive,
        pressSequence, waitForSeatNet, resolveSeatNetWaiters, abortSeatNetWaiters,
        updateOverlay, updateOverlayIfChanged, runWasSuperseded,
        FOCUS_WORKERS,
      },
    };
    installNetworkWatch();
    log(alreadyLoaded ? `autopilot reloaded ${AUTOPILOT_BUILD}` : `autopilot loaded ${AUTOPILOT_BUILD}`);
    if (alreadyLoaded && isSeatPage()) {
      updateOverlay(`스크립트 갱신 · ${AUTOPILOT_BUILD}<br>맵 클릭만 사용합니다`, "ok");
    }
    if (!alreadyLoaded) bootRoute();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  let lastPath = location.href;
  window.__nolsniperWatchId = setInterval(() => {
    if (location.href !== lastPath) {
      lastPath = location.href;
      seatState.running = false;
      bootRoute();
    }
    // Only while a run is actually in progress. Clicking buttons on a page the
    // user is working by hand is worse than leaving a dialog up: they can close
    // it themselves, but they cannot undo a press they did not make.
    if (isSeatPage() && seatState.running) dismissSeatErrorDialog();
    if (isSeatPage() && !seatState.discoveredBlocks?.length) void ensureSeatCatalog();
    if (isNolProductPage()) void ensureProductCatalog();
    void maybeReenter().catch((error) => {
      armState.lastError = `재진입 실패: ${String(error).slice(0, 90)}`;
      log("maybeReenter rejected", error);
    });
  }, 400);
})();
