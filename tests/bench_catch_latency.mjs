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
const source = readFileSync(resolve(here, "../browser/nolsniper_autopilot.js"), "utf8");

const BLOCKS = 12;
const SEATS_PER_BLOCK = 1800;
const DRAWN = 2200; // circles mounted for the open 구역
const PRESELECT_RTT_MS = 220; // the site's own soft-hold round trip

const noop = () => {};

// ---- a DOM with a real seat map in it ------------------------------------

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
    memoizedProps: { seat, blockKey: undefined, isDisabled: disabled, isSelected: false },
    return: null,
  };
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

// 4. find the circle and press it
if (race.clickSeatOnMap) {
  bench("click · clickSeatOnMap (find node + dispatch)", 50, () => race.clickSeatOnMap(pick.seatInfoId));
} else {
  results.push({ name: "click · clickSeatOnMap — NOT EXPORTED", median: NaN, worst: NaN, runs: 0 });
}

// 5. the per-tick instrumentation that runs whether or not anything freed
race.state.lastBlocks = venue;
if (race.checkDomAgreement) {
  race.noteBitmapSawFree?.(drawnSeats[900].seatInfoId);
  bench("per tick · checkDomAgreement (1 watched seat)", 20, () => race.checkDomAgreement());
} else {
  results.push({ name: "per tick · checkDomAgreement — NOT EXPORTED", median: NaN, worst: NaN, runs: 0 });
}

// 6. the cart wait: how long after the site's own preselect lands we notice.
async function cartWait() {
  cartCount = 0;
  refreshSidebar();
  race.resetSeatCountScope();
  const before = race.selectedSeatCount();
  const clickedAt = performance.now();
  setTimeout(() => { cartCount = 1; refreshSidebar(); }, PRESELECT_RTT_MS);
  const ok = await race.pageRegisteredSelection(before, 1);
  const noticedAt = performance.now();
  return { ok, total: noticedAt - clickedAt, overhead: noticedAt - clickedAt - PRESELECT_RTT_MS };
}

// 7. the quiet gap held before 선택 완료.
function quietGapMs() {
  return race.SOFT_HOLD_QUIET_MS ?? 250;
}

const runs = [];
for (let at = 0; at < 5; at += 1) runs.push(await cartWait());
const overheads = runs.map((r) => r.overhead).sort((a, b) => a - b);
results.push({
  name: `cart · pageRegisteredSelection overhead past a ${PRESELECT_RTT_MS}ms preselect`,
  median: overheads[Math.floor(overheads.length / 2)],
  worst: overheads[overheads.length - 1],
  runs: runs.length,
});

const quiet = quietGapMs();
results.push({ name: "confirm · quiet gap held before 선택 완료", median: quiet, worst: quiet, runs: 1 });

// ---- report ---------------------------------------------------------------

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
  (results.find((r) => r.name.startsWith("detect"))?.median || 0) +
  (results.find((r) => r.name.includes("clickableAmong"))?.median || 0) +
  (results.find((r) => r.name.includes("clickSeatOnMap"))?.median || 0);
const perTick = results.find((r) => r.name.startsWith("per tick"))?.median || 0;
const cart = results.find((r) => r.name.startsWith("cart"))?.median || 0;
console.log("-".repeat(84));
console.log(`${pad("detect -> click, our own code", 60)}${num(hot)}`);
console.log(`${pad("added per watch tick (runs 10x/s whether or not anything freed)", 60)}${num(perTick)}`);
console.log(`${pad("click -> 선택 완료 pressed, our own overhead", 60)}${num(cart + quiet)}`);
console.log(`${pad("TOTAL overhead we add to detect -> 선택 완료", 60)}${num(hot + cart + quiet)}\n`);
