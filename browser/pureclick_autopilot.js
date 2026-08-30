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
    if (window.__pureclickPopupShim) return;
    window.__pureclickPopupShim = true;
    window.__pureclickPopups = [];

    const record = (entry) => {
      window.__pureclickPopups.push({ at: Date.now(), ...entry });
      if (window.__pureclickPopups.length > 20) window.__pureclickPopups.shift();
      console.log("[NOL Sniper] popup", entry);
    };

    const go = (url, { replace = false } = {}) => {
      if (!url) return;
      const absolute = new URL(String(url), location.href).href;
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
        clone.target = "_self";
        document.body.appendChild(clone);
        nativeSubmit.call(clone);
      } catch (error) {
        console.log("[NOL Sniper] written form failed", error);
      }
    }

    let nativeSubmit = null;

    if (typeof window.open === "function") {
      window.open = function pureclickOpen(url, name, features) {
        record({ open: String(url || ""), name: String(name || ""), features: String(features || "") });
        if (url) go(url);
        return popupProxy(name);
      };
    }

    // form.submit() bypasses submit listeners entirely, and that is exactly the
    // call NOL uses for the seat-booking POST, so the prototype has to be patched.
    if (typeof HTMLFormElement !== "undefined") {
      nativeSubmit = HTMLFormElement.prototype.submit;
      HTMLFormElement.prototype.submit = function pureclickSubmit() {
        if (!POPUP_SELF_TARGETS.has(this.target || "")) {
          record({ formTarget: this.target, action: this.action });
          this.target = "_self";
        }
        return nativeSubmit.apply(this, arguments);
      };
    }

    // User-triggered submits and target=_blank links, for completeness.
    document.addEventListener(
      "submit",
      (event) => {
        const form = event.target;
        if (form && form.target && !POPUP_SELF_TARGETS.has(form.target)) form.target = "_self";
      },
      true,
    );
    document.addEventListener(
      "click",
      (event) => {
        const anchor = event.target?.closest?.("a[target]");
        if (anchor && !POPUP_SELF_TARGETS.has(anchor.target)) anchor.target = "_self";
      },
      true,
    );

    // openPCOnestop's queue path calls window.self.close() *before* steering the
    // popup. Honouring it would close the only window we have.
    try {
      window.close = function pureclickClose() {
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

  const alreadyLoaded = Boolean(window.PureClick);
  // Abort any in-flight run from a previous script copy. Old async loops keep
  // their old selectSeats (with the API fallback) unless we invalidate them.
  window.__pureclickRunGen = (window.__pureclickRunGen || 0) + 1;
  if (alreadyLoaded) {
    try {
      window.PureClick.stopAll();
    } catch {
      /* ignore */
    }
  }
  if (window.__pureclickWatchId) {
    clearInterval(window.__pureclickWatchId);
    window.__pureclickWatchId = 0;
  }

  function runWasSuperseded(runGen) {
    return runGen !== window.__pureclickRunGen;
  }

  const SEAT_STORAGE_KEY = "pureclick_seat_v1";
  const ARM_STORAGE_KEY = "pureclick_arm_v1";
  const SYNC_URL = "https://poticket.interpark.com/Book/BookMain.asp";
  const TICKETFRONT = "https://api-ticketfront.interpark.com";
  const NOL_ORIGIN = "https://nol.yanolja.com";
  const SSO_ORIGIN = "https://sso.yanolja.com";
  const GATE_ORIGIN = "https://tickets.interpark.com";
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
  };

  const SEAT_META_CONCURRENCY = 6;
  // One seatStatus call covers two blocks. Holding requests-per-second steady
  // is what keeps the gateway quiet, so this and the interval below are the
  // budget; how long a sweep takes follows from how many blocks are watched.
  // How far ahead of the open to start asking for a queue slot. The request
  // loop is built to retry across the boundary, so being early is the point:
  // the first request the server is willing to accept is then ours.
  const ENTRY_LEAD_MS = 400;
  const CATCH_MIN_POLL_MS = 200;
  const CATCH_MAX_REQUESTS_PER_TICK = 1;
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
    // seatInfoId -> unix ms when it may be tried again. A seat someone else
    // just took is not gone for good: holds expire and carts are abandoned, so
    // it rejoins the pool rather than being blacklisted for the run.
    takenUntil: new Map(),
    // Seats the page's own traffic showed opening, waiting for the loop.
    pageFreed: [],
    // What travelling to a seat actually costs, by kind of move.
    mapMoves: {},
    // The panel's whole-venue "did anything free?" verdict.
    watchTrigger: null,
    triggerActedAt: 0,
    syncedSummary: null,
    lastProbe: null,
    lastBlocks: null,
    awaitingPayment: false,
    stopRequested: false,
    lastStatusStamp: "",
    showCatalog: null,
    message: "",
    catchCursor: 0,
    discoveredBlocks: null,
    domCircleCount: 0,
  };

  // Survives reload_autopilot the same way the trace does. 감시 시작 used to
  // re-inject the script, which emptied the grape-map sketch the panel copies.
  // Bump when the shape or the contents of a parked sketch change. The cache
  // outlives a reload_autopilot — it hangs off window, which is the point — so
  // without a version a fixed builder keeps serving the old build's output and
  // the fix looks like it did nothing. That cost a full round of debugging.
  const SKETCH_CACHE_VERSION = 4;
  const parkedSketch = (window.__pureclickZoneSketch = window.__pureclickZoneSketch || {
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
    // What the last entry actually did, so a rehearsal has something to show.
    // fireEntry already measured the lateness and then discarded it.
    latenessMs: null,
    firedAtServer: 0,
    syncMs: 0,
    enterMs: 0,
    clockQuality: "",
    clockOffsetMs: 0,
    enteredVia: "",
    goodsCode: "",
    playSeq: "",
  };

  const clockState = {
    offsetSeconds: 0,
    syncedAt: 0,
    quality: "none",
    samples: 0,
    spreadSeconds: 0,
    syncMs: 0,
    note: "",
  };

  function log(...args) {
    console.log("[NOL Sniper]", ...args);
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
    return Date.now() / 1000 + clockState.offsetSeconds;
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
          clockState.offsetSeconds = fallbackOffset;
          clockState.syncedAt = Date.now();
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
      clockState.offsetSeconds = fallbackOffset;
      clockState.syncedAt = Date.now();
      clockState.quality = "fallback";
      return fallbackOffset;
    }
    const best = Math.max(...observed);
    const spread = best - Math.min(...observed);
    // A spread well under a second means the samples never straddled a boundary,
    // so `best` may still understate. The host's own sync is better in that case.
    if (spread < 0.6 && Number.isFinite(fallbackOffset) && fallbackOffset !== 0) {
      clockState.offsetSeconds = fallbackOffset;
      clockState.quality = "host";
    } else {
      clockState.offsetSeconds = best;
      clockState.quality = "boundary";
    }
    clockState.samples = observed.length;
    clockState.spreadSeconds = spread;
    clockState.syncedAt = Date.now();
    log(`clock ${clockState.quality} offset=${clockState.offsetSeconds.toFixed(3)}s n=${observed.length} spread=${spread.toFixed(3)}s`);
    return clockState.offsetSeconds;
  }

  async function waitUntilServerUnix(targetUnix) {
    while (serverTimeUnix() < targetUnix) {
      const remainingMs = (targetUnix - serverTimeUnix()) * 1000;
      if (remainingMs <= 4) {
        while (serverTimeUnix() < targetUnix) {
          /* spin */
        }
        return;
      }
      await sleep(Math.min(20, remainingMs - 4));
    }
  }

  function updateOverlay(message, tone = "info") {
    seatState.message = String(message || "").replace(/<br\s*\/?>/gi, " · ");
    let root = document.getElementById("pureclick-overlay");
    if (!root) {
      root = document.createElement("div");
      root.id = "pureclick-overlay";
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
    root.innerHTML = `<strong style="color:${colors[tone] || colors.info}">스나이퍼</strong><br>${message}`;
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
  function currentPlaySeqFromDom() {
    const tally = new Map();
    for (const node of collectSeatCircles()) {
      const key = seatFromFiber(node)?.blockKey;
      if (!key) continue;
      const seq = String(key).split(":")[0];
      if (!seq) continue;
      tally.set(seq, (tally.get(seq) || 0) + 1);
    }
    let best = null;
    let bestCount = 0;
    for (const [seq, count] of tally) {
      if (count > bestCount) {
        best = seq;
        bestCount = count;
      }
    }
    // A handful of circles is not evidence; a drawn 구역 is.
    return bestCount >= 3 ? best : null;
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
    const ctx = readInterparkContext() || {};
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
  const CAPTCHA_JUNK = /^(NOL|LOGO|TICKET|INTERPARK|YANOLJA|CAPTCHA|IMAGE|PURECLICK)$/;

  // Only the Interpark modal title. Never "보안문자" — that string is in our
  // own toast, and matching it made the sniper wait forever after a manual solve.
  function isCaptchaPageCopy(text) {
    return /화면의\s*문자를\s*입력해주세요|문자를\s*입력해주세요/.test(String(text || ""));
  }

  function isSniperOverlay(node) {
    return Boolean(node && (node.id === "pureclick-overlay" || node.closest?.("#pureclick-overlay")));
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
    const overlay = document.getElementById("pureclick-overlay");
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

  function isVisible(node) {
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
      if (!isVisible(node)) continue;
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
      if (!isVisible(node)) continue;
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

  // After 선택 완료, Interpark shows 취소/환불 안내. Clicking 확인하고 예매하기
  // is what actually advances to the price step. Navigating to ?step=price
  // while this modal is up makes the sniper think checkout started.
  async function confirmPostSelectNotices({ tries = 40 } = {}) {
    for (let attempt = 0; attempt < tries; attempt += 1) {
      if (!bookingNoticeVisible()) return true;
      if (dismissBookingNotices()) {
        updateOverlay("환불 안내 확인 버튼 클릭…", "info");
        await sleep(350);
        continue;
      }
      await sleep(200);
    }
    return !bookingNoticeVisible();
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
      const net = window.__pureclickLastSeatNet || {};
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
    const net = window.__pureclickLastSeatNet || {};
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
      const net = window.__pureclickLastSeatNet || {};
      if ((net.preselectAt || 0) >= since) {
        if (net.preselectOk === false) return false;
        sawPreselect = true;
        await sleep(quietMs);
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
      const held = [...seatState.heldSeatIds];
      if (held.length) {
        await releasePreselected(held);
        seatState.heldSeatIds.clear();
      }
      seatState.locked = false;
      seatState.awaitingPayment = false;
      seatState.lastExit = "takenByAnother";
      updateOverlay(`${label} 이미 선점됨 — 다음 자리로`, "warn");
      return { awaitingPayment: false, takenConflict: true };
    }

    dismissSeatTakenDialog();
    dismissSeatErrorDialog();
    if (pageHasSelectedSeats()) clearSelectedSeats();
    await sleep(500);
    const held = [...seatState.heldSeatIds];
    if (held.length) {
      await releasePreselected(held);
      seatState.heldSeatIds.clear();
    }
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
    const holdSince = Date.now() - 5000;
    updateOverlay(`가선점 확인 후 선택 완료…<br>${AUTOPILOT_BUILD}`, "info");
    const idle = await waitForSoftHoldIdle({ since: holdSince, quietMs: 250, timeoutMs: 2500 });
    if (!idle || !pageHasSelectedSeats()) {
      seatState.confirmStarted = false;
      seatState.locked = false;
      seatState.lastExit = "advanceWithNoSeat";
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
      updateOverlay("선택 완료를 직접 눌러 주세요", "warn");
      return { awaitingPayment: false, awaitingManualConfirm: true };
    }

    const outcome = await waitForPageSelectOutcome({ since, timeoutMs: 5000 });
    traceCall("confirmSelectOutcome", null, outcome);
    if (outcome.ok === false) {
      seatState.confirmStarted = false;
      return recoverFailedConfirm(seatState.lastSeat || "", outcome.via);
    }

    seatState.lastExit = "reservedUserContinues";
    updateOverlay(
      `예약 요청 ${seatState.lastSeat || ""}<br>환불 안내부터는 직접 · ${AUTOPILOT_BUILD}`,
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
    dismissBlockingDialogs();
    setCaptchaReport("waiting", "예매 창에서 6자리를 입력하세요");
    updateOverlay("보안문자 — 예매 창에서 직접 입력하세요<br>통과하면 바로 이어서 진행합니다", "warn");

    while (Date.now() < deadline) {
      dismissBlockingDialogs();
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


  function clickByExactText(labels) {
    const nodes = [...document.querySelectorAll("button, a, label, span, li, div, p")];
    for (const label of labels) {
      const hit = nodes.find((node) => node.childElementCount <= 2 && (node.textContent || "").trim() === label);
      if (hit) {
        hit.click();
        return true;
      }
    }
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

  function fillLabeledInput(pattern, value) {
    if (!value) return false;
    const labels = [...document.querySelectorAll("label, th, dt, span, p, div")];
    for (const label of labels) {
      if (!pattern.test(label.textContent || "")) continue;
      const root = label.closest("tr, li, div, dl, label") || label.parentElement;
      const field = root?.querySelector("input, select");
      if (!field || field.offsetParent === null) continue;
      if (field.tagName === "SELECT") {
        const option = [...field.options].find((item) => item.text.includes(value) || item.value === String(value));
        if (option) field.value = option.value;
      } else {
        field.focus();
        field.value = value;
      }
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    return false;
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

  function isCheckoutAdvanceSafe(el) {
    const text = (el.textContent || "").trim();
    if (!ADVANCE_BUTTON.test(text)) return false;
    if (COMMIT_BUTTON.test(text)) return false;
    const root = el.closest("[role=dialog], aside, section, article, div") || el.parentElement;
    const around = root?.innerText || "";
    if (/구매하실\s*좌석을\s*선택해주세요|좌석을\s*선택해주세요|선점\s*실패|오류/.test(around)) return false;
    return true;
  }

  /** Tick required consent boxes; optional marketing ones are left alone. */
  function acceptRequiredAgreements() {
    for (const box of document.querySelectorAll('input[type="checkbox"]')) {
      if (box.checked || box.disabled || box.offsetParent === null) continue;
      const label = `${box.getAttribute("name") || ""} ${box.id || ""} ${
        box.closest("label")?.textContent || box.parentElement?.textContent || ""
      }`;
      if (/광고|마케팅|선택/.test(label)) continue;
      if (/동의|필수|약관|확인/.test(label)) box.click();
    }
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
    if (!response.ok && response.status !== 401) {
      throw new Error(`waiting HTTP ${response.status}`);
    }
    return data;
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

  async function enterFromNolPage(arm) {
    // Past here we are pressing the page's own buttons rather than talking to
    // the queue API, and those only work once the show is actually open.
    await waitUntilServerUnix(Number(arm.target_server_unix));

    if (clickFirstMatching(/^예매하기$|^본인인증 후 예매하기$/)) {
      await sleep(250);
      const modalBook = [...document.querySelectorAll("button, a")].find((el) =>
        el.getAttribute("data-testid") === "modal-booking-button" || /^예매하기$/.test((el.textContent || "").trim()),
      );
      if (modalBook && modalBook.offsetParent !== null) {
        modalBook.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
        return { clicked: true };
      }
      return { clicked: true };
    }
    if (arm.place_code) {
      location.href = buildSsoUrl(arm);
      return { sso: true };
    }
    throw new Error("NOL 예매하기 버튼을 찾지 못했습니다. 로그인·본인인증을 확인하세요.");
  }

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
    const target = Number(arm?.target_server_unix);
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
    const target = Number(arm.target_server_unix) || serverTimeUnix();
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
    while (serverTimeUnix() < giveUpAt) {
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
      } catch (error) {
        lastError = error;
        noteWaitingAttempt(sentOffsetMs, `오류 ${String(error).slice(0, 40)}`,
                           performance.now() - startedPerf);
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
    const latenessMs = (firedAt - arm.target_server_unix) * 1000;
    log("entry fire", { firedAt, target: arm.target_server_unix, latenessMs });
    armState.latenessMs = latenessMs;
    armState.firedAtServer = firedAt;
    armState.enteredVia = "";
    armState.lastError = "";
    // The round it actually used. A rehearsal that reports 회차 017 while the
    // 예매 창 shows 022 has found the bug for you.
    const live = withLivePlaySeq(getInitData());
    armState.goodsCode = String(arm.goods_code || live?.goods?.goodsCode || "");
    armState.playSeq = String(live?.playSeq?.playSeq || live?.playSeq || arm.play_seq || "");

    if (arm.dry_run) {
      armState.enteredVia = "dry-run";
      updateOverlay(`테스트 진입 ${latenessMs >= 0 ? "+" : ""}${latenessMs.toFixed(2)} ms`, "ok");
      return { dryRun: true, latenessMs };
    }

    await waitForCaptchaClear();

    if (isNolProductPage()) {
      updateOverlay("NOL 예매 진입…", "info");
      if (arm.use_waiting_api !== false) {
        try {
          const waitingUrl = await acquireWaitingUrl(arm);
          armState.waitingUrl = waitingUrl || "";
          if (waitingUrl === "NP") throw new Error("선예매 인증이 필요합니다 (NP)");
          if (waitingUrl === "BL") throw new Error("비정상 예매로 차단되었습니다 (BL)");
          if (typeof waitingUrl === "string" && /^https?:\/\//i.test(waitingUrl)) {
            armState.enteredVia = "waiting";
            rememberQueueHost(waitingUrl);
            location.href = waitingUrl;
            return { waitingUrl, latenessMs };
          }
          if (waitingUrl === "N") {
            armState.enteredVia = "book";
            return { ...openBookSession(arm), waitingUrl, latenessMs };
          }
        } catch (error) {
          armState.lastError = String(error);
          log("NOL waiting API", error);
        }
      }
      return enterFromNolPage(arm);
    }

    if (isGatesPage()) {
      updateOverlay("게이트 세션 연결 중…", "info");
      return { gates: true, latenessMs };
    }

    let waitingUrl = null;
    if (arm.use_waiting_api !== false && isGoodsPage()) {
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
      updateOverlay("대기열 URL로 진입…", "info");
      rememberQueueHost(waitingUrl);
      location.href = waitingUrl;
      return { waitingUrl, latenessMs };
    }

    if (waitingUrl === "N") {
      updateOverlay("대기열 없음 — BookSession 진입", "info");
      return { ...openBookSession(arm), waitingUrl, latenessMs };
    }

    // The queue API can fail fast — a throw, or a terminal answer — and leave
    // us here while the countdown is still running. The page's own buttons do
    // not work before the open, so wait the rest of it out.
    await waitUntilServerUnix(Number(arm.target_server_unix));

    if (clickFirstMatching(/^예매하기$|^본인인증 후 예매하기$/)) {
      updateOverlay("예매하기 클릭", "warn");
      return { clicked: true, waitingUrl, latenessMs };
    }

    if (arm.place_code) {
      location.href = buildSsoUrl(arm);
      return { sso: true, latenessMs };
    }

    throw new Error(armState.lastError || "대기열 API와 예매하기 모두 실패");
  }

  const QUEUE_HOST_KEY = "pureclick_queue_host_v1";

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
    if (!host || document.querySelector(`link[data-pureclick-preconnect="${host}"]`)) return host;
    try {
      const link = document.createElement("link");
      link.rel = "preconnect";
      link.href = host;
      link.crossOrigin = "anonymous";
      link.dataset.pureclickPreconnect = host;
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

  async function runArmScheduler(arm) {
    if (!arm?.enabled || arm.fired || armState.running) return;
    if (armState.fired) return;

    armState.running = true;
    updateOverlay("서버 시각 동기화 중…", "info");
    // Where the time goes, so "it took too long" can be answered with numbers
    // rather than argued about.
    const syncStarted = performance.now();
    await syncServerClock(Number(arm.offset_seconds || 0));
    armState.syncMs = Math.round(performance.now() - syncStarted);
    armState.clockQuality = clockState.quality;
    armState.clockOffsetMs = Math.round((clockState.offsetSeconds || 0) * 1000);
    armState.queueHost = preconnectQueueHost();
    const remaining = arm.target_server_unix - serverTimeUnix();
    updateOverlay(`${arm.dry_run ? "테스트 " : ""}대기열 예약<br>${Math.max(0, remaining).toFixed(1)}초`, "info");
    // Stop short of the open by exactly the lead the request loop expects.
    // Waiting out the full deadline here is what made that lead dead code.
    await waitUntilServerUnix(armEntryStartUnix(arm) ?? Number(arm.target_server_unix));

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
    } finally {
      armState.running = false;
    }
  }

  async function maybeReenter() {
    const seat = loadSeatConfig();
    const arm = loadArmConfig();
    if (!seat.reentry || !arm?.enabled) return;
    if (isSeatPage() && getInitData()?.sessionId) return;
    if (armState.reentryTries > 40) return;
    if (isWaitingPage() || isGatesPage()) return;
    if (!(isNolProductPage() || isGoodsPage())) return;
    armState.reentryTries += 1;
    updateOverlay(`재진입 ${armState.reentryTries}회`, "warn");
    try {
      await fireEntry({ ...arm, dry_run: false, fired: false });
    } catch (error) {
      log("reentry failed", error);
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

  function strategyKeys(seat, strategy, seed, centerX, stage = null) {
    // `== null` on purpose: toCandidate normalises to null, but DOM-built and
    // test-built seats simply omit the field. Treating `undefined` as a number
    // yields NaN, and one NaN key makes the whole comparison meaningless.
    const left = seat.posLeft == null ? null : seat.posLeft;
    const mid = centerX == null ? null : centerX;
    // A missing key sorts last, so positioned seats lead and the positionless
    // tail falls through to the rowNo/seatNo chain exactly as it did before.
    const NONE = Number.POSITIVE_INFINITY;
    const near = stageDistance(seat, stage);
    const sideward = left === null || mid === null ? NONE : Math.abs(left - mid);
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

      ranked.push({ ...seat, _rank: rank, _posA: keyA, _posB: keyB });
    }
    const sorted = ranked.sort((a, b) => {
      // Grade preference still wins outright — a strategy only decides which
      // seat *within* a grade tier.
      if (a._rank !== b._rank) return a._rank - b._rank;
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
      centerX: seatState.mapCenterX ?? null,
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

  function venueCenterX(blocks) {
    const values = [];
    for (const block of blocks || []) {
      for (const seat of block.seats || []) {
        const left = numOrNull(seat.posLeft);
        if (left !== null) values.push(left);
      }
    }
    if (!values.length) return null;
    values.sort((a, b) => a - b);
    return values[Math.floor(values.length / 2)];
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

  function markSeatTaken(seatInfoId) {
    if (!seatInfoId) return;
    seatState.takenUntil.set(String(seatInfoId), Date.now() + TAKEN_COOLDOWN_MS);
    seatState.takenConflicts = (seatState.takenConflicts || 0) + 1;
  }

  function seatInCooldown(seatInfoId) {
    const until = seatState.takenUntil.get(String(seatInfoId));
    if (!until) return false;
    if (Date.now() >= until) {
      seatState.takenUntil.delete(String(seatInfoId));
      return false;
    }
    return true;
  }

  function sweepTakenCooldowns() {
    // 취켓팅 runs unbounded, so this map would otherwise grow for the whole
    // sitting.
    const now = Date.now();
    for (const [id, until] of seatState.takenUntil) {
      if (now >= until) seatState.takenUntil.delete(id);
    }
  }

  // Which 구역 is open, measured rather than remembered.
  //
  // seatState.blockEntered is only set when *we* opened a block, and the user
  // normally opens it themselves — so every "am I in the right block" test read
  // as false and the switch never fired. That is the ordinary case: sit in one
  // 구역, watch a seat free in another, never reach it. Every rendered seat
  // already carries props.blockKey, so the answer is in the DOM.
  function blockKeyForSeatId(seatInfoId) {
    // The rendered seat does not always carry its block: measured on a live
    // venue, all 273 drawn seats came back with blockKey undefined, which
    // silently disabled every "which 구역 am I in" test. seatMeta knows, and we
    // already hold it per block, so look the seat up there.
    const wanted = String(seatInfoId);
    for (const block of seatState.lastBlocks || []) {
      for (const seat of block.seats || []) {
        if (String(seat.seatInfoId) === wanted) return String(block.blockKey);
      }
    }
    return null;
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
    // A mapping that has already worked on this show is tried first and alone.
    const order = seatState.blockEntryHypothesis
      ? [seatState.blockEntryHypothesis]
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
    if (window.__pureclickMapPointerWatch) return;
    window.__pureclickMapPointerWatch = true;
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

  // Which seat to travel to, when none is drawn yet. See the note at the call
  // site: reachability beats distance, because the travel costs more than the
  // difference between two seats usually does.
  function aimForCandidates(candidates, openBlock) {
    if (openBlock) {
      const here = candidates.find((seat) => String(seat.blockKey) === String(openBlock));
      if (here) return here;
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

  function seatNodeFor(seatInfoId, readSeat = seatFromFiber) {
    if (!seatInfoId) return null;
    const wanted = String(seatInfoId);
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

  function clickSeatOnMap(seatInfoId) {
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
    const before = selectedSeatCount();
    firePointerSelect(node);
    traceClickAttempt(seatInfoId, node, "dispatched", { before });
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
    try {
      const direct = withSeq(window.__NEXT_DATA__?.props?.pageProps?.initData);
      if (direct) return direct;
    } catch (error) {
      /* fall through */
    }
    try {
      return withSeq(getInitData());
    } catch (error) {
      return null;
    }
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
    for (const block of seatingBlocks(metaBlocks)) {
      const key = String(block?.blockKey || "");
      if (!key) continue;
      for (const seat of block.seats || []) {
        const x = numOrNull(seat.posLeft);
        const y = numOrNull(seat.posTop);
        // Include sold seats too — the grey dots are what make the house shape.
        if (x == null || y == null) continue;
        points.push({ k: key, x, y });
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
  async function mapLimit(items, limit, worker, shouldStop = () => false) {
    const results = [];
    let cursor = 0;
    const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (cursor < items.length) {
        if (shouldStop(results)) return;
        const index = cursor;
        cursor += 1;
        try {
          results.push(await worker(items[index], index));
        } catch (error) {
          log("mapLimit item failed", error);
        }
      }
    });
    await Promise.all(runners);
    return results;
  }

  async function fetchMetaBatch(rawInitData, blockKeys) {
    const initData = withLivePlaySeq(rawInitData);
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const params = new URLSearchParams({
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
      bizCode: initData.bizCode || "WEBBR",
    });
    for (const blockKey of blockKeys) params.append("blockKeys", blockKey);
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
    return data.filter((entry) => typeof entry === "string").map(decodeStatusMask);
  }

  async function fetchSeatStatus(rawInitData, blockKeys = []) {
    const initData = withLivePlaySeq(rawInitData);
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const params = new URLSearchParams({
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
      bizCode: initData.bizCode || "WEBBR",
    });
    for (const blockKey of blockKeys) params.append("blockKeys", blockKey);
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
  // repeat that. Read through PureClick.status().seat.trace.
  const TRACE_LIMIT = 24;
  // Parked on `window`, not in this closure. `reload_autopilot` re-runs the
  // whole IIFE, which is how every fix gets deployed — with the array declared
  // here, each deployment wiped the evidence from the attempt that motivated it.
  const trace = (window.__pureclickTrace = window.__pureclickTrace || []);

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
  function notePageSeatNet(label, status, text) {
    const net = (window.__pureclickLastSeatNet = window.__pureclickLastSeatNet || {});
    const at = Date.now();
    const body = String(text || "");
    const name = String(label || "");
    if (/preselect/i.test(name)) {
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
    window.__pureclickNotePageSeatNet = notePageSeatNet;
    window.__pureclickNotePageSeatStatus = notePageSeatStatus;
    // v5+ records select/preselect outcomes. Older hooks only traced; rebuild once.
    if (window.__pureclickNetWatchNotes) return;
    window.__pureclickNetWatchNotes = true;
    window.__pureclickNetWatch = true;

    const nativeFetch = window.__pureclickNativeFetch || window.fetch;
    if (typeof nativeFetch === "function") {
      window.__pureclickNativeFetch =
        typeof nativeFetch.bind === "function" ? nativeFetch.bind(window) : nativeFetch;
      window.fetch = async function pureclickFetch(input, init) {
        const url = String(input?.url || input || "");
        const watched = /\/onestop\/(gql|api\/(seats|seatStatus|seatMeta))/.test(url);
        const response = await window.__pureclickNativeFetch.apply(window, arguments);
        if (!watched) return response;
        try {
          const body = String(init?.body || "").slice(0, 200);
          const text = await response.clone().text();
          const label = (body.match(/mutation\s+(\w+)/) || [])[1] || url.split("?")[0].split("/").pop();
          if (label === "seatStatus") window.__pureclickNotePageSeatStatus?.(url, text);
          window.__pureclickNotePageSeatNet?.(label, response.status, text);
          traceCall(`page:${label}`, body, `HTTP ${response.status} ${text}`);
        } catch {
          /* opaque or already consumed */
        }
        return response;
      };
    }

    // The onestop SPA talks through axios, which is XMLHttpRequest — a fetch
    // hook alone sees none of the page's own booking calls.
    if (!window.__pureclickXhrHooked) {
      window.__pureclickXhrHooked = true;
      const nativeOpen = XMLHttpRequest.prototype.open;
      const nativeSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function pureclickOpen(method, url) {
        this.__pureclickUrl = String(url || "");
        return nativeOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function pureclickSend(body) {
        const url = this.__pureclickUrl || "";
        if (/\/onestop\/(gql|api\/(seats|seatStatus|seatMeta))/.test(url)) {
          this.addEventListener("loadend", () => {
            try {
              const sent = String(body || "").slice(0, 200);
              const label = (sent.match(/mutation\s+(\w+)/) || [])[1] || url.split("?")[0].split("/").pop();
              const text = String(this.responseText || "");
              if (label === "seatStatus") window.__pureclickNotePageSeatStatus?.(url, text);
              window.__pureclickNotePageSeatNet?.(label, this.status, text);
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
      throw new Error(`gql HTTP ${response.status}${detail ? ` · ${detail.slice(0, 160)}` : ""}`);
    }
    const payload = await response.json();
    if (payload?.errors?.length) {
      traceCall(name, variables, payload.errors);
      const blockedMs = readGatewayBlock(payload.errors);
      if (blockedMs >= 0) throw gatewayBlockError(blockedMs);
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
    return (meta || []).map((block, index) => ({
      blockKey: block?.blockKey || blockKeys[index] || null,
      seats: block?.seats || [],
      mask: masks[index] || null,
    }));
  }

  function seatIsFree(block, position) {
    if (!block.mask) return false;
    return position < block.mask.length && block.mask[position];
  }

  function countFree(blocks) {
    let total = 0;
    for (const block of blocks || []) {
      for (let index = 0; index < (block.seats || []).length; index += 1) {
        if (block.seats[index]?.isExposable && seatIsFree(block, index)) total += 1;
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

    seatState.showCatalog = catalog;
    if (isNolProductPage()) {
      const total = catalog.grades.reduce((sum, row) => sum + (Number(row.remain) || 0), 0);
      const round = [catalog.play_date, catalog.play_time || catalog.play_seq].filter(Boolean).join(" ");
      updateOverlay(
        `예매판 동기화<br>${catalog.goods_name || catalog.goods_code || "?"}<br>${round || "회차 확인 중"} · 잔여 ${total}석`,
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
  const SEAT_MAP_SETTLE_MS = 100;
  const SEAT_MAP_SETTLE_TRIES = 15;

  // How many seats the page currently holds. It renders the number itself
  // ("선택 좌석 4"), which is the only reading that stays correct once more than
  // one seat is involved.
  function selectedSeatCount() {
    const text = pageTextWithoutOverlay();
    if (/선택한\s*좌석이\s*없습니다/.test(text)) return 0;
    const match = text.match(/선택\s*좌석\s*(\d+)/);
    return match ? Number(match[1]) : -1;
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
      await sleep(SEAT_MAP_SETTLE_MS);
      // The page has already answered — stop waiting on a count that will never
      // arrive. Polling the full 1.5s here is the difference between losing one
      // seat and losing the next one too, and during an open that is the whole
      // game.
      if (seatTakenDialogVisible() || seatErrorDialogVisible()) return false;
      const now = selectedSeatCount();
      if (now >= before + added) return true;
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
        if (clickSeatOnMap(seat.seatInfoId)) clicked += 1;
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
          await sleep(SEAT_MAP_SETTLE_MS);
          if (selectedSeatCount() >= seats.length) {
            registered = true;
            break;
          }
        }
      }

      if (registered) {
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
  async function releasePreselected(seatInfoIds) {
    const ids = [...new Set((seatInfoIds || []).map(String))].filter(Boolean);
    if (!ids.length) return false;
    const query = `mutation BulkDeselectSeats($command: BulkDeselectSeatsCommand!) {
      bulkDeselectSeats(command: $command)
    }`;
    try {
      await gql(query, { command: { seatInfoIds: ids } });
      ids.forEach((id) => seatState.heldSeatIds.delete(id));
      return true;
    } catch (error) {
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
  function readGatewayBlock(payload) {
    const nodes = Array.isArray(payload) ? payload : [payload];
    for (const node of nodes) {
      const extensions = node?.extensions;
      if (!extensions) continue;
      const code = String(extensions.errorCode || "");
      if (code.includes("ABUSE") || extensions.abuseStage === "BLOCKED") {
        return Math.max(0, Number(extensions.retryAfterMs) || 0);
      }
    }
    return -1;
  }

  function gatewayBlockError(retryAfterMs) {
    const seconds = Math.ceil(retryAfterMs / 1000);
    const error = new Error(`GATEWAY_ABUSE_BLOCKED retryAfterMs=${retryAfterMs}`);
    error.gatewayBlockedMs = retryAfterMs;
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
    for (const [id, sawAt] of domAgreeWatch) {
      const node = seatNodeFor(id);
      if (!node) continue;
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

  async function pollFreedSeats(initData, blockKeys, config, { burst = false } = {}) {
    if (!blockKeys.length) return [];
    if (!seatState.lastBlocks?.length) {
      const collected = [];
      for (const batch of chunk(blockKeys, 2)) {
        if (seatState.stopRequested) break;
        collected.push(...(await fetchBlockSeats(initData, batch)));
      }
      seatState.lastBlocks = collected;
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
    const all = seatState.lastBlocks.map((block) => block.blockKey).filter(Boolean);
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
    const perTick = burst
      ? Math.ceil(keys.length / 2)
      : Math.min(CATCH_MAX_REQUESTS_PER_TICK, Math.ceil(keys.length / 2));
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
    const masks = [];
    for (const pair of chunk(batch, 2)) {
      masks.push(...parseSeatStatus(await fetchSeatStatus(initData, pair)));
    }
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
    const freed = [];
    const rect = normalizeWatchRect(config.watch_rect);
    for (let pos = 0; pos < Math.min(previous.length, mask.length); pos += 1) {
      if (!mask[pos] || previous[pos]) continue;
      const seat = block.seats[pos];
      if (!seat?.isExposable || !seat?.seatGrade || !seat?.seatInfoId) continue;
      if (seat.seatGroupId && config.allow_group_seats === false) continue;
      const candidate = toCandidate(seat, block.blockKey);
      if (!seatInWatchRect(candidate, rect)) continue;
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

  function seatStatusSummary() {
    return {
      seat: {
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
        blockedForMs: Math.max(0, (seatState.blockedUntil || 0) - Date.now()),
        trace: trace.slice(-TRACE_LIMIT),
        // Which build the page is actually running. Without it there is no way
        // to tell a reload that silently failed from a command that did nothing.
        build: AUTOPILOT_BUILD,
        traceLen: trace.length,
        clickableNow: seatState.clickableNow || 0,
        statusFailures: seatState.statusFailures || 0,
        watchRectIgnored: Boolean(seatState.watchRectIgnored),
        // Racing other buyers is normal and should read as normal. Without
        // these a busy open looks identical to a stuck macro.
        takenConflicts: seatState.takenConflicts || 0,
        cooldownSeats: seatState.takenUntil.size,
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
  function liveSignature(live) {
    if (!live.length) return "0";
    return `${live.length}:${live[0].seatInfoId}:${live[live.length - 1].seatInfoId}`;
  }

  function freeSeatCount() {
    const blocks = seatState.polledBlocks?.size
      ? (seatState.lastBlocks || []).filter((block) =>
          seatState.polledBlocks.has(String(block.blockKey)),
        )
      : seatState.lastBlocks || [];
    return blocks.reduce(
      (total, block) => total + (block.mask || []).filter(Boolean).length,
      0,
    );
  }

  // Why nothing is happening, in words. The old text reported a block cursor
  // that is always 0 on a two-block venue and said nothing about whether any
  // seat was even a candidate.
  function catchStatusText(live, free, pollMs, liveExhausted) {
    const lines = [`취소표 감시 중 · ${pollMs}ms 간격`];
    if (!free) {
      lines.push("빈 좌석 0석 — 취소표가 나오면 즉시 잡습니다");
    } else if (!live.length) {
      lines.push(`빈 좌석 ${free}석 있으나 <b>내 조건에 맞는 등급이 없음</b>`);
      lines.push("좌석 조건에서 등급 선택을 늘려 보세요");
    } else if (liveExhausted) {
      lines.push(`후보 ${live.length}석 · ${CATCH_LIVE_TRIES}회 모두 남이 먼저 가져감`);
      lines.push("좌석이 바뀌면 자동으로 다시 시도합니다");
    } else {
      lines.push(`빈 좌석 ${free}석 · 후보 ${live.length}석`);
    }
    return lines.join("<br>");
  }

  async function runSeatAutopilot(config, { probe = false, catchMode = false, userInitiated = false } = {}) {
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
    const runGen = (window.__pureclickRunGen = (window.__pureclickRunGen || 0) + 1);
    seatState.lastExit = "started";
    if (userInitiated) seatState.haltedByUser = false;
    else if (seatState.haltedByUser) {
      seatState.lastExit = "haltedByUser";
      return;
    }

    // Starting during a gateway block cannot succeed and risks extending it.
    const blockedFor = (seatState.blockedUntil || 0) - Date.now();
    if (blockedFor > 0 && !probe) {
      const seconds = Math.ceil(blockedFor / 1000);
      seatState.lastError = `접속 차단 중 — ${seconds}초 후에 다시 시도하세요.`;
      updateOverlay(`접속 차단 중<br>${seconds}초 남음`, "error");
      return;
    }

    if (seatState.locked) {
      if (emptyPriceStepVisible() || seatSelectionEmpty()) {
        recoverEmptyPriceStep();
        seatState.running = false;
        return;
      }
      if (bookingNoticeVisible() || !seatState.awaitingPayment) {
        updateOverlay("선점된 좌석 — 안내 확인 후 결제 단계로 이동합니다", "info");
        const advanced = await advanceAfterSeatLock(config);
        if (advanced?.noSeat || advanced?.recovered) seatState.locked = false;
        seatState.running = false;
        return;
      }
      updateOverlay("이미 좌석을 선점했습니다. 결제 화면을 확인하세요.", "ok");
      return;
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
    const askedMs = Number(config.speed_ms || config.poll_ms || 0);
    const pollMs = isCatch
      ? Math.max(CATCH_MIN_POLL_MS, askedMs > 0 ? askedMs : CATCH_MIN_POLL_MS)
      : Number(config.speed_ms || config.poll_ms || 100);
    const quantity = Math.max(1, Number(config.quantity) || 1);

    seatState.running = true;
    seatState.stopRequested = false;
    seatState.confirmStarted = false;
    seatState.attempts = 0;
    seatState.lastError = "";
    seatState.discoveredBlocks = null;
    seatState.statusFailures = 0;
    seatState.catchLiveTries = 0;
    seatState.catchLiveSignature = "";
    seatState.pageFreed.length = 0;
    seatState.pageStatusSeen = 0;
    seatState.pageStatusFreed = 0;
    seatState.observedTickMs = 0;
    seatState.mapMoves = {};
    seatState.triggerActedAt = 0;
    seatState.triggerBursts = 0;
    seatState.domScans = 0;
    seatState.domScanMs = 0;
    seatState.domScanWorstMs = 0;
    seatState.consecutiveRejects = 0;
    seatState.skippedByMap = 0;
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
      if (stranded.length) await releasePreselected(stranded);
      seatState.heldSeatIds.clear();
    }
    updateOverlay(
      probe
        ? `좌석 프로브… · ${AUTOPILOT_BUILD}`
        : isCatch
          ? `취켓팅 감시 중… · ${AUTOPILOT_BUILD}`
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

    await waitForSeatMapReady({ allowRefundConfirm: !probe });

    async function refreshCandidates() {
      const remains = await fetchGradeRemains(initData);
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
    //
    // Done once, at the moment 감시 시작 is pressed, which is a deliberate
    // action rather than the macro moving the map under you while you browse.
    if (isCatch && !config.auto_assign) {
      try {
        const rect = normalizeWatchRect(config.watch_rect);
        const watchedKeys = rect
          ? blocksInWatchRect(seatState.lastBlocks || [], rect) || statusBlockKeys
          : statusBlockKeys;
        const openNow = currentOpenBlock();
        const target = (seatState.discoveredBlocks || []).find(
          (block) => watchedKeys.includes(String(block.blockKey)),
        );
        if (target && openNow !== String(target.blockKey)) {
          updateOverlay(
            `감시할 구역 ${target.selfDefineBlock || target.blockKey} 여는 중…`,
            "info",
          );
          if (openNow) await leaveBlockToVenue();
          const entered = await enterBlockForSeats(target);
          traceCall("prepareWatch", target.blockKey, entered);
          if (entered.ok) await fitBlockToView();
        } else if (target) {
          // Already in the right 구역 — make sure all of it is mounted.
          await fitBlockToView();
        }
      } catch (error) {
        // Preparation is an optimisation; the run still works without it.
        traceCall("prepareWatch", null, { error: String(error).slice(0, 160) });
      }
    }

    while (seatState.attempts < maxAttempts && !seatState.locked && !seatState.stopRequested) {
      if (runWasSuperseded(runGen)) {
        seatState.lastExit = "superseded";
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
        const scoped =
          (watchRect && blocksInWatchRect(seatState.lastBlocks || [], watchRect)) || statusBlockKeys;
        // Anything the 예매 창's own traffic already showed opening is taken
        // first. It cost us no request, so it is not subject to the budget
        // that paces the sweep, and it is as fresh as the page itself — which
        // on the block the user is looking at beats waiting for our cursor to
        // come round to it.
        const tickStartedPerf = performance.now();
        const overheard = seatState.pageFreed.splice(0);
        const burst = triggerFired();
        if (burst) seatState.triggerBursts = (seatState.triggerBursts || 0) + 1;
        const freed = overheard.length
          ? overheard
          : await pollFreedSeats(initData, scoped, config, { burst });
        if (overheard.length) seatState.lastFreedVia = "page";
        else if (freed.length) seatState.lastFreedVia = "poll";
        // What a tick actually costs, rather than what the sleep alone says.
        // Smoothed, because one slow request should not rewrite the estimate.
        if (!overheard.length) {
          const spent = performance.now() - tickStartedPerf + pollMs;
          seatState.observedTickMs = seatState.observedTickMs
            ? Math.round(seatState.observedTickMs * 0.7 + spent * 0.3)
            : Math.round(spent);
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
          // decides whether the seat is ours.
          seatState.freedAtPerf = performance.now();
          candidates = rankCandidates(freed, gradeOrder, blockKeys, pickerOptions(config, { isCatch: true }));
          seatState.catchLiveTries = 0;
        } else if (live.length && !liveExhausted) {
          candidates = live;
        } else {
          seatState.attempts += 1;
          updateOverlay(catchStatusText(live, freeSeatCount(), pollMs, liveExhausted), "info");
          await sleep(pollMs);
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
        if (!isCatch && (seatState.lastBlocks || []).length && freeSeatCount() === 0) {
          seatState.lastExit = "soldOut";
          seatState.lastError =
            "지금 빈 좌석이 없습니다. 취소표를 기다리려면 [감시 시작]으로 취켓팅을 켜 두세요.";
          updateOverlay("빈 좌석 없음 — 취켓팅으로 기다리세요", "warn");
          break;
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
      if (!clickable.length && !config.auto_assign) {
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
        const openBlock = currentOpenBlock();
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
              clickable = clickableAmong(candidates);
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
                clickable = clickableAmong(candidates);
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
          // Someone else holds these. Drop them for good and take the next
          // ones; the bitmap said they were free a moment ago, so the map is
          // simply behind the server.
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
            seatState.blockedUntil = Date.now() + (error.gatewayBlockedMs || 0);
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
    seatState.running = false;
  }

  function compactDate(value) {
    if (!value) return null;
    const digits = String(value).replace(/\D/g, "");
    return digits.length >= 8 ? digits.slice(0, 8) : null;
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
    if (initData?.goods?.goodsCode) context.goods_code = initData.goods.goodsCode;
    if (initData?.goods?.goodsName) context.goods_name = initData.goods.goodsName;
    if (initData?.goods?.placeCode) context.place_code = initData.goods.placeCode;
    if (initData?.playSeq?.playSeq) context.play_seq = initData.playSeq.playSeq;
    if (initData?.playSeq?.playDate) context.play_date = compactDate(initData.playSeq.playDate);
    if (initData?.playSeq?.playTime) context.play_time = normalizePlayTime(initData.playSeq.playTime);

    const payload = flightPayload();
    if (!context.goods_name) context.goods_name = payloadString(payload, "goodsName");
    if (!context.place_code) context.place_code = payloadString(payload, "placeCode", /\d+/);
    // Do not fall back to playStartDate on the product page — that is the run's
    // first night and is why the panel showed 0석 while Aug 28 had seats.
    if (!context.play_date && context.page !== "nol") {
      context.play_date = compactDate(payloadString(payload, "playStartDate"));
    }
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
    return Boolean(arm?.auto_seats_after_entry || seat.auto_seats_after_entry);
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

  function bootRoute() {
    const arm = loadArmConfig();
    const seat = loadSeatConfig();

    if ((isNolProductPage() || isGoodsPage()) && arm?.enabled && !arm.fired) {
      runArmScheduler(arm);
      return;
    }

    if (isNolProductPage()) {
      void ensureProductCatalog();
      return;
    }

    if (isSeatPage()) {
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
      const autoRun = seat.enabled && shouldAutoSeatsAfterEntry();
      if (autoRun) {
        runSeatAutopilot(seat, { catchMode: false });
      }
    }
  }

  function boot() {
    const seat = loadSeatConfig();
    saveSeatConfig(seat);
    window.PureClick = {
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
        seatInCooldown,
        sweepTakenCooldowns,
        state: seatState,
        TAKEN_COOLDOWN_MS,
        calibrateVenueToScreen,
        blockClickPoint,
        blockAbsoluteExtent,
        overlayFit,
        recoverFailedConfirm,
        dismissAnyBlockingOverlay,
        describeBlockingOverlay,
        currentOpenBlock,
        blockKeyForSeatId,
        currentPlaySeqFromDom,
        withLivePlaySeq,
        adoptBlocksKey,
        stagePoint,
        ENTRY_LEAD_MS,
        armEntryStartUnix,
        waitingIntervalAt,
        WAITING_POLL_SHAPE,
        describeWaitingAnswer,
        noteWaitingAttempt,
        WAITING_LOG_LIMIT,
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
        triggerFired,
        aimForCandidates,
        noteMapMove,
        notePageSeatStatus,
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
      auditBlocks: () => auditBlocks(),
      sketchCache: { parkSketch, parkedSketchFor, restoreParkedSketch, currentSketchKey },
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
        return restoreParkedSketch(seatState.showCatalog);
      },
      status: seatStatusSummary,
      runEntry: () => runArmScheduler(loadArmConfig()),
      runSeats: () => runSeatAutopilot(loadSeatConfig(), { userInitiated: true }),
      runCatch: () =>
        runSeatAutopilot({ ...loadSeatConfig(), mode: "catch" }, { catchMode: true, userInitiated: true }),
      probeSeats: () => runSeatAutopilot(loadSeatConfig(), { probe: true, userInitiated: true }),
      stopAll() {
        window.__pureclickRunGen = (window.__pureclickRunGen || 0) + 1;
        seatState.running = false;
        seatState.stopRequested = true;
        // Sticky: bootRoute must not restart it on the next URL change.
        seatState.haltedByUser = true;
        armState.running = false;
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
        resolveSeatType,
        decodeStatusMask,
        parseSeatStatus,
        readUnselectable,
        readGatewayBlock,
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
      resetArm() {
        const arm = loadArmConfig();
        if (!arm) return null;
        const next = { ...arm, fired: false };
        saveArmConfig(next);
        armState.fired = false;
        armState.reentryTries = 0;
        return next;
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
  window.__pureclickWatchId = setInterval(() => {
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
    maybeReenter();
  }, 400);
})();
