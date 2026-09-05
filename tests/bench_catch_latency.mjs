/**
 * What 취켓팅 actually costs, in milliseconds, from "a seat freed" to "선택 완료".
 *
 * The four segments the race is decided in:
 *
 *   detect  — a 0->1 in the availability bitmap becomes a ranked candidate
 *   click   — that candidate's circle is found and a real pointer press fires
 *   cart    — the page's 선택 좌석 count rises (proof the soft hold is on the page)
 *   confirm — 선택 완료 is pressed
 *
 * Only the first two are ours to make fast; `cart` is dominated by the site's
 * own preselect round trip, which this harness simulates at a fixed latency so
 * the *overhead we add on top of it* is visible. `confirm` is the quiet gap we
 * hold before pressing, which is entirely ours.
 *
 * The venue is synthetic but its shape is taken from the live measurements in
 * the autopilot's own comments: ~21,600 seats across 12 blocks, ~2,200 circles
 * mounted for the open 구역, and seat circles whose React props carry no
 * blockKey (measured live: all 273 drawn seats came back with blockKey
 * undefined).
 *
 * Run: node tests/bench_catch_latency.mjs
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(process.env.NOLSNIPER_AUTOPILOT || resolve(here, "../browser/nolsniper_autopilot.js"), "utf8");

const BLOCKS = 12;
const SEATS_PER_BLOCK = 1800;
const DRAWN = SEATS_PER_BLOCK; // the whole open 구역 mounted, which is what 전체보기 achieves
const PRESELECT_RTT_MS = 220; // the site's own soft-hold round trip
const CART_RENDER_MS = 60; // and the re-render of 선택 좌석 that follows it
const DISAGREEING = [120, 500, 900, 1300, 1700]; // seats the bitmap called free while the map still draws them taken

const noop = () => {};

// ---- a DOM with a real seat map in it ------------------------------------

// The components above the circles: the block (onSeatClick + seatMeta +
// blockKey) and the page root (seatSelectHandler), as the bundle has them.
const benchRoot = { memoizedProps: { seatSelectHandler: noop, goods: { isInterlocking: false } }, return: null };
const benchBlock = { memoizedProps: { onSeatClick: noop, blockKey: "004:001", seatMeta: [] }, return: benchRoot };
let fiberSeq = 0;
function circleNode(seat, { disabled = false } = {}) {
  const node = {
    tagName: "circle",
    isConnected: true,
    style: {},
    dataset: {},
    classList: { contains: () => false, add: noop, remove: noop },
    getAttribute: (name) => (name === "r" ? "3" : ""),
    setAttribute: noop,
    getBoundingClientRect: () => ({ left: 100, top: 100, width: 6, height: 6, right: 106, bottom: 106 }),
    dispatchEvent: () => true,
    querySelectorAll: () => [],
    closest: () => null,
  };
  // Two fibers up, as the real map nests the circle inside its seat component.
  const inner = { memoizedProps: {}, return: null };
  const outer = {
    memoizedProps: { seat, blockKey: undefined, isSelected: false },
    return: benchBlock,
  };
  Object.defineProperty(outer.memoizedProps, "isDisabled", {
    enumerable: true,
    get: () => disabled || node.__benchDisabled === true,
  });
  inner.return = outer;
  node[`__reactFiber$${(fiberSeq += 1).toString(36)}`] = inner;
  return node;
}

function makeVenue() {
  const blocks = [];
  let id = 0;
  for (let b = 0; b < BLOCKS; b += 1) {
    const blockKey = `0${String(b + 1).padStart(2, "0")}:001`;
    const seats = [];
    for (let s = 0; s < SEATS_PER_BLOCK; s += 1) {
      id += 1;
      seats.push({
        seatInfoId: `S${id}`,
        seatGrade: "1",
        seatGradeName: "R석",
        rowNo: String(Math.floor(s / 30) + 1),
        seatNo: String((s % 30) + 1),
        posLeft: 100 + (s % 30) * 8,
        posTop: 100 + Math.floor(s / 30) * 8,
        isExposable: true,
        seatGroupId: null,
      });
    }
    blocks.push({ blockKey, selfDefineBlock: `${b + 1}구역`, seats, mask: seats.map(() => false) });
  }
  return blocks;
}

const venue = makeVenue();
const openBlock = venue[3]; // the 구역 we are standing in
const drawnSeats = openBlock.seats.slice(0, DRAWN);
const circles = drawnSeats.map((seat) => circleNode({ ...seat, blockKey: undefined }));
const byId = new Map(drawnSeats.map((seat, at) => [String(seat.seatInfoId), circles[at]]));

let cartCount = 0;
let sidebarText = "선택한 좌석이 없습니다";
const refreshSidebar = () => {
  sidebarText = cartCount > 0 ? `선택 좌석 ${cartCount}` : "선택한 좌석이 없습니다";
};

let confirmPressedAt = 0;
const confirmButton = {
  tagName: "button",
  isConnected: true,
  disabled: false,
  style: {},
  value: "",
  textContent: "선택 완료",
  classList: { contains: () => false, add: noop, remove: noop },
  getAttribute: () => "",
  setAttribute: noop,
  getBoundingClientRect: () => ({ left: 10, top: 10, width: 120, height: 40, right: 130, bottom: 50 }),
  click: () => { confirmPressedAt = performance.now(); },
  dispatchEvent: () => true,
  querySelectorAll: () => [],
  closest: () => null,
};

const sidebarNode = {
  tagName: "div",
  isConnected: true,
  style: {},
  classList: { contains: () => false, add: noop, remove: noop },
  getAttribute: () => "",
  setAttribute: noop,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 200, height: 40, right: 200, bottom: 40 }),
  querySelectorAll: () => [],
  closest: () => null,
  get innerText() { return sidebarText; },
};

const mapRoot = {
  tagName: "div",
  isConnected: true,
  style: {},
  classList: { contains: () => false, add: noop, remove: noop },
  getAttribute: () => "",
  setAttribute: noop,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 900, right: 1200, bottom: 900 }),
  querySelectorAll: () => [],
  closest: () => null,
};

const CIRCLE_SELECTORS = new Set([
  "circle.js-seat",
  'circle[class*="SeatMap"]',
  '[class*="SeatMap"] circle',
  "svg circle",
]);

const document_ = {
  documentElement: mapRoot,
  head: mapRoot,
  readyState: "complete",
  createElement: () => ({ style: {}, setAttribute: noop, appendChild: noop, remove: noop, classList: { add: noop, remove: noop, contains: () => false } }),
  getElementById: () => null,
  addEventListener: noop,
  elementFromPoint: () => null,
  get body() {
    return {
      tagName: "body",
      isConnected: true,
      style: {},
      classList: { contains: () => false, add: noop, remove: noop },
      getAttribute: () => "",
      setAttribute: noop,
      appendChild: noop,
      querySelectorAll: (sel) => document_.querySelectorAll(sel),
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 900, right: 1200, bottom: 900 }),
      get innerText() { return `${sidebarText}\n선택 완료`; },
    };
  },
  querySelector(sel) {
    if (/seatMap|SeatMap|placeImg/.test(sel)) return mapRoot;
    if (sel === "svg") return mapRoot;
    return null;
  },
  querySelectorAll(sel) {
    if (CIRCLE_SELECTORS.has(sel)) return sel === "circle.js-seat" ? circles : [];
    if (/^button/.test(sel) || sel.includes("role=button")) return [confirmButton];
    if (sel === "div,section,aside,p,span") return [sidebarNode];
    return [];
  },
};

class MutationObserverStub {
  observe() {}
  disconnect() {}
}

const sandbox = {
  console: { log: noop, warn: noop, error: noop },
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval: noop,
  fetch: async () => ({ ok: false, status: 500, text: async () => "", json: async () => ({}) }),
  location: {
    href: "https://tickets.interpark.com/onestop/seat",
    pathname: "/onestop/seat",
    hostname: "tickets.interpark.com",
    search: "",
    origin: "https://tickets.interpark.com",
    assign: noop,
    replace: noop,
    reload: noop,
  },
  URL,
  URLSearchParams,
  MouseEvent: class { constructor(t, i = {}) { this.type = t; Object.assign(this, i); } },
  PointerEvent: class { constructor(t, i = {}) { this.type = t; Object.assign(this, i); } },
  KeyboardEvent: class { constructor(t, i = {}) { this.type = t; Object.assign(this, i); } },
  MutationObserver: MutationObserverStub,
  XMLHttpRequest: class { open() {} send() {} addEventListener() {} },
  open: () => null,
  close: noop,
  sessionStorage: { length: 0, key: () => null, getItem: () => null, setItem: noop, removeItem: noop },
  localStorage: {
    _data: new Map(),
    getItem(k) { return this._data.has(k) ? this._data.get(k) : null; },
    setItem(k, v) { this._data.set(k, String(v)); },
    removeItem(k) { this._data.delete(k); },
  },
  document: document_,
  navigator: { userAgent: "node" },
  performance,
  Promise,
  Set,
  Map,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.top = sandbox;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "nolsniper_autopilot.js" });

const api = sandbox.window.NOLSniper;
const race = api?.race;
if (!race) throw new Error("autopilot did not expose window.NOLSniper.race");

race.state.lastBlocks = venue;
race.state.discoveredBlocks = venue.map((b) => ({ blockKey: b.blockKey, selfDefineBlock: b.selfDefineBlock }));
race.state.runBaseline = new Set(venue.map((b) => String(b.blockKey)));

// ---- measuring ------------------------------------------------------------

const results = [];
function bench(name, runs, fn) {
  fn(); // warm
  const samples = [];
  for (let at = 0; at < runs; at += 1) {
    const started = performance.now();
    fn();
    samples.push(performance.now() - started);
  }
  samples.sort((a, b) => a - b);
  const median = samples[Math.floor(samples.length / 2)];
  const worst = samples[samples.length - 1];
  results.push({ name, median, worst, runs });
  return median;
}

const CONFIG = { grade_order: ["R석"], quantity: 1, allow_group_seats: false, retry_ms: 100 };

// 1. detect: fold a fresh bitmap in and report what opened.
let freedCandidate = null;
bench("detect · applyBlockMask (1 block, 1800 seats, one 0->1)", 200, () => {
  const mask = openBlock.seats.map(() => false);
  mask[900] = true;
  openBlock.mask = openBlock.seats.map(() => false);
  const freed = race.applyBlockMask(openBlock, mask, CONFIG);
  freedCandidate = freed[0] || freedCandidate;
});

// 2. where am I: the travel decision made after a seat frees.
bench("travel decision · currentOpenBlock()", 5, () => race.currentOpenBlock());

// 3. which of these can I click right now
const candidates = [race.toCandidate ? race.toCandidate(drawnSeats[900], openBlock.blockKey) : freedCandidate];
const pick = freedCandidate || candidates[0];
bench("click · clickableAmong(1 candidate)", 50, () => race.clickableAmong([pick]));

// 4. find the circle and press it. Averaged over seats spread through the
// mounted set, because a linear scan's cost depends on where the seat sits.
const SPREAD = [50, 450, 900, 1350, DRAWN - 1];
bench("click · clickSeatOnMap (find node + dispatch)", 20, () => {
  for (const at of SPREAD) race.clickSeatOnMap(drawnSeats[at].seatInfoId);
});

// 4a. the fiber bypass: the circle is still drawn disabled (the page's SWR
// poll has not redrawn it), so the press goes up the fiber to the page's
// own seatSelectHandler instead of waiting 0-4s for the leaf gate to open.
race.state.lastBlocks = venue;
bench("press via handler · disabled circle -> seatSelectHandler through the fiber", 100, () => {
  const at = 901;
  circles[at].__benchDisabled = true;
  race.state.fastClickedId = "";
  const ok = race.clickSeatOnMap(drawnSeats[at].seatInfoId, { node: circles[at], blockKey: openBlock.blockKey });
  circles[at].__benchDisabled = false;
  if (!ok || race.state.lastPressVia !== "handler") throw new Error("bench: the handler press did not go through the fiber");
});
race.state.fastClickedId = "";

// 4b. detect -> press, the whole synchronous stretch of the focus fast path:
// a 0->1 in the bitmap becomes a ranked candidate and pressSequence fires the
// pointer events. pressSequence is async, but nothing in it yields before the
// events leave, so the press stamp is readable the moment the call returns.
// Measured from the seat's own freedAtPerf (stamped inside applyBlockMask),
// which is the clock the live run uses too.
const seq = api.__test;
const pickerApi = api.picker;
sandbox.window.__nolsniperRunGen = 1;
const pressSamples = [];
{
  const state = race.state;
  const mask = openBlock.seats.map(() => false);
  mask[900] = true;
  for (let at = 0; at < 60; at += 1) {
    openBlock.mask = openBlock.seats.map(() => false);
    state.fastClickedId = ""; state.fastClickedAt = 0; state.locked = false; state.stopRequested = false;
    state.pressSequenceBusy = false; state.lastCatchLatency = null;
    const freed = race.applyBlockMask(openBlock, mask, CONFIG);
    const ranked = pickerApi.rankCandidates(freed, ["R석"], [], { strategy: "center" });
    seq.pressSequence(ranked, CONFIG).catch(noop);
    // The sequence is now parked on the preselect answer; wake it as a stop so
    // the next sample does not queue behind a 2.5s timeout.
    seq.abortSeatNetWaiters();
    const lat = state.lastCatchLatency;
    if (!lat || lat.outcome === "no-node") throw new Error("bench: pressSequence did not press the freed seat");
    if (at >= 10) pressSamples.push(lat.pressMs);
  }
  pressSamples.sort((a, b) => a - b);
  results.push({
    name: "detect -> press · bitmap flip -> rank -> pressSequence pointer events",
    median: pressSamples[Math.floor(pressSamples.length / 2)],
    worst: pressSamples[pressSamples.length - 1],
    runs: pressSamples.length,
  });
  state.locked = false; state.stopRequested = false; state.fastClickedId = "";
}

// 5. the per-tick instrumentation that runs whether or not anything freed.
//
// The seats it watches are ones the bitmap called free while the map still
// draws them taken — that disagreement is the whole point of the measurement,
// and it is also what keeps them in the watch list tick after tick.
race.state.lastBlocks = venue;
for (const at of DISAGREEING) circles[at].__benchDisabled = true;
bench("per tick · checkDomAgreement (5 seats the map still draws taken)", 20, () => {
  for (const at of DISAGREEING) race.noteBitmapSawFree(drawnSeats[at].seatInfoId);
  race.checkDomAgreement();
});
for (const at of DISAGREEING) circles[at].__benchDisabled = false;

// 6. the count read every judgement about page state goes through, on the
// watch's own thread, ten times a second.
bench("per tick · selectedSeatCount()", 200, () => race.selectedSeatCount());

// 6. the cart wait: how long after the site's own preselect lands we notice.
async function cartWait() {
  cartCount = 0;
  refreshSidebar();
  race.resetSeatCountScope();
  const before = race.selectedSeatCount();
  const clickedAt = performance.now();
  setTimeout(() => { cartCount = 1; refreshSidebar(); }, PRESELECT_RTT_MS + CART_RENDER_MS);
  const ok = await race.pageRegisteredSelection(before, 1);
  const noticedAt = performance.now();
  return { ok, total: noticedAt - clickedAt, overhead: noticedAt - clickedAt - PRESELECT_RTT_MS - CART_RENDER_MS };
}

// 7. click -> 선택 완료, the whole way: the site answers preselect, the cart
// rises, we hold the quiet gap NOL's own in-flight guard needs, we press.
async function clickToConfirm() {
  cartCount = 0;
  confirmPressedAt = 0;
  refreshSidebar();
  race.resetSeatCountScope();
  sandbox.window.__nolsniperLastSeatNet = {};
  const before = race.selectedSeatCount();
  const clickedAt = performance.now();
  const since = Date.now();
  setTimeout(() => {
    // The page's own soft hold lands: its GraphQL answer, then its re-render.
    sandbox.window.__nolsniperLastSeatNet.preselectAt = Date.now();
    sandbox.window.__nolsniperLastSeatNet.preselectOk = true;
    setTimeout(() => { cartCount = 1; refreshSidebar(); }, CART_RENDER_MS);
  }, PRESELECT_RTT_MS);
  const registered = await race.pageRegisteredSelection(before, 1);
  const cartAt = performance.now();
  await race.waitForSoftHoldIdle({ since: since - 5000, quietMs: 250, timeoutMs: 2500 });
  const confirmAt = performance.now();
  return {
    registered,
    cartOverhead: cartAt - clickedAt - PRESELECT_RTT_MS - CART_RENDER_MS,
    cartToConfirm: confirmAt - cartAt,
    clickToConfirm: confirmAt - clickedAt,
  };
}

const runs = [];
for (let at = 0; at < 5; at += 1) runs.push(await clickToConfirm());
const med = (field) => {
  const values = runs.map((r) => r[field]).sort((a, b) => a - b);
  return { median: values[Math.floor(values.length / 2)], worst: values[values.length - 1] };
};
if (!runs.every((r) => r.registered)) throw new Error("bench: the cart never registered the click");
const cartRow = med("cartOverhead");
results.push({
  name: `cart · notice lag past a ${PRESELECT_RTT_MS}+${CART_RENDER_MS}ms preselect+render`,
  ...cartRow,
  runs: runs.length,
});
const confirmRow = med("cartToConfirm");
results.push({ name: "confirm · quiet gap held before 선택 완료", ...confirmRow, runs: runs.length });
const quiet = confirmRow.median;

// ---- report ---------------------------------------------------------------

// Machine-readable, so the ceilings can be asserted instead of eyeballed. The
// segments this guards are the ones that were measured at 925ms and 65ms; a
// regression in them is silent otherwise, because everything still works — it
// just loses.
if (process.argv.includes("--json")) {
  const keyed = {};
  for (const row of results) {
    const key = row.name.includes("currentOpenBlock")
      ? "currentOpenBlockMs"
      : row.name.startsWith("detect -> press")
        ? "detectToPressMs"
      : row.name.startsWith("press via handler")
        ? "handlerPressMs"
      : row.name.includes("clickSeatOnMap")
        ? "clickSeatOnMapMs"
        : row.name.includes("checkDomAgreement")
          ? "checkDomAgreementMs"
          : row.name.startsWith("cart")
            ? "cartNoticeLagMs"
            : row.name.startsWith("confirm")
              ? "quietGapMs"
              : null;
    if (key) keyed[key] = row.median;
  }
  console.log(JSON.stringify(keyed));
  process.exit(0);
}

const pad = (s, n) => String(s).padEnd(n);
const num = (v) => (Number.isFinite(v) ? v.toFixed(1).padStart(8) : "       —");
console.log(`\nvenue: ${BLOCKS} blocks x ${SEATS_PER_BLOCK} seats = ${BLOCKS * SEATS_PER_BLOCK}, ${DRAWN} circles drawn`);
console.log(`build: ${api.status ? api.status().seat.build : "?"}\n`);
console.log(`${pad("segment", 62)}${pad("median ms", 11)}worst ms`);
console.log("-".repeat(84));
for (const row of results) {
  console.log(`${pad(row.name, 60)}${num(row.median)}   ${num(row.worst)}`);
}
const hot =
  (results.find((r) => r.name.startsWith("detect ·"))?.median || 0) +
  (results.find((r) => r.name.includes("clickableAmong"))?.median || 0) +
  (results.find((r) => r.name.includes("clickSeatOnMap"))?.median || 0);
const perTick = results.find((r) => r.name.startsWith("per tick"))?.median || 0;
const cart = cartRow.median;
console.log("-".repeat(84));
console.log(`${pad("detect -> click, our own code", 60)}${num(hot)}`);
console.log(`${pad("detect -> press, measured end to end through pressSequence", 60)}${num(results.find((r) => r.name.startsWith("detect -> press"))?.median)}`);
console.log(`${pad("added per watch tick (runs 10x/s whether or not anything freed)", 60)}${num(perTick)}`);
console.log(`${pad("click -> 선택 완료 pressed, our own overhead", 60)}${num(cart + quiet)}`);
console.log(`${pad("TOTAL overhead we add to detect -> 선택 완료", 60)}${num(hot + cart + quiet)}\n`);
