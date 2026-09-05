/**
 * Journey 5: 취켓팅 catches a seat → holds it → the user lets it go → the engine
 * stays PAUSED until 감시 시작. Plus the two other ways the page is handed back:
 * a real pointer press on a seat while the watch runs, and the price step.
 *
 * This drives the real focus poller against a stubbed seatStatus feed — the
 * workers detect the 0->1 themselves and pressSequence fires the pointer
 * events — so what is asserted is the engine's own behaviour, not a re-telling
 * of it. The site's side (preselect / select answers, the sidebar count) is
 * played by the harness.
 *
 * Run: node tests/journey_hold_lifecycle.mjs          (readable)
 *      node tests/journey_hold_lifecycle.mjs --json   (for pytest)
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(process.env.NOLSNIPER_AUTOPILOT || resolve(here, "../browser/nolsniper_autopilot.js"), "utf8");
const noop = () => {};

// ---- a block with 60 seats drawn --------------------------------------------

const BLOCK_KEY = "001:103";
const SEATS = 60;
let fiberSeq = 0;
function circleNode(seat) {
  const node = {
    tagName: "circle",
    isConnected: true,
    style: {},
    dataset: {},
    parentNode: null,
    classList: { contains: () => false, add: noop, remove: noop },
    getAttribute: (name) => (name === "r" ? "3" : ""),
    setAttribute: noop,
    getBoundingClientRect: () => ({ left: 100, top: 100, width: 6, height: 6, right: 106, bottom: 106 }),
    dispatchEvent: () => true,
    querySelectorAll: () => [],
    closest: () => null,
  };
  const inner = { memoizedProps: {}, return: null };
  const outer = { memoizedProps: { seat, blockKey: undefined, isSelected: false, isDisabled: false }, return: blockFiber };
  inner.return = outer;
  node.__fiberProps = outer.memoizedProps;
  node[`__reactFiber$${(fiberSeq += 1).toString(36)}`] = inner;
  return node;
}
// The components above the circles, as the bundle has them: a block
// component carrying onSeatClick + seatMeta + blockKey, and the page root
// carrying seatSelectHandler. What the handlers are handed is recorded, and
// the page's side (optimistic cart, PreselectSeat answer) is played back.
const handlerCalls = [];
let handlerAnswers = true;
const rootFiber = {
  memoizedProps: {
    goods: { isInterlocking: false },
    seatSelectHandler: (select, seat, blockKey, skipNetwork, _unused, group) => {
      handlerCalls.push({ kind: "root", select, seat, blockKey, skipNetwork, group, at: performance.now() });
      if (!handlerAnswers) return;
      setCart(cartCount + 1);                     // optimistic, before the network
      setTimeout(() => {                          // the page's own PreselectSeat answers
        sandbox.window.__nolsniperNotePageSeatNet?.("PreselectSeat", 200, '{"data":{"preselectSeat":true}}', { at: Date.now(), perf: performance.now() });
      }, 8);
    },
  },
  return: null,
};
const blockFiber = { memoizedProps: { onSeatClick: (seat, isSelected, blockKey) => handlerCalls.push({ kind: "block", seat, isSelected, blockKey }), blockKey: BLOCK_KEY, seatMeta: [] }, return: rootFiber };
const seats = [];
for (let s = 0; s < SEATS; s += 1) {
  seats.push({
    seatInfoId: `S${s + 1}`, seatGrade: "1", seatGradeName: "R석",
    rowNo: String(Math.floor(s / 10) + 1), seatNo: String((s % 10) + 1),
    posLeft: 100 + (s % 10) * 8, posTop: 100 + Math.floor(s / 10) * 8,
    isExposable: true, seatGroupId: null,
  });
}
const block = { blockKey: BLOCK_KEY, selfDefineBlock: "1구역", seats, mask: seats.map(() => false) };
const circles = seats.map((seat) => circleNode(seat));

// The feed: one hex string, four seats a character, high bit first.
const freeSet = new Set();
function hexMask() {
  let out = "";
  for (let c = 0; c < SEATS / 4; c += 1) {
    let v = 0;
    for (let b = 0; b < 4; b += 1) if (freeSet.has(c * 4 + b)) v |= 1 << (3 - b);
    out += v.toString(16);
  }
  return out;
}

// ---- the page's side ---------------------------------------------------------

let cartCount = 0;
let sidebarText = "선택한 좌석이 없습니다";
const setCart = (n) => { cartCount = n; sidebarText = n > 0 ? `선택 좌석 ${n}` : "선택한 좌석이 없습니다"; };
let confirmPresses = 0;
const confirmButton = {
  tagName: "button", isConnected: true, disabled: false, style: {}, value: "", textContent: "선택 완료",
  classList: { contains: () => false, add: noop, remove: noop },
  getAttribute: () => "", setAttribute: noop,
  getBoundingClientRect: () => ({ left: 10, top: 10, width: 120, height: 40, right: 130, bottom: 50 }),
  click: () => { confirmPresses += 1; },
  dispatchEvent: () => true, querySelectorAll: () => [], closest: () => null,
};
const sidebarNode = {
  tagName: "div", isConnected: true, style: {},
  classList: { contains: () => false, add: noop, remove: noop },
  getAttribute: () => "", setAttribute: noop,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 200, height: 40, right: 200, bottom: 40 }),
  querySelectorAll: () => [], closest: () => null,
  get innerText() { return sidebarText; },
};
const mapRoot = {
  tagName: "svg", isConnected: true, style: {},
  classList: { contains: () => false, add: noop, remove: noop },
  getAttribute: () => "", setAttribute: noop,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 900, right: 1200, bottom: 900 }),
  querySelectorAll: (sel) => (/circle/.test(sel) ? circles : []),
  querySelector: () => null,
  closest: () => null,
  appendChild: noop,
};
for (const c of circles) c.parentNode = mapRoot;

// MutationObserver that can be driven: observers register per target, and
// bodyNode.appendChild delivers a childList record the way the DOM would.
const observers = [];
class MutationObserverStub {
  constructor(cb) { this.cb = cb; this.target = null; }
  observe(target, options) { this.target = target; this.options = options; observers.push(this); }
  disconnect() { const i = observers.indexOf(this); if (i >= 0) observers.splice(i, 1); }
}
const dispatched = [];
const bodyNode = {
  tagName: "body", isConnected: true, style: {}, children: [],
  classList: { contains: () => false, add: noop, remove: noop },
  getAttribute: () => "", setAttribute: noop,
  contains: (node) => bodyNode.children.includes(node),
  appendChild(node) {
    bodyNode.children.push(node);
    node.isConnected = true;
    const record = { type: "childList", target: bodyNode, addedNodes: [node], removedNodes: [] };
    for (const o of observers.slice()) if (o.target === bodyNode) queueMicrotask(() => o.cb([record]));
  },
  querySelectorAll: (sel) => document_.querySelectorAll(sel),
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 900, right: 1200, bottom: 900 }),
  get innerText() { return `${sidebarText}\n선택 완료`; },
};
const listeners = { pointerdown: [] };
const document_ = {
  documentElement: mapRoot,
  head: mapRoot,
  readyState: "complete",
  createElement: () => ({ style: {}, setAttribute: noop, appendChild: noop, remove: noop, classList: { add: noop, remove: noop, contains: () => false } }),
  getElementById: () => null,
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  elementFromPoint: () => null,
  get body() { return bodyNode; },
  visibilityState: "visible",
  querySelector(sel) {
    if (/seatMap|SeatMap|placeImg/.test(sel) || sel === "svg") return mapRoot;
    return null;
  },
  querySelectorAll(sel) {
    if (sel === "circle.js-seat") return circles;
    if (/circle/.test(sel)) return [];
    if (/^button/.test(sel) || sel.includes("role=button")) return [confirmButton];
    if (sel === "div,section,aside,p,span") return [sidebarNode];
    return [];
  },
};

// The feed, with every request stamped so the cadence can be read back.
const sent = [];
let feedRtt = 10;
const sandbox = {
  console: { log: noop, warn: noop, error: noop },
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  fetch: (url) =>
    new Promise((res) => {
      if (/seatStatus/.test(String(url))) sent.push(performance.now());
      setTimeout(() => res({
        ok: true, status: 200, headers: { get: () => null },
        json: async () => ({ data: [hexMask()] }),
        text: async () => "",
        clone() { return this; },
      }), feedRtt);
    }),
  location: {
    href: "https://tickets.interpark.com/onestop/seat", pathname: "/onestop/seat",
    hostname: "tickets.interpark.com", search: "", origin: "https://tickets.interpark.com",
    assign: noop, replace: noop, reload: noop,
  },
  URL, URLSearchParams, MessageChannel,
  MouseEvent: class { constructor(t, i = {}) { this.type = t; Object.assign(this, i); } },
  PointerEvent: class { constructor(t, i = {}) { this.type = t; Object.assign(this, i); } },
  KeyboardEvent: class { constructor(t, i = {}) { this.type = t; Object.assign(this, i); } },
  MutationObserver: MutationObserverStub,
  Event: class { constructor(type) { this.type = type; } },
  dispatchEvent: (event) => { dispatched.push({ type: event.type, at: performance.now() }); return true; },
  XMLHttpRequest: class { open() {} send() {} addEventListener() {} },
  open: () => null, close: noop,
  sessionStorage: { length: 0, key: () => null, getItem: () => null, setItem: noop, removeItem: noop },
  localStorage: {
    _data: new Map(),
    getItem(k) { return this._data.has(k) ? this._data.get(k) : null; },
    setItem(k, v) { this._data.set(k, String(v)); },
    removeItem(k) { this._data.delete(k); },
  },
  document: document_,
  navigator: { userAgent: "node" },
  performance, Promise, Set, Map,
};
sandbox.window = sandbox; sandbox.globalThis = sandbox; sandbox.self = sandbox; sandbox.top = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "nolsniper_autopilot.js" });

const api = sandbox.window.NOLSniper;
const T = api.__test;
const { seatState, focusPoller } = T;
const CONFIG = { grade_order: ["R석"], quantity: 1, allow_group_seats: false, retry_ms: 100 };
const INIT = { sessionId: "s", goods: { goodsCode: "26099999" }, playSeq: "001" };

// ---- helpers ------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(pred, ms, what) {
  const deadline = performance.now() + ms;
  while (performance.now() < deadline) {
    if (pred()) return true;
    await sleep(5);
  }
  throw new Error(`timed out (${ms}ms) waiting for: ${what}`);
}
const report = { steps: [] };
const checks = [];
function check(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail });
  if (!ok) report.failed = (report.failed || 0) + 1;
}
function liveWatch() {
  Object.assign(seatState, {
    running: true, runMode: "catch", userCatch: true, quietWatch: false,
    stopRequested: false, haltedByUser: false, locked: false, confirmStarted: false,
    catchFocusBlock: BLOCK_KEY, catchFocusCheckedAt: Date.now(), blockedUntil: 0,
    pageFreed: [], pressSequenceBusy: false, fastClickedId: "", fastClickedAt: 0,
    lastBlocks: [block], runBaseline: new Set([BLOCK_KEY]),
  });
  seatState.heldSeatIds.clear();
  sandbox.window.__nolsniperRunGen = (sandbox.window.__nolsniperRunGen || 0) + 1;
  sandbox.window.__nolsniperLastSeatNet = {};
  setCart(0);                                   // a fresh watch starts with an empty cart
}
function requestsSince(t0) { return sent.filter((t) => t >= t0).length; }

// ---- 1. the watch runs: three workers, a gapless stream under the cap -------

liveWatch();
// The observer-maintained circle index the real watch attaches on the seat
// map (watchSeatMap runs before the first tick); without it every lookup is
// a scan of the 구역, which is not the path being measured.
check("the seat-map index attaches", T.watchSeatMap() === true);
const t0 = performance.now();
T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []);
check("three workers spawn on 감시 시작", focusPoller.workers === T.FOCUS_WORKERS && T.FOCUS_WORKERS === 3, `workers=${focusPoller.workers}`);
await sleep(1200);
{
  const window1s = sent.filter((t) => t >= t0 + 150 && t < t0 + 1150);
  const gaps = [];
  for (let i = 1; i < window1s.length; i += 1) gaps.push(window1s[i] - window1s[i - 1]);
  gaps.sort((a, b) => a - b);
  const stream = {
    requestsPerSec: window1s.length,
    gapMedianMs: +(gaps[Math.floor(gaps.length / 2)] || 0).toFixed(2),
    gapP95Ms: +(gaps[Math.floor(gaps.length * 0.95)] || 0).toFixed(2),
    gapMaxMs: +(gaps[gaps.length - 1] || 0).toFixed(2),
    cap: T.CATCH_MAX_REQUESTS_PER_SEC,
  };
  report.stream = stream;
  check("the stream sits at the cap, never over it", stream.requestsPerSec >= 45 && stream.requestsPerSec <= T.CATCH_MAX_REQUESTS_PER_SEC + 2, JSON.stringify(stream));
  check("no gap in the stream wider than two cap periods", stream.gapMaxMs < 2 * (1000 / T.CATCH_MAX_REQUESTS_PER_SEC) + 10, `max gap ${stream.gapMaxMs}ms`);
}

// ---- 2. a seat frees: the poller itself presses it, sub-millisecond ----------

freeSet.add(7);
const freedAt = performance.now();
await until(() => (seatState.fastClicks || 0) >= 1, 800, "the freed seat to be pressed by the poller");
const noticedMs = +(performance.now() - freedAt).toFixed(1);
report.detect = { feedToPressMs: noticedMs, detectToPressMs: seatState.lastDetectToPressMs };
check("the pressed seat is the one that freed", seatState.fastClickedId === "S8", seatState.fastClickedId);
check("an enabled circle takes the pointer press", seatState.lastPressVia === "pointer", seatState.lastPressVia);
// F1: our own polls never pass through our own fetch hook, so the worker's
// diff is the first and the press fires from its callback.
check("F1: the worker's own diff pressed it (lastFreedVia focus)", seatState.lastFreedVia === "focus", seatState.lastFreedVia);
check("F1: our polls are not counted as the page's", !(seatState.pageStatusSeen > 0), String(seatState.pageStatusSeen || 0));
check("the 0->1 pulsed the page's SWR listener (online)", dispatched.filter((e) => e.type === "online").length === 1 && seatState.swrNudges === 1, `${dispatched.length} events, nudges ${seatState.swrNudges}`);
// The FIRST press of the sitting, cold apart from warmPressPath, inside a vm
// with three workers and a stub DOM churning: ~1ms here, 0.2-0.4ms on every
// press after it, 0.07ms in the isolated bench. The sub-0.5ms promise is
// asserted there; this guards the cold path against a layout or a scan.
// 3ms, not 1.5: measured 0.7-1.4ms alone and up to 1.6ms with pytest running
// beside it. The sub-0.5ms promise is the bench's; this guards the cold path.
check("detect -> press (first press, cold) under 3ms", typeof seatState.lastDetectToPressMs === "number" && seatState.lastDetectToPressMs < 3, `${seatState.lastDetectToPressMs}ms`);
// The site answers the page's own preselect, the cart rises, 선택 완료 goes.
sandbox.window.__nolsniperLastSeatNet.preselectAt = Date.now();
sandbox.window.__nolsniperLastSeatNet.preselectOk = true;
T.resolveSeatNetWaiters();
setCart(1);
await until(() => confirmPresses >= 1, 800, "선택 완료 to be pressed once the preselect answered");
check("선택 완료 pressed exactly once", confirmPresses === 1, String(confirmPresses));
sandbox.window.__nolsniperLastSeatNet.selectAt = Date.now();
sandbox.window.__nolsniperLastSeatNet.selectOk = true;
T.resolveSeatNetWaiters();
await until(() => seatState.locked === true, 800, "the hold to lock");

// ---- 3. held: the poller is gone, the guard is on, the status says so --------

await until(() => focusPoller.workers === 0, 600, "the workers to retire on lock");
const heldAt = performance.now();
await sleep(250);
check("no seatStatus leaves while a seat is held", requestsSince(heldAt) === 0, `${requestsSince(heldAt)} sent`);
check("the overlay says 좌석 선점 완료", /좌석 선점 완료/.test(seatState.message), seatState.message);
check("the hold guard is watching", seatState.holdGuardOn === true);
check("the held seat is remembered", seatState.heldSeatIds.has("S8"));
report.steps.push({ step: "held", message: seatState.message, workers: focusPoller.workers });

// ---- 4. the user lets the seat go: PAUSED, nothing cleared, nothing released --

setCart(0);
T.noteDeselectSeen();                       // the page's own BulkDeselectSeats answered
const letGoAt = performance.now();
await until(() => seatState.pauseReason === "userDeselect", 1500, "the engine to pause on the deselect");
report.pause = { noticedMs: +(performance.now() - letGoAt).toFixed(0), message: seatState.message };
check("paused: not running", seatState.running === false);
check("paused: the stale lock is dropped", seatState.locked === false && seatState.confirmStarted === false);
check("paused: nothing is remembered as held", seatState.heldSeatIds.size === 0);
check("paused: sticky until 감시 시작", seatState.haltedByUser === true);
check("paused: the poller is off", focusPoller.active === false && focusPoller.workers === 0);
check("paused: the guard is off", seatState.holdGuardOn === false);
check("paused: the overlay says so", /감시 일시정지/.test(seatState.message), seatState.message);

// ---- 5. another seat frees: NOTHING is taken --------------------------------

freeSet.add(12);
const presses = seatState.fastClicks;
const quietAt = performance.now();
T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []); // a stray start
await sleep(300);
check("no press while paused", seatState.fastClicks === presses, `${seatState.fastClicks} vs ${presses}`);
check("no request while paused", requestsSince(quietAt) === 0, `${requestsSince(quietAt)} sent`);
check("stray workers retire at once", focusPoller.workers === 0, String(focusPoller.workers));

// ---- 6. 감시 시작: the button, and only the button, lifts the pause ------------

{
  const fn = source.slice(source.indexOf("async function runSeatAutopilot("));
  const head = fn.slice(0, fn.indexOf("const blockedFor = gatewayBlockRemainingMs();"));
  check("감시 시작 clears haltedByUser and the pause reason",
    /if \(userInitiated\) \{[\s\S]{0,200}seatState\.haltedByUser = false;[\s\S]{0,200}seatState\.pauseReason = "";/.test(head));
  const boot = source.slice(source.indexOf("function bootRoute("), source.indexOf("function bootRoute(") + 20000);
  check("bootRoute never restarts a halted watch",
    /if \(seatState\.haltedByUser\) \{\s*void ensureSeatCatalog\(\);\s*return;/.test(boot));
}
liveWatch();                                // what the button does before the loop
freeSet.clear();
seatState.pauseReason = "";
const resumedAt = performance.now();
T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []);
await sleep(300);
check("after 감시 시작 the stream is back", focusPoller.workers === 3 && requestsSince(resumedAt) >= 10, `${requestsSince(resumedAt)} sent`);

// ---- 7. human touch: a real press on a seat yields; ours never does ----------

const pointerdown = listeners.pointerdown[0];
check("a capture-phase pointerdown guard is installed", typeof pointerdown === "function");
pointerdown({ isTrusted: false, target: circles[20] });   // what the engine itself dispatches
await sleep(30);
check("an untrusted (our own) press does not pause", seatState.running === true && seatState.pauseReason === "");
setCart(0);
pointerdown({ isTrusted: true, target: circles[20] });    // the user's hand
await until(() => seatState.pauseReason === "humanTouch", 300, "the watch to yield to the user");
await until(() => focusPoller.workers === 0, 600, "the workers to retire after the touch");
const touchedAt = performance.now();
await sleep(200);
check("yielded: not running, sticky", seatState.running === false && seatState.haltedByUser === true);
check("yielded: no request follows", requestsSince(touchedAt) === 0, `${requestsSince(touchedAt)} sent`);
check("yielded: the user's selection is untouched (no clear, no release)",
  !/clearSelectedSeats|releasePreselected/.test(source.slice(source.indexOf("function pauseWatch("), source.indexOf("const HOLD_GUARD_MS"))));
check("yielded: the overlay says 직접 선택", /직접 선택/.test(seatState.message), seatState.message);

// ---- 8. price step: every poller stops the moment the URL says so ----------

liveWatch();
seatState.pauseReason = "";
T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []);
await sleep(150);
sandbox.location.search = "?step=price";
await until(() => focusPoller.workers === 0, 600, "the workers to stop on step=price");
const pricedAt = performance.now();
await sleep(200);
check("step=price: no request after the URL changed", requestsSince(pricedAt) === 0, `${requestsSince(pricedAt)} sent`);
check("step=price: the run loop pauses with reason priceStep",
  /if \(onPriceStep\(\)\) \{\s*pauseWatch\("priceStep"\);\s*return;/.test(source));
sandbox.location.search = "";

// ---- 9. the fiber bypass: a circle the page still draws disabled --------------

liveWatch();
seatState.pauseReason = ""; freeSet.clear();
circles[30].__fiberProps.isDisabled = true;      // the page has not redrawn it yet
handlerCalls.length = 0; dispatched.length = 0; seatState.swrNudgedAt = 0;
T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []);
await sleep(120);
const before30 = seatState.fastClicks || 0;
freeSet.add(30);
await until(() => (seatState.fastClicks || 0) > before30, 800, "the disabled seat to be pressed through the handler");
const hcall = handlerCalls[0];
check("a disabled circle goes through the page's handler", seatState.lastPressVia === "handler", seatState.lastPressVia);
check("the root seatSelectHandler was the one called", hcall?.kind === "root", hcall?.kind);
check("called as seatSelectHandler(true, seat, blockKey, isInterlocking, undefined, undefined)",
  hcall && hcall.select === true && hcall.blockKey === BLOCK_KEY && hcall.skipNetwork === false && hcall.group === undefined, JSON.stringify(hcall && { select: hcall.select, blockKey: hcall.blockKey, skip: hcall.skipNetwork }));
check("the seat handed over is the page's own seatMeta object", hcall?.seat === seats[30], hcall?.seat?.seatInfoId);
check("handler detect -> press (cold) under 3ms", typeof seatState.lastDetectToPressMs === "number" && seatState.lastDetectToPressMs < 3, `${seatState.lastDetectToPressMs}ms`);
report.handler = { detectToPressMs: seatState.lastDetectToPressMs, kind: hcall?.kind, presses: seatState.handlerPresses, misses: seatState.handlerMisses };
check("the pulse went out again for this flip", dispatched.some((e) => e.type === "online"), String(dispatched.length));
// The page's own preselect answers (played back by the handler stub): the
// sequence confirms and locks exactly as it does after a pointer press.
await until(() => confirmPresses >= 2, 800, "선택 완료 after the handler press");
sandbox.window.__nolsniperLastSeatNet.selectAt = Date.now(); sandbox.window.__nolsniperLastSeatNet.selectOk = true; T.resolveSeatNetWaiters();
await until(() => seatState.locked === true, 800, "the handler-pressed seat to lock");
check("the handler press ends in a hold", seatState.locked === true && seatState.heldSeatIds.has("S31"));
check("the 7-minute hold clock is set", seatState.holdExpiresAt > Date.now() + 400000);
T.pauseWatch("userDeselect"); seatState.pauseReason = ""; circles[30].__fiberProps.isDisabled = false;

// ---- 10. fallback: no handler in the tree -> pointer, or nothing ----------------

liveWatch(); freeSet.clear();
const savedReturn = blockFiber.return;
for (const c of circles) c.__fiberProps && (c.__fiberProps.isDisabled = false);
blockFiber.return = null; blockFiber.memoizedProps.onSeatClick = null;   // fiber unreachable
circles[40].__fiberProps.isDisabled = true;
T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []);
await sleep(120);
const before40 = seatState.fastClicks || 0; const misses40 = seatState.handlerMisses || 0;
freeSet.add(40);
await sleep(250);
check("no handler and a disabled circle: nothing is pressed (node-disabled), a miss is counted",
  seatState.fastClicks === before40 && seatState.handlerMisses > misses40, `clicks ${seatState.fastClicks} vs ${before40}, misses ${seatState.handlerMisses}`);
freeSet.add(41);                                  // an enabled one: the pointer press still works
await until(() => (seatState.fastClicks || 0) > before40, 800, "the enabled seat to take the pointer fallback");
check("fallback: the enabled seat took the pointer press", seatState.lastPressVia === "pointer" && seatState.fastClickedId === "S42", `${seatState.lastPressVia} ${seatState.fastClickedId}`);
blockFiber.return = savedReturn; blockFiber.memoizedProps.onSeatClick = (seat, isSelected, blockKey) => handlerCalls.push({ kind: "block", seat, isSelected, blockKey });
circles[40].__fiberProps.isDisabled = false;
T.pauseWatch("userDeselect"); seatState.pauseReason = "";

// ---- 11. dialogs: P41149 answered the instant it mounts; session-expired never --

function dialogNode(text, { withConfirm = true } = {}) {
  let clicks = 0;
  const button = { tagName: "button", textContent: "확인", getAttribute: () => "", closest: () => null, click: () => { clicks += 1; } };
  const node = {
    nodeType: 1, tagName: "div", isConnected: false, style: {},
    classList: { contains: () => false, add: noop, remove: noop },
    getAttribute: () => "nds-e-dialog", setAttribute: noop,
    getBoundingClientRect: () => ({ left: 300, top: 300, width: 320, height: 160, right: 620, bottom: 460 }),
    querySelectorAll: (sel) => (/button/.test(sel) && withConfirm ? [button] : []),
    querySelector: () => null, closest: () => null,
    get innerText() { return text; },
    clicks: () => clicks,
  };
  return node;
}
check("P41149 classifies as statusChanged", T.classifyDialogText("좌석 상태가 변경되었습니다.\n다른 좌석을 선택해 주세요.") === "statusChanged");
check("이미 선점된 좌석입니다 classifies as taken", T.classifyDialogText("이미 선점된 좌석입니다.") === "taken");
check("세션이 만료 classifies as sessionExpired", T.classifyDialogText("세션이 만료되었습니다.") === "sessionExpired");
check("the 7-minute expiry copy classifies as holdExpired", T.classifyDialogText("좌석을 선택할 수 있는 시간 10분이 종료되었어요") === "holdExpired");
check("the slider captcha copy is a captcha", T.classifyDialogText("화살표를 밀어 퍼즐을 맞춰주세요") === "captcha");
liveWatch();
check("the dialog watch is installed on the body", observers.some((o) => o.target === bodyNode));
const p41149 = dialogNode("좌석 상태가 변경되었습니다.\n다른 좌석을 선택해 주세요.");
const mountedAt = performance.now();
bodyNode.appendChild(p41149);
await until(() => p41149.clicks() === 1, 200, "P41149's 확인 to be pressed by the observer");
const dismissMs = performance.now() - mountedAt;
report.dialog = { p41149DismissMs: +dismissMs.toFixed(2), reported: seatState.lastFastDismissMs };
check("P41149 dismissed in under 30ms of mounting", dismissMs < 30, `${dismissMs.toFixed(2)}ms`);
check("P41149 is counted as 이선좌", seatState.statusChangedDialogs >= 1 && seatState.takenConflicts >= 1, `${seatState.statusChangedDialogs}/${seatState.takenConflicts}`);
const expired = dialogNode("세션이 만료되었습니다.");
bodyNode.appendChild(expired);
await sleep(30);
check("a session-expired dialog is never auto-pressed (its 확인 navigates)", expired.clicks() === 0 && seatState.lastDialog?.kind === "sessionExpired", JSON.stringify(seatState.lastDialog));
T.pauseWatch("userDeselect"); seatState.pauseReason = "";
const idle = dialogNode("이미 선점된 좌석입니다.");
bodyNode.appendChild(idle);
await sleep(30);
check("nothing is pressed while the engine is idle", idle.clicks() === 0);

// ---- 12. blocker 1: 선택 완료 is pressed after one macrotask, and pressed again
// if the page's select never leaves within 120ms ---------------------------------

async function catchViaHandler(seatIdx, { selectSentAfterMs = null, answerAfterMs = 20 } = {}) {
  liveWatch(); freeSet.clear(); seatState.pauseReason = "";
  circles[seatIdx].__fiberProps.isDisabled = true;
  T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []);
  await sleep(100);
  const pressesBefore = confirmPresses;
  freeSet.add(seatIdx);
  await until(() => confirmPresses > pressesBefore, 1000, "선택 완료 after the handler press");
  const firstPressAt = performance.now();
  if (selectSentAfterMs !== null) {
    setTimeout(() => { sandbox.window.__nolsniperLastSeatNet.selectSentAt = Date.now(); }, selectSentAfterMs);
  }
  await sleep(answerAfterMs + (selectSentAfterMs || 0));
  sandbox.window.__nolsniperLastSeatNet.selectAt = Date.now(); sandbox.window.__nolsniperLastSeatNet.selectOk = true; T.resolveSeatNetWaiters();
  await until(() => seatState.locked === true, 800, "the seat to lock");
  const presses = confirmPresses - pressesBefore;
  T.pauseWatch("userDeselect"); seatState.pauseReason = ""; circles[seatIdx].__fiberProps.isDisabled = false;
  return { presses, firstPressAt };
}
check("the watchdog window is 120ms", T.CONFIRM_WATCHDOG_MS === 120);
{
  // A healthy page: its select leaves 30ms after the click → exactly one press.
  const healthy = await catchViaHandler(44, { selectSentAfterMs: 30 });
  check("select seen within 120ms: 선택 완료 pressed exactly once", healthy.presses === 1, String(healthy.presses));
  // A swallowed click: nothing leaves for 250ms → a second, decisive press.
  const reps = seatState.confirmRepresses || 0;
  const swallowed = await catchViaHandler(45, { selectSentAfterMs: 250, answerAfterMs: 10 });
  check("no select within 120ms: 선택 완료 pressed a second time", swallowed.presses === 2 && seatState.confirmRepresses === reps + 1, `${swallowed.presses} presses, represses ${seatState.confirmRepresses}`);
  report.watchdog = { healthyPresses: healthy.presses, swallowedPresses: swallowed.presses };
}
{
  // The press sequence yields one macrotask between the preselect answer and
  // the click — asserted in the source, since the harness cannot see task edges.
  const seq = source.slice(source.indexOf("async function pressSequence("), source.indexOf("function startFocusPoller("));
  const preAt = seq.indexOf('await waitForSeatNet("preselect", since, 2500);');
  const hopAt = seq.indexOf("await yieldFast();\n      if (halted()) return bail(lat);\n      // Then confirm");
  const clickAt = seq.indexOf("if (clickConfirmSelect()) { confirmed = true; break; }");
  check("one macrotask between the preselect answer and 선택 완료", preAt > 0 && hopAt > preAt && clickAt > hopAt);
}

// ---- 13. blocker 2: the root handler only with an empty cart and 매수 1 -------

{
  liveWatch(); freeSet.clear(); seatState.pauseReason = "";
  handlerCalls.length = 0;
  setCart(1);                                    // something already in the cart
  handlerAnswers = false;                        // the stub must not add to the cart here
  circles[50].__fiberProps.isDisabled = true;
  T.startFocusPoller(INIT, BLOCK_KEY, CONFIG, sandbox.window.__nolsniperRunGen, ["R석"], []);
  await sleep(100);
  const before = seatState.fastClicks || 0;
  freeSet.add(50);
  await until(() => (seatState.fastClicks || 0) > before, 800, "the press with a non-empty cart");
  check("non-empty cart: the block onSeatClick is used, never the root handler", handlerCalls[0]?.kind === "block" && seatState.handlerRootRefused >= 1, JSON.stringify(handlerCalls[0] && { kind: handlerCalls[0].kind, isSelected: handlerCalls[0].isSelected }));
  check("block onSeatClick called as (seat, false, blockKey)", handlerCalls[0]?.seat === seats[50] && handlerCalls[0]?.isSelected === false && handlerCalls[0]?.blockKey === BLOCK_KEY);
  T.pauseWatch("userDeselect"); seatState.pauseReason = ""; circles[50].__fiberProps.isDisabled = false; setCart(0); handlerAnswers = true;

  // 매수 2 with an empty cart: also the block handler.
  liveWatch(); freeSet.clear(); handlerCalls.length = 0;
  circles[51].__fiberProps.isDisabled = true;
  T.startFocusPoller(INIT, BLOCK_KEY, { ...CONFIG, quantity: 2 }, sandbox.window.__nolsniperRunGen, ["R석"], []);
  await sleep(100);
  const pressed = T.pressViaHandler("S52", { blockKey: BLOCK_KEY, node: circles[51], quantity: 2 });
  check("매수 2: the block onSeatClick is used", pressed && handlerCalls[0]?.kind === "block", handlerCalls[0]?.kind);
  T.pauseWatch("userDeselect"); seatState.pauseReason = ""; circles[51].__fiberProps.isDisabled = false;

  // No block handler in the tree and a non-empty cart: refused (pointer fallback).
  setCart(1);
  const keep = blockFiber.memoizedProps.onSeatClick; blockFiber.memoizedProps.onSeatClick = null;
  const misses = seatState.handlerMisses || 0;
  check("no block handler + non-empty cart: refused, a miss counted", T.pressViaHandler("S53", { blockKey: BLOCK_KEY, node: circles[52], quantity: 1 }) === false && seatState.handlerMisses === misses + 1);
  blockFiber.memoizedProps.onSeatClick = keep; setCart(0);
}

// ---- 14. the 10-minute expiry modal is never pressed, by copy or by clock ------

{
  liveWatch();
  const tenMin = dialogNode("좌석을 선택할 수 있는 시간 10분이 종료되었어요");
  bodyNode.appendChild(tenMin);
  await sleep(30);
  check("the 10분 expiry modal is never auto-pressed", tenMin.clicks() === 0 && seatState.pauseReason === "holdExpired", `${tenMin.clicks()} ${seatState.pauseReason}`);
  seatState.pauseReason = "";
  liveWatch();
  // The clock says the session is over: even an unlabelled small dialog is the expiry.
  sandbox.window.__NEXT_DATA__ = { props: { pageProps: { initData: { ...INIT, expireAt: Date.now() - 1000 } } } };
  const unlabelled = dialogNode("확인해 주세요.");
  bodyNode.appendChild(unlabelled);
  await sleep(30);
  check("past initData.expireAt, an unlabelled dialog reads as sessionExpired and is not pressed", unlabelled.clicks() === 0 && seatState.lastDialog?.kind === "sessionExpired", JSON.stringify(seatState.lastDialog));
  sandbox.window.__NEXT_DATA__ = undefined;
  T.pauseWatch("userDeselect"); seatState.pauseReason = "";
}

// ---- report ---------------------------------------------------------------------

report.checks = checks;
report.ok = !report.failed;
report.build = api.build;
if (process.argv.includes("--json")) {
  console.log(JSON.stringify(report));
} else {
  console.log(`build ${report.build}`);
  console.log(`stream: ${JSON.stringify(report.stream)}`);
  console.log(`detect: ${JSON.stringify(report.detect)}   pause: ${JSON.stringify(report.pause)}`);
  console.log(`handler: ${JSON.stringify(report.handler)}   dialog: ${JSON.stringify(report.dialog)}   watchdog: ${JSON.stringify(report.watchdog)}`);
  for (const c of checks) console.log(`${c.ok ? "ok  " : "FAIL"} ${c.name}${c.detail ? `  — ${c.detail}` : ""}`);
  console.log(`\n${checks.filter((c) => c.ok).length}/${checks.length} passed`);
}
process.exit(report.ok ? 0 : 1);
