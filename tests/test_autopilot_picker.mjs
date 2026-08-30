/**
 * Verifies the in-browser seat picker behaves like the tested Python core.
 *
 * The autopilot is an IIFE that expects a DOM, so this harness stubs just
 * enough of the browser to let it install `window.PureClick`, then exercises
 * the exported pure helpers with seat shapes taken from real shows.
 */

// Non-strict assert: arrays produced inside the vm context have that realm's
// prototypes, which deepStrictEqual rejects even when contents match.
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");

const noop = () => {};

// Records where the shim sent this window, so the BookingPop path can be
// asserted without a real browser.
const navigations = [];

// A form that behaves like the one openPCOnestop() builds: a real
// HTMLFormElement.prototype.submit() the shim has to intercept, since that call
// never fires a submit event.
class HTMLFormElement {
  constructor() {
    this.target = "";
    this.action = "";
    this.method = "get";
    this.name = "";
    this.children = [];
    this.submitted = false;
  }
  appendChild(child) {
    this.children.push(child);
  }
  submit() {
    this.submitted = true;
    navigations.push({ post: this.action, target: this.target });
  }
}

// Enough of the DOM event constructors to record what got dispatched.
class MouseEvent {
  constructor(type, init = {}) {
    this.type = type;
    Object.assign(this, init);
  }
}
class PointerEvent extends MouseEvent {}

const element = () => ({
  style: {},
  classList: { contains: () => false, add: noop, remove: noop },
  setAttribute: noop,
  getAttribute: () => "",
  appendChild: noop,
  remove: noop,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: noop,
  textContent: "",
  innerHTML: "",
});

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval: noop,
  fetch: async () => ({ ok: false, status: 500, json: async () => ({}) }),
  location: {
    href: "https://nol.yanolja.com/ticket",
    pathname: "/ticket",
    search: "",
    origin: "https://nol.yanolja.com",
    assign(url) { navigations.push({ assign: url }); },
    replace(url) { navigations.push({ replace: url }); },
    reload: noop,
  },
  URL,
  // fetchSeatStatus builds its query with this; without it the whole poll
  // throws before it can be observed.
  URLSearchParams,
  HTMLFormElement,
  MouseEvent,
  PointerEvent,
  XMLHttpRequest: class { open() {} send() {} addEventListener() {} },
  open: () => null,
  close() { navigations.push({ close: true }); },
  // The page reads this during context discovery; without it every helper that
  // touches getInitData throws inside the sandbox.
  sessionStorage: { length: 0, key: () => null, getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: {
    _data: new Map(),
    getItem(key) { return this._data.has(key) ? this._data.get(key) : null; },
    setItem(key, value) { this._data.set(key, String(value)); },
    removeItem(key) { this._data.delete(key); },
  },
  document: {
    documentElement: element(),
    body: element(),
    head: element(),
    readyState: "complete",
    createElement: element,
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: noop,
  },
  navigator: { userAgent: "node" },
  performance: { now: () => Date.now() },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.top = sandbox; // a top-level page; the autopilot skips subframes

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "pureclick_autopilot.js" });

const picker = sandbox.window.PureClick?.picker;
assert.ok(picker, "autopilot did not expose window.PureClick.picker");

const seat = (id, grade, gradeName, rowNo, seatNo, extra = {}) => ({
  seatInfoId: id,
  seatGrade: String(grade),
  seatGradeName: gradeName,
  rowNo,
  seatNo,
  blockKey: extra.blockKey ?? "001:001",
  seatGroupId: extra.seatGroupId ?? null,
  label: `[${gradeName}] ${rowNo} ${seatNo}`,
});

const tests = {
  "grade order matches by name, not per-show code"() {
    const musical = [
      seat("a", 1, "VIP석", "1열", "1"),
      seat("b", 2, "R석", "1열", "2"),
      seat("c", 3, "S석", "1열", "3"),
    ];
    const ranked = picker.rankCandidates(musical, ["S석", "VIP석"]);
    assert.deepEqual(ranked.map((s) => s.seatGradeName), ["S석", "VIP석", "R석"]);

    // Same preference against a concert where grade "1" means something else.
    const concert = [
      seat("d", 1, "EARLY ENTRY PACKAGE", "1열", "1"),
      seat("e", 3, "스탠딩 P", "1열", "2"),
      seat("f", 7, "지정석 S석", "1열", "3"),
    ];
    assert.equal(picker.rankCandidates(concert, ["S석"])[0].seatGradeName, "지정석 S석");
  },

  "empty grade order keeps every seat"() {
    const seats = [seat("a", 1, "VIP석", "1열", "1"), seat("b", 2, "R석", "1열", "2")];
    assert.equal(picker.rankCandidates(seats, []).length, 2);
  },

  "strict mode drops unmatched grades"() {
    const seats = [seat("a", 1, "VIP석", "1열", "1"), seat("b", 2, "R석", "1열", "2")];
    assert.equal(picker.rankCandidates(seats, ["VIP석"]).length, 2);
    const strict = picker.rankCandidates(seats, ["VIP석"], [], { strict: true });
    assert.deepEqual(strict.map((s) => s.seatGradeName), ["VIP석"]);
  },

  "block filter keeps only requested zones"() {
    const seats = [
      seat("a", 1, "R석", "1열", "1", { blockKey: "001:001" }),
      seat("b", 1, "R석", "1열", "2", { blockKey: "009:009" }),
    ];
    const ranked = picker.rankCandidates(seats, [], ["001:001"]);
    assert.deepEqual(ranked.map((s) => s.seatInfoId), ["a"]);
  },

  "package seats form one atomic unit"() {
    const seats = [
      seat("g1", 1, "VIP PACKAGE", "1열", "1", { seatGroupId: "pkg" }),
      seat("g2", 1, "VIP PACKAGE", "1열", "2", { seatGroupId: "pkg" }),
      seat("s1", 2, "R석", "2열", "1"),
      seat("s2", 2, "R석", "2열", "2"),
    ];
    assert.deepEqual(picker.groupCandidates(seats).map((u) => u.length), [2, 1, 1]);
    assert.deepEqual(picker.selectSeatUnit(seats, 2).map((s) => s.seatInfoId), ["g1", "g2"]);
    // A 2-seat package cannot fill a 1-ticket order.
    assert.deepEqual(picker.selectSeatUnit(seats, 1).map((s) => s.seatInfoId), ["s1"]);
  },

  "wrong-sized group is skipped for loose adjacent seats"() {
    const seats = [
      seat("t1", 1, "테이블석", "1열", "1", { seatGroupId: "t4" }),
      seat("t2", 1, "테이블석", "1열", "2", { seatGroupId: "t4" }),
      seat("t3", 1, "테이블석", "1열", "3", { seatGroupId: "t4" }),
      seat("s1", 2, "일반석", "2열", "1"),
      seat("s2", 2, "일반석", "2열", "2"),
    ];
    assert.deepEqual(picker.selectSeatUnit(seats, 2).map((s) => s.seatInfoId), ["s1", "s2"]);
  },

  "adjacent picking prefers consecutive seat numbers"() {
    const seats = [
      seat("a", 1, "R석", "1열", "1"),
      seat("c", 1, "R석", "1열", "5"),
      seat("b", 1, "R석", "1열", "2"),
    ];
    assert.deepEqual(picker.selectSeatUnit(seats, 2, true).map((s) => s.seatNo), ["1", "2"]);
  },

  "sports products use the SPORTS seat type"() {
    assert.equal(picker.resolveSeatType({ isSportOneStop: true }), "SPORTS");
    assert.equal(picker.resolveSeatType({ kindOfGoods: "01007" }), "SPORTS");
    assert.equal(picker.resolveSeatType({ kindOfGoods: "01011" }), "DEFAULT");
  },

  "status hex decodes MSB-first, four seats per character"() {
    assert.deepEqual(picker.decodeStatusMask("8"), [true, false, false, false]);
    assert.deepEqual(picker.decodeStatusMask("1"), [false, false, false, true]);
    assert.equal(picker.decodeStatusMask("0FFFF01FFFFC").length, 48);
    assert.deepEqual(picker.decodeStatusMask(""), []);
  },

  "seatStatus envelope unwraps to per-block masks"() {
    const masks = picker.parseSeatStatus({ data: ["F0", "0F"] });
    assert.equal(masks.length, 2);
    assert.deepEqual(masks[0].slice(0, 4), [true, true, true, true]);
    assert.deepEqual(masks[1].slice(0, 4), [false, false, false, false]);
    assert.deepEqual(picker.parseSeatStatus(null), []);
  },



  "the seat index survives without a MutationObserver"() {
    // Not every venue exposes a subtree we can observe. When one does not, the
    // watch must fall back to scanning rather than run on an empty index and
    // decide nothing is clickable.
    const { race } = sandbox.window.PureClick;
    const node = {
      __reactProps$test: { seat: { seatInfoId: "Z9", seatGrade: "2" }, blockKey: "022:001" },
      getAttribute: () => "3",
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 6, height: 6 }),
    };
    const originalQsa = sandbox.document.querySelectorAll;
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? [node] : [];
      const index = race.liveSeatIndex();
      // Not `instanceof Map`: the vm has its own realm, so its Map is a
      // different constructor than this file's. Behaviour is what matters.
      assert.equal(typeof index?.get, "function", "an index is returned either way");
      assert.equal(index.get("Z9"), node, "and it finds the drawn seat");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
    }
  },

  // --- Reaching a seat -----------------------------------------------------

  "opening a 구역 makes its seats clickable without another poll"() {
    // The block-switch tax. Travelling to a seat — leaveBlock, enterBlock,
    // fitBlock — is the largest cost in 취켓팅 once one frees, and having paid
    // it the loop then did `candidates = []; continue`, throwing the ranking
    // away and going back round for another poll before clicking anything.
    // clickableAmong is what lets the tick finish the job it started.
    const { race } = sandbox.window.PureClick;
    const seat = { seatInfoId: "S1", blockKey: "022:001", seatGrade: "1" };
    const node = {
      __reactProps$t: { seat: { seatInfoId: "S1", seatGrade: "1" }, blockKey: "022:001" },
      getAttribute: () => "3",
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 6, height: 6 }),
      isConnected: true,
    };
    const originalQsa = sandbox.document.querySelectorAll;
    try {
      // Before the 구역 opens the map has drawn nothing.
      sandbox.document.querySelectorAll = () => [];
      race.rebuildSeatIndex();
      assert.deepEqual(race.clickableAmong([seat]), [], "nothing is drawn yet");

      // Opening it mounts the seat; the same candidate is now clickable
      // against a freshly read index, with no request in between.
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? [node] : [];
      race.rebuildSeatIndex();
      assert.deepEqual(
        race.clickableAmong([seat]).map((s) => s.seatInfoId), ["S1"],
        "the seat must be reachable in the same pass that opened its 구역",
      );
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      race.rebuildSeatIndex();
    }
  },

  // --- The whole-venue trigger ---------------------------------------------

  "the trigger fires once per change, and only when it can see"() {
    // Detection costs one seatStatus request per two blocks — 58ms each, hard
    // capped at two keys — so a 34-block venue spends 17 requests and ~4.4s to
    // answer a question the remaining-seat feed answers in one and ~132ms.
    // Measured agreement: round 097 read 202 both ways.
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    const set = (trigger) => {
      state.watchTrigger = trigger;
      return race.triggerFired();
    };
    state.triggerActedAt = 0;
    try {
      assert.equal(set({ usable: true, changed_at: 0 }), false, "no change yet is not an event");
      assert.equal(set({ usable: true, changed_at: 500 }), true, "a change fires");
      assert.equal(set({ usable: true, changed_at: 500 }), false, "and only once");
      assert.equal(set({ usable: true, changed_at: 900 }), true, "the next one fires again");

      // Every way the trigger can be blind must leave the sweep untouched: the
      // show hides its remains, the round is not on sale, or the panel could
      // not reach the feed. Measured: rounds reporting remainCnt 0 still had
      // 600+ seats on the map, so a blind trigger must never mean "stop".
      state.triggerActedAt = 0;
      for (const blind of [
        { usable: false, changed_at: 9000 },
        { changed_at: 9000 },
        null,
        "nonsense",
      ]) {
        assert.equal(set(blind), false, JSON.stringify(blind));
      }
    } finally {
      state.watchTrigger = null;
      state.triggerActedAt = 0;
    }
  },

  "a burst sweeps the whole watch in one tick"() {
    // The budget is an average. Spending almost nothing while the venue is
    // quiet is what makes it affordable to spend the whole sweep at the moment
    // something actually frees.
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    const blocks = [];
    for (let i = 1; i <= 8; i += 1) {
      blocks.push({
        blockKey: `001:00${i}`,
        mask: null,
        seats: [{ seatInfoId: `s${i}`, seatGrade: "1", seatGradeName: "R석", rowNo: "A",
                  seatNo: "1", isExposable: true, posLeft: 10, posTop: 10 }],
      });
    }
    const originalFetch = sandbox.fetch;
    const asked = [];
    sandbox.fetch = async (url) => {
      const keys = [...String(url).matchAll(/blockKeys=([^&]+)/g)].map((m) => decodeURIComponent(m[1]));
      asked.push(keys);
      return { ok: true, status: 200, json: async () => keys.map(() => "0") };
    };
    const initData = { goods: { goodsCode: "G", placeCode: "P" }, playSeq: { playSeq: "001" } };
    state.lastBlocks = blocks;
    try {
      state.catchCursor = 0;
      asked.length = 0;
      return (async () => {
        const watched = blocks.map((block) => block.blockKey);
        await race.pollFreedSeats(initData, watched, {});
        const paced = asked.flat().length;

        state.catchCursor = 0;
        asked.length = 0;
        await race.pollFreedSeats(initData, watched, {}, { burst: true });
        const burst = asked.flat().length;

        assert.equal(paced, 2, "a normal tick looks at two blocks");
        assert.equal(burst, 8, "a burst looks at all of them");
        assert.ok(burst > paced, "or the trigger buys nothing");
      })().finally(() => {
        sandbox.fetch = originalFetch;
        state.lastBlocks = [];
        state.catchCursor = 0;
      });
    } catch (error) {
      sandbox.fetch = originalFetch;
      throw error;
    }
  },

  "a seat in the open 구역 beats a nearer one we would have to travel to"() {
    // Leaving a 구역 and entering another is the most expensive thing the loop
    // does — up to eight zoom-out clicks at 250ms each, then an entry that
    // settles for up to 900ms, then a fit. Fitting the 구역 already open costs
    // a fraction of that. Ranking by distance alone sent the macro on that
    // journey for a seat one row nearer, which by arrival is usually gone.
    const { race } = sandbox.window.PureClick;
    const nearer = { seatInfoId: "far-block", blockKey: "001:001", posTop: 0 };
    const reachable = { seatInfoId: "same-block", blockKey: "022:001", posTop: 90 };
    const ranked = [nearer, reachable];

    assert.equal(
      race.aimForCandidates(ranked, "022:001").seatInfoId, "same-block",
      "must aim at the 구역 already open",
    );
    // With nothing open, or nothing here, distance decides as before.
    assert.equal(race.aimForCandidates(ranked, "").seatInfoId, "far-block");
    assert.equal(race.aimForCandidates(ranked, "099:009").seatInfoId, "far-block");
    assert.equal(race.aimForCandidates([], "022:001"), null);
  },

  "what a map move costs is recorded, not assumed"() {
    // The settle budgets these run against (900/700/250ms) are ceilings someone
    // chose; the only real figure anywhere was a 389ms note in a comment. The
    // travel is the biggest latency in 취켓팅, so it needs measuring.
    const { race } = sandbox.window.PureClick;
    race.state.mapMoves = {};
    return (async () => {
      await race.noteMapMove("enterBlock", "022:001", async () => ({ ok: true }));
      await race.noteMapMove("enterBlock", "022:002", async () => ({ ok: false }));
      const seen = race.state.mapMoves.enterBlock;
      assert.equal(seen.n, 2, "every move is counted");
      assert.equal(seen.failed, 1, "including the ones that did not work");
      assert.ok(seen.totalMs >= 0 && seen.worstMs >= 0, "with real durations");
      race.state.mapMoves = {};
    })();
  },

  "a configured speed can only slow the watch, never override its budget"() {
    // The fix that never took effect.
    //
    // The panel sent speed_ms=400 unconditionally and floored it at 400 on
    // load, and the watch took max(budget, configured) — so raising the pace to
    // 200ms changed nothing at all: measured live at "400ms 간격" with a
    // 12-tick sweep, i.e. 4.8s, exactly the speed it had before the work.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const at = source.indexOf("const askedMs =");
    assert.ok(at > 0, "the configured value must be read separately");
    const region = source.slice(at, at + 400);
    assert.match(region, /askedMs > 0 \? askedMs : CATCH_MIN_POLL_MS/,
      "an absent or zero speed must fall back to the budget, not to 400");

    const panel = readFileSync(resolve(here, "../mac/pureclick.py"), "utf8");
    assert.ok(
      !/max\(int\(preferences\.speed_ms\), 400\)/.test(panel),
      "the panel must not floor the watch at 400ms on load",
    );
  },


  "a seat is taken by clicking it, and by nothing else"() {
    // The macro was generating its own 이선좌.
    //
    // An API hold looked faster — no waiting for the map to draw — but the
    // server took the hold while React never learned of it, so the cart stayed
    // empty and the hold had to be handed back before clicking the seat the
    // ordinary way. That release is a network call that fails silently, and
    // even when it works it races the click behind it: click a seat we are
    // still holding and the server answers 이미 선점된 좌석입니다.
    //
    // One rendered circle, clicked once. Nothing before it.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    assert.ok(!/apiHoldFirst/.test(source), "no API hold may be attempted before a click");
    assert.ok(!/api_hold_first/.test(source), "and no flag left to switch one back on");

    // preselect may still exist for the paths that legitimately need it, but
    // selectSeats must not reach for it.
    const start = source.indexOf("async function selectSeats(");
    const body = source.slice(start, source.indexOf("\n  async function ", start + 10));
    assert.ok(
      !/preselectSeat\(|bulkPreselectSeats\(/.test(body),
      "selectSeats must click, never hold first",
    );
  },

  "it never presses anything that leaves the page": async () => {
    // This actually happened: while 취켓팅 was running the macro pressed
    // 마이페이지 and the exit link.
    //
    // mapAreaControls skipped its position filter when the map element could
    // not be found — `if (box)` — so it fell back to every button and link in
    // the document, it included `a` anchors, and leaveBlockToVenue clicked them
    // one at a time until the seats disappeared. Navigating away makes seats
    // disappear, so pressing 마이페이지 scored as success.
    const { race } = sandbox.window.PureClick;
    const pressed = [];
    const control = (label, tag = "BUTTON") => ({
      tagName: tag,
      textContent: label,
      getAttribute: (name) => (name === "class" ? "hdr" : ""),
      getBoundingClientRect: () => ({ left: 20, top: 20, right: 90, bottom: 50, width: 70, height: 30 }),
      querySelectorAll: () => [],
      closest: (sel) => (tag === "A" && /a|href/.test(sel) ? {} : null),
      click() { pressed.push(label); },
    });
    const chrome = [
      control("마이페이지"),
      control("예매확인/취소"),
      control("로그아웃", "A"),
      control("나가기"),
    ];

    const originalQsa = sandbox.document.querySelectorAll;
    const originalQs = sandbox.document.querySelector;
    try {
      // No seat map anywhere — the exact state that made it fall back to the
      // whole document.
      sandbox.document.querySelector = () => null;
      sandbox.document.querySelectorAll = () => chrome;

      assert.deepEqual(
        race.mapAreaControls(),
        [],
        "with no map there are no controls: guessing is how it pressed 마이페이지",
      );

      // And the caller must not press anything either.
      await race.leaveBlockToVenue({ settleMs: 20 });
      assert.deepEqual(pressed, [], `nothing may be pressed, but it pressed: ${pressed.join(",")}`);
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      sandbox.document.querySelector = originalQs;
    }
  },

  "the watch opens its 구역 before waiting, not after a seat appears"() {
    // What actually loses the race.
    //
    // Detection is fast, but a freed seat that is not drawn cannot be clicked:
    // the map mounts only what is in the viewport, so the run opened the 구역
    // (389ms), fitted it (250ms) and waited a further poll tick before it could
    // even try — the better part of a second spent arriving somewhere it could
    // have been standing all along, while anyone already there clicks in a
    // frame. Preparing at 감시 시작 is the difference.
    //
    // Structural: driving it needs a live map, but the ordering is the claim.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const loopAt = source.indexOf("while (seatState.attempts < maxAttempts");
    const prepAt = source.indexOf("if (isCatch && !config.auto_assign) {");
    assert.ok(prepAt > 0, "the watch must prepare its 구역");
    assert.ok(prepAt < loopAt, "and do it before the loop, not on the first freed seat");

    const prep = source.slice(prepAt, loopAt);
    assert.match(prep, /enterBlockForSeats\(target\)/, "it opens the watched 구역");
    assert.match(prep, /fitBlockToView\(\)/, "and mounts all of it");
    assert.match(prep, /currentOpenBlock\(\)/, "and does nothing if already there");
  },

  "the watch polls the 구역 you chose, not the whole venue": async () => {
    // How a macro loses a race it is fast enough to win.
    //
    // This ignored its blockKeys argument, re-derived from every block in the
    // venue, and polled two per tick on a rotating cursor — so a 43-block
    // stadium took 22 ticks to come back round to any one block. A seat
    // freeing just behind the cursor waited a whole sweep, nearly nine
    // seconds. The 감시 구역 was applied only as a filter after fetching, so
    // drawing one narrowed the results but never the work.
    const { race } = sandbox.window.PureClick;
    const state = race.state;

    // A venue of 20 blocks, of which the user watches two.
    const blocks = [];
    for (let i = 1; i <= 20; i += 1) {
      const key = `001:${String(i).padStart(3, "0")}`;
      blocks.push({
        blockKey: key,
        mask: [false],
        seats: [{ seatInfoId: `${key}-1`, seatGrade: "1", seatGradeName: "R석",
                  rowNo: "A", seatNo: "1", isExposable: true, posLeft: 10, posTop: 10 }],
      });
    }

    const asked = [];
    const originalFetch = sandbox.fetch;
    sandbox.fetch = async (url) => {
      const keys = [...String(url).matchAll(/blockKeys=([^&]+)/g)].map((m) => decodeURIComponent(m[1]));
      asked.push(...keys);
      // Nothing freed; we are measuring what gets asked about, not the answer.
      return { ok: true, status: 200, json: async () => keys.map(() => "0") };
    };
    state.lastBlocks = blocks;
    state.catchCursor = 0;
    try {
      const watched = ["001:007", "001:008"];
      await race.pollFreedSeats(
        { goods: { goodsCode: "G", placeCode: "P" }, playSeq: { playSeq: "001" } },
        watched,
        {},
      );
      assert.ok(asked.length > 0, "the watch must actually ask about something");
      assert.deepEqual(
        [...new Set(asked)].sort(),
        watched,
        `only the watched 구역 should be polled, got ${[...new Set(asked)].join(",")}`,
      );
      // And both of them in a single tick, so nothing waits for a cursor.
      assert.equal(state.catchSweepTicks, 1, "a small watch is swept in one tick");
      assert.equal(state.catchWatchedBlocks, 2);
    } finally {
      sandbox.fetch = originalFetch;
      state.lastBlocks = [];
      state.catchCursor = 0;
    }
  },

  // --- Overhearing the page ------------------------------------------------
  //
  // The 예매 창 fetches seatStatus for its own drawing. The network watch
  // covered /onestop/api/seats/* but seatStatus sits beside that path, not
  // under it, so the one kind of traffic worth overhearing was the one kind
  // not matched. These observations cost no request, so they are not paced by
  // the gateway budget that caps our own sweep.

  "a seat the page's own traffic shows opening is caught without a request"() {
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    const block = {
      blockKey: "001:001",
      // One seat taken, one free: the mask we start from.
      mask: [false, true],
      seats: [
        { seatInfoId: "s1", seatGrade: "1", seatGradeName: "R석", rowNo: "A", seatNo: "1",
          isExposable: true, posLeft: 10, posTop: 10 },
        { seatInfoId: "s2", seatGrade: "1", seatGradeName: "R석", rowNo: "A", seatNo: "2",
          isExposable: true, posLeft: 20, posTop: 10 },
      ],
    };
    state.lastBlocks = [block];
    state.pageFreed.length = 0;
    state.pageStatusSeen = 0;
    try {
      // Four seats per hex character, most-significant bit first: "C" = 1100,
      // so both of this block's two seats are free. s1 just flipped.
      race.notePageSeatStatus(
        "https://tickets.interpark.com/onestop/api/seatStatus?goodsCode=G&blockKeys=001%3A001",
        JSON.stringify(["C"]),
      );
      assert.equal(state.pageStatusSeen, 1, "the response must be counted");
      assert.deepEqual(
        state.pageFreed.map((seat) => seat.seatInfoId), ["s1"],
        "only the seat that changed is reported, not every free seat",
      );
      assert.equal(block.mask[0], true, "and the stored mask moves with it");
    } finally {
      state.lastBlocks = [];
      state.pageFreed.length = 0;
    }
  },

  "a first sighting is not an opening"() {
    // Without a previous mask every free seat would read as newly freed, and
    // the watch would fire on the whole venue the moment it started.
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    const block = {
      blockKey: "001:001",
      mask: null,
      seats: [{ seatInfoId: "s1", seatGrade: "1", seatGradeName: "R석", rowNo: "A",
                seatNo: "1", isExposable: true, posLeft: 10, posTop: 10 }],
    };
    state.lastBlocks = [block];
    state.pageFreed.length = 0;
    try {
      race.notePageSeatStatus(
        "/onestop/api/seatStatus?blockKeys=001%3A001", JSON.stringify(["8"]),
      );
      assert.deepEqual(state.pageFreed, [], "nothing opened; we simply had not looked before");
      assert.equal(block.mask[0], true, "but the baseline is now recorded");
    } finally {
      state.lastBlocks = [];
      state.pageFreed.length = 0;
    }
  },

  "unreadable page traffic is ignored rather than thrown from"() {
    // This runs inside a network callback on the booking page. Throwing there
    // would surface as a page error on traffic that is none of our business.
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    state.lastBlocks = [{ blockKey: "001:001", mask: [false], seats: [] }];
    try {
      for (const [url, body] of [
        ["/onestop/api/seatStatus?blockKeys=001%3A001", "not json"],
        ["/onestop/api/seatStatus", JSON.stringify(["1"])],
        ["::::", JSON.stringify(["1"])],
      ]) {
        race.notePageSeatStatus(url, body);
      }
      assert.deepEqual(state.pageFreed, []);
    } finally {
      state.lastBlocks = [];
      state.pageFreed.length = 0;
    }
  },

  "a sold-out block yields no candidates despite isExposable"() {
    // Maroon 5 shape: every seat exposable, one bit set in the live bitmap.
    const seats = Array.from({ length: 8 }, (_, i) => ({
      seatInfoId: `g:p:001:${i}`,
      seatGrade: "1",
      seatGradeName: "스탠딩 P",
      rowNo: "A구역",
      seatNo: String(i),
      isExposable: true,
    }));

    const noMask = [{ blockKey: "001:001", seats, mask: null }];
    assert.equal(picker.collectFromBlocks(noMask, {}).length, 0);

    const mask = picker.decodeStatusMask("10"); // 0001 0000 -> index 3 free
    const withMask = [{ blockKey: "001:001", seats, mask }];
    const picked = picker.collectFromBlocks(withMask, {});
    assert.deepEqual(picked.map((s) => s.seatNo), ["3"]);
    assert.equal(picker.countFree(withMask), 1);
    // The watch's request budget, not a magic number. seatStatus takes two
    // blocks per call, and the gateway answers GATEWAY_ABUSE_BLOCKED with a
    // ~165s lockout if pushed — so what must hold is requests per second, and
    // sweep time is allowed to follow from how much is being watched.
    const perSecond =
      (picker.CATCH_MAX_REQUESTS_PER_TICK * 1000) / picker.CATCH_MIN_POLL_MS;
    assert.ok(
      perSecond <= 6,
      `polling at ${perSecond}/s risks a 165s lockout; keep it at or under 6/s`,
    );
    assert.ok(picker.CATCH_MIN_POLL_MS >= 100, "and never hammer below 100ms");
    assert.equal(picker.CATCH_LIVE_TRIES, 8);
  },

  // The post-selection modal is worded several ways. Reported live and
  // previously unmatched: "취소/환불 기간이 지난 예매를 선택했습니다…". Because it
  // went unrecognised it was never dismissed, and being modal it blocked the
  // seat map, so every later action failed with 좌석 요청이 잘못되었습니다.
  //
  // Matched only against a scoped, visible dialog node — NOT whole-page text,
  // where the same sentence also appears as static 예매 안내 copy and would make
  // the autopilot believe a modal was permanently up. The test below pins that
  // narrower page-text behaviour; the two must stay different.
  "the post-selection modal is matched however it is worded"() {
    const seen = (text) => sandbox.window.PureClick.guards.BOOKING_MODAL_COPY.test(text);

    assert.equal(
      seen("취소/환불 기간이 지난 예매를 선택했습니다. 이 일정은 예매 후 취소/환불이 불가능합니다."),
      true,
      "the wording that was actually blocking the map",
    );
    assert.equal(seen("취소/환불 안내"), true);
    assert.equal(seen("확인하고 예매하기"), true);

    // Ordinary seat-map chrome must not look like a modal.
    assert.equal(seen("좌석 선택 시간 9:25 등급 별 가격 선택 좌석"), false);
    assert.equal(seen("선택한 좌석이 없습니다."), false);
    assert.equal(seen(""), false);
  },

  // The confirm path must not read the sidebar before the page has had a chance
  // to fill it. NOL updates 선택 좌석 only once its own PreselectSeat resolves,
  // and advanceAfterSeatLock runs immediately after the map click — so an
  // instantaneous check reported "empty", unlocked a live hold, and the
  // end-of-run cleanup then released it. Observed as: PreselectSeat true, no
  // page:select at all, then BulkDeselectSeats.
  "an empty sidebar right after a click is not proof of no seat"() {
    const held = sandbox.window.PureClick.picker.pageHasSelectedSeats;
    const withText = (text) => {
      sandbox.document.body.innerText = text;
      return held();
    };

    // The moment after a click the sidebar still says empty.
    assert.equal(withText("선택 좌석 선택한 좌석이 없습니다."), false);
    // Once the page catches up it reports the seat, and only then may we confirm.
    assert.equal(withText("선택 좌석 1 R석 77,000원 선택 완료"), true);

    // The source must not bail on this check before awaiting the hold, or the
    // race returns. waitForSoftHoldIdle has to come first.
    const src = source;
    const fn = src.slice(src.indexOf("async function advanceAfterSeatLock"));
    const body = fn.slice(0, fn.indexOf("waitForSoftHoldIdle"));
    assert.ok(
      !/if \(!pageHasSelectedSeats\(\)\) \{[\s\S]{0,200}?noSeat: true/.test(body),
      "advanceAfterSeatLock must not bail on the sidebar before awaiting the soft hold",
    );
  },

  // A watch rect belongs to one show's coordinate space. Seat positions have no
  // common scale between shows — posTop spans 52-111 on one venue and 1168-1183
  // on another — so a box drawn on one map matches nothing on the next. Left
  // alone, the run reports 후보 없음 forever with a full seat map on screen.
  "a watch rect that matches no seat is ignored, not obeyed"() {
    const block = {
      blockKey: "001:001",
      mask: [true, true, true],
      seats: [0, 1, 2].map((i) => ({
        seatInfoId: `s${i}`,
        seatGrade: "1",
        seatGradeName: "R석",
        rowNo: "1열",
        seatNo: String(i),
        isExposable: true,
        posLeft: 100 + i,
        posTop: 1170,
      })),
    };

    // A rect from this venue keeps only what is inside it.
    const inside = picker.collectFromBlocks([block], {
      watch_rect: { left: 99, top: 1169, right: 101, bottom: 1171 },
    });
    assert.deepEqual(inside.map((s) => s.seatInfoId), ["s0", "s1"]);

    // A rect from a *different* venue contains no seats at all. Obeying it
    // means watching an empty region; the whole map is the honest fallback.
    const stale = picker.collectFromBlocks([block], {
      watch_rect: { left: 68, top: 99, right: 189, bottom: 130 },
    });
    assert.equal(stale.length, 3, "a rect matching nothing must not zero the map");

    // But a rect over real seats that simply has none free *right now* is the
    // ordinary state of 취켓팅 — you are waiting for one to open. Falling back
    // here would abandon the chosen area and take a seat somewhere else, which
    // is exactly what "it grabs a seat outside my area" looks like.
    const soldOut = {
      ...block,
      mask: [false, false, true],
      seats: block.seats.map((seat, i) =>
        i === 2 ? { ...seat, posLeft: 500, posTop: 1170 } : seat,
      ),
    };
    const waiting = picker.collectFromBlocks([soldOut], {
      watch_rect: { left: 99, top: 1169, right: 101, bottom: 1171 },
    });
    assert.deepEqual(
      waiting.map((s) => s.seatInfoId),
      [],
      "a full area must keep waiting, not spill outside the box",
    );
  },

  "payment buttons are never treated as advance buttons"() {
    const { COMMIT_BUTTON, ADVANCE_BUTTON } = sandbox.window.PureClick.guards;
    const commits = ["결제하기", "결제 하기", "결제완료", "입금하기", "구매하기", "주문완료", "결제진행"];
    for (const label of commits) {
      assert.ok(COMMIT_BUTTON.test(label), `should be recognised as commit: ${label}`);
      assert.ok(!ADVANCE_BUTTON.test(label), `must not be advanced through: ${label}`);
    }
    for (const label of ["다음", "다음 단계", "확인"]) {
      assert.ok(ADVANCE_BUTTON.test(label), `should advance: ${label}`);
      assert.ok(!COMMIT_BUTTON.test(label), `should not be a commit: ${label}`);
    }
    const { isBookingNoticeConfirm, isBookingNoticeCopy } = sandbox.window.PureClick.guards;
    assert.equal(isBookingNoticeConfirm("확인하고 예매하기"), true);
    assert.equal(isBookingNoticeConfirm("확인하고예매하기"), true);
    assert.equal(isBookingNoticeConfirm("확인"), false);
    assert.equal(isBookingNoticeConfirm("예매확인/취소"), false);
    assert.equal(isBookingNoticeConfirm("입력완료"), false);
    assert.equal(isBookingNoticeCopy("취소/환불 안내"), true);
    assert.equal(isBookingNoticeCopy("취소/환불 기간이 지난 예매를 선택했습니다"), false);
    assert.equal(isBookingNoticeCopy("확인하고 예매하기"), true);
    assert.equal(isBookingNoticeCopy("입력 완료. 결제 화면을 확인하세요."), false);
    assert.equal(isBookingNoticeCopy("선점됨. 파란 확인 버튼을 직접 눌러 주세요."), false);
  },

  // The macro no longer types captchas — the solver reported success on a
  // misread, submitting a wrong answer and carrying on. Only *detection*
  // remains, and it has to keep working: without it the run never learns to
  // wait for the human.
  //
  // isCaptchaPageCopy is deliberately narrow. It matches 문자를 입력해주세요 and
  // NOT 보안문자, because the sniper's own toast contains that word and would
  // otherwise make the page look permanently blocked by its own status line.
  "the captcha modal is detected without matching our own toast"() {
    const { isCaptchaPageCopy } = sandbox.window.PureClick.captcha;

    assert.equal(isCaptchaPageCopy("화면의 문자를 입력해주세요"), true);
    assert.equal(isCaptchaPageCopy("문자를 입력해주세요"), true);

    // Our own overlay copy must never read as a captcha.
    assert.equal(isCaptchaPageCopy("보안문자 — 예매 창에서 직접 입력하세요"), false);
    assert.equal(isCaptchaPageCopy("보안문자 감지 — 화면에 보이는 문자를 인식합니다."), false);
    assert.equal(isCaptchaPageCopy("스나이퍼"), false);
    assert.equal(isCaptchaPageCopy(""), false);
  },

  "seats beyond the mask length are not treated as free"() {
    const seats = Array.from({ length: 8 }, (_, i) => ({
      seatInfoId: `s${i}`,
      seatGrade: "1",
      seatGradeName: "R석",
      rowNo: "A",
      seatNo: String(i),
      isExposable: true,
    }));
    const blocks = [{ blockKey: "b", seats, mask: [true, true] }];
    assert.deepEqual(picker.collectFromBlocks(blocks, {}).map((s) => s.seatNo), ["0", "1"]);
  },

  // The seat map reports its own tally ("선택 좌석 4"). Reading it as a boolean
  // "is the panel empty" could only ever detect the first selection: from the
  // second attempt on, a good selection was reported as declined, so the loop
  // moved on and clicked another seat. Four seats piled onto a 매수 1 order
  // while every attempt was logged as a failure.
  "the page's own seat tally is read as a number, not empty-or-not"() {
    const count = picker.selectedSeatCount;
    const withText = (text) => {
      sandbox.document.body.innerText = text;
      return count();
    };
    assert.equal(withText("선택 좌석 4 VIP석 180,000원"), 4);
    assert.equal(withText("선택 좌석 1 VIP석"), 1);
    assert.equal(withText("선택 좌석  2"), 2);
    assert.equal(withText("선택한 좌석이 없습니다."), 0);
    // Unknown is distinct from zero: the caller must not treat it as success.
    assert.equal(withText("무관한 화면"), -1);
  },

  // The site answers 좌석 선택 도중 오류가 발생했습니다 when 선택 완료 is pressed
  // with nothing selected. advanceAfterSeatLock pressed it unconditionally, and
  // several paths reach it without a seat — a stale `locked`, bootRoute firing
  // on a transient notice match, or a selection the page dropped. So the macro
  // produced the error itself, which is why it never appeared on a manual click.
  "an empty cart is recognised so 선택 완료 is never pressed on it"() {
    const withText = (text) => {
      sandbox.document.body.innerText = text;
      return picker.seatSelectionEmpty();
    };
    assert.equal(withText("선택 좌석 선택한 좌석이 없습니다. 선택 완료"), true);
    assert.equal(withText("구매하실 좌석을 선택해주세요"), true);
    // A real selection must not read as empty, or the flow would stall forever.
    assert.equal(withText("선택 좌석 1 VIP석 180,000원 선택 완료"), false);
  },

  // 선택 완료 must require a positive cart count. An unreadable sidebar used to
  // fall through seatSelectionEmpty() === false and still click the button.
  "선택 완료 requires a positive page cart count"() {
    const withText = (text) => {
      sandbox.document.body.innerText = text;
      return picker.pageHasSelectedSeats();
    };
    assert.equal(withText("선택한 좌석이 없습니다. 선택 완료"), false);
    assert.equal(withText("무관한 화면"), false);
    assert.equal(withText("선택 좌석 1 VIP석 180,000원"), true);
  },

  // --- Aiming strategies -------------------------------------------------
  //
  // Every macro ordering seats by grade→row→seat converges on the same front
  // seat, so instances collide. The strategies aim different copies at
  // different parts of the map. Position comes from seatMeta's posLeft/posTop,
  // which toCandidate used to discard.

  // A 3x3 grid. posLeft 0/50/100 = left/middle/right, posTop 0/10/20 = rows.



  "the stage decides the order, and the side only narrows the field"() {
    // Every mode used to sort by x first, so 왼쪽부터 took the leftmost seat in
    // the building — possibly the back row — with the stage as a mere tiebreak.
    // Distance leads now; the side is a filter.
    const grid = [];
    for (const [li, left] of [0, 50, 100].entries()) {
      for (const [ti, top] of [0, 10, 20].entries()) {
        grid.push({
          seatInfoId: `s${li}${ti}`, seatGrade: "1", seatGradeName: "R석",
          rowNo: `${ti + 1}열`, seatNo: String(li + 1), blockKey: "b",
          seatGroupId: null, posLeft: left, posTop: top,
        });
      }
    }
    const first = (strategy) =>
      picker.rankCandidates(grid, [], [], { strategy, centerX: 50 })[0];

    // Whatever the side, never a seat further from the stage than necessary.
    for (const strategy of ["center", "left", "right"]) {
      assert.equal(first(strategy).posTop, 0, `${strategy} must start at the stage`);
    }
    assert.equal(first("center").posLeft, 50, "가운데 takes the middle of the front row");

    // The side narrows which seats are eligible at all.
    const lefts = picker.rankCandidates(grid, [], [], { strategy: "left", centerX: 50 });
    assert.ok(lefts.every((seat) => seat.posLeft <= 50), "왼쪽 never returns a right-side seat");
    const rights = picker.rankCandidates(grid, [], [], { strategy: "right", centerX: 50 });
    assert.ok(rights.every((seat) => seat.posLeft >= 50), "오른쪽 never returns a left-side seat");
  },

  // The Redoor shape: side blocks that run the full depth of the house, so a
  // far-left seat sits level with the front row while being right out at the
  // wall. Depth alone scored it identically to a centre seat in that row.
  "a seat level with the front row loses to one actually nearer the stage"() {
    const { race } = sandbox.window.PureClick;
    const seats = [];
    const add = (block, x0, x1, y0, y1) => {
      for (let x = x0; x <= x1; x += 6)
        for (let y = y0; y <= y1; y += 6)
          seats.push({
            seatInfoId: `${block}-${x}-${y}`, seatGrade: "1", seatGradeName: "R석",
            rowNo: String(y), seatNo: String(x), blockKey: "b", seatGroupId: null,
            posLeft: x, posTop: y,
          });
    };
    add("F1", 200, 280, 60, 190);   // floor, under the stage
    add("F2", 300, 380, 60, 190);   // the gap between these two is the aisle
    add("F3", 200, 380, 200, 260);
    add("A", 40, 110, 60, 280);     // side blocks, level with the front row
    add("E", 470, 540, 60, 280);    // yet out at the wall

    const stage = race.stagePoint([{ blockKey: "b", seats }]);
    assert.equal(stage.y, 60, "the stage sits at the front row");
    assert.ok(Math.abs(stage.x - 290) <= 10, `and over the middle of it, got ${stage.x}`);

    // The case that shows depth is measuring the wrong thing. Only two seats
    // free: one at the wall level with the front row, one dead centre but well
    // back. Depth picks the wall seat (60 < 190); distance picks the centre
    // seat, which is genuinely the nearer of the two to the stage.
    const wall = seats.find((s) => s.posLeft === 40 && s.posTop === 60);
    // The deepest seat in F1, hard against the aisle: dead centre, well back.
    const deepCentre = seats
      .filter((s) => s.posLeft === 278)
      .reduce((a, b) => (b.posTop > a.posTop ? b : a));
    assert.ok(wall && deepCentre, "both seats exist in the layout");
    const ranked = picker.rankCandidates([wall, deepCentre], [], [], {
      strategy: "center", centerX: 290, stage,
    });
    assert.equal(
      ranked[0].seatInfoId, deepCentre.seatInfoId,
      "took the seat at the edge of the house over one nearer the stage",
    );

    // And with the whole house free it still takes the front of the floor —
    // the aisle between F1 and F2 no longer being anything the ordering sees.
    const best = picker.rankCandidates(seats, [], [], {
      strategy: "center", centerX: 290, stage,
    })[0];
    assert.equal(best.posTop, 60, `must take the front row, took y=${best.posTop}`);
    assert.ok(
      best.posLeft >= 200 && best.posLeft <= 380,
      `and from the floor under the stage, took x=${best.posLeft}`,
    );
  },

  // The stage has to be a fixed landmark. Derived from whatever happens to be
  // free, it would drift as the house sells and reorder the seats underneath
  // itself — the ranking would change without a single seat moving.
  "the stage does not move as seats sell"() {
    const { race } = sandbox.window.PureClick;
    const seats = [];
    for (let x = 0; x <= 100; x += 10)
      for (let y = 0; y <= 60; y += 10)
        seats.push({
          seatInfoId: `x${x}y${y}`, seatGrade: "1", seatGradeName: "R석",
          rowNo: `${y}열`, seatNo: String(x), blockKey: "b", seatGroupId: null,
          posLeft: x, posTop: y,
        });
    const all = [{ blockKey: "b", seats }];
    const stage = race.stagePoint(all);

    // Sell the whole front and the left half of the house.
    const remaining = seats.filter((s) => s.posTop >= 30 && s.posLeft >= 60);
    const drifted = race.stagePoint([{ blockKey: "b", seats: remaining }]);
    assert.notDeepEqual(
      { x: drifted.x, y: drifted.y }, { x: stage.x, y: stage.y },
      "this venue must be one where a free-seat-derived stage would drift",
    );

    // The order of two survivors is the same before and after that sale.
    const pair = [remaining[0], remaining[remaining.length - 1]];
    const order = (point) =>
      picker.rankCandidates(pair, [], [], { strategy: "center", centerX: 50, stage: point })
        .map((s) => s.seatInfoId);
    assert.deepEqual(order(stage), order(race.stagePoint(all)), "ranking must be stable");
  },

  // The stage is over the *front*, not over the middle of the building. On a
  // house whose back fans out wider than its front — or that has extra seating
  // down one side only — those are different places, and using the whole-house
  // middle aims the macro at the middle of the back block.
  "the stage sits over the front rows, not the middle of the house"() {
    const { race } = sandbox.window.PureClick;
    const seats = [];
    const add = (x0, x1, y0, y1) => {
      for (let x = x0; x <= x1; x += 10)
        for (let y = y0; y <= y1; y += 10)
          seats.push({
            seatInfoId: `x${x}y${y}`, seatGrade: "1", seatGradeName: "R석",
            rowNo: `${y}열`, seatNo: String(x), blockKey: "b", seatGroupId: null,
            posLeft: x, posTop: y,
          });
    };
    add(100, 200, 0, 20);    // a narrow front, where the stage faces
    add(100, 500, 40, 100);  // a back that fans out to one side

    const stage = race.stagePoint([{ blockKey: "b", seats }]);
    assert.equal(stage.x, 150, `stage over the front rows, got x=${stage.x}`);

    const frontEdge = seats.find((s) => s.posLeft === 200 && s.posTop === 0);
    const backMiddle = seats.find((s) => s.posLeft === 300 && s.posTop === 40);
    const ranked = picker.rankCandidates([frontEdge, backMiddle], [], [], {
      strategy: "center", centerX: 300, stage,
    });
    assert.equal(
      ranked[0].seatInfoId, frontEdge.seatInfoId,
      "a front-row seat must beat one out in the middle of the back block",
    );
  },

  "a plain venue still takes front centre"() {
    const { race } = sandbox.window.PureClick;
    const seats = [];
    for (let x = 0; x <= 100; x += 10)
      for (let y = 0; y <= 60; y += 10)
        seats.push({
          seatInfoId: `x${x}y${y}`, seatGrade: "1", seatGradeName: "R석",
          rowNo: `${y}열`, seatNo: String(x), blockKey: "b", seatGroupId: null,
          posLeft: x, posTop: y,
        });
    const stage = race.stagePoint([{ blockKey: "b", seats }]);
    const best = picker.rankCandidates(seats, [], [], {
      strategy: "center", centerX: 50, stage,
    })[0];
    assert.equal(best.posTop, 0, "front row");
    assert.equal(best.posLeft, 50, "and centre");

    // 왼쪽 keeps to its half and takes the nearest seat to the stage in it.
    const lefts = picker.rankCandidates(seats, [], [], {
      strategy: "left", centerX: 50, stage,
    });
    assert.ok(lefts.every((s) => s.posLeft <= 50), "왼쪽 never returns a right-side seat");
    assert.equal(lefts[0].posTop, 0, "and starts at the front");
    assert.equal(lefts[0].posLeft, 50, "at the stage end of its half");
  },

  // A seat the map gave no coordinates for must fall to the back of the queue,
  // not to the front — Infinity sorts last, but only if it stays a number.
  "seats without coordinates sort last"() {
    const seats = [
      { seatInfoId: "nowhere", seatGrade: "1", seatGradeName: "R석", rowNo: "1열",
        seatNo: "1", blockKey: "b", seatGroupId: null, posLeft: null, posTop: null },
      { seatInfoId: "back", seatGrade: "1", seatGradeName: "R석", rowNo: "9열",
        seatNo: "9", blockKey: "b", seatGroupId: null, posLeft: 50, posTop: 900 },
    ];
    const ranked = picker.rankCandidates(seats, [], [], {
      strategy: "center", centerX: 50, stage: { x: 50, y: 0 },
    });
    assert.equal(ranked[0].seatInfoId, "back", "a placed seat beats an unplaced one");
    assert.equal(ranked.length, 2, "but the unplaced seat is still catchable");
  },

  // --- Getting into the queue --------------------------------------------

  // acquireWaitingUrl is built to start before the open and retry across the
  // boundary, because one perfectly-timed request loses to a clock error of a
  // few tens of milliseconds or one dropped packet. That lead was dead code:
  // runArmScheduler awaited the full deadline before ever calling it, so its
  // pre-wait loop was always already past and the first request went out *at*
  // the open, never before it.
  "the entry scheduler stops short of the open by the retry loop's lead"() {
    const { race } = sandbox.window.PureClick;
    const target = 1_800_000_000;
    const start = race.armEntryStartUnix({ target_server_unix: target });

    assert.ok(start < target, "must be ready before the open, not at it");
    // Unix seconds are floats, so this lands at 400.0000009 rather than 400.
    assert.ok(
      Math.abs((target - start) * 1000 - race.ENTRY_LEAD_MS) < 0.01,
      `head start ${(target - start) * 1000}ms must match the loop's ${race.ENTRY_LEAD_MS}ms lead`,
    );
    assert.ok(race.ENTRY_LEAD_MS > 0, "a zero lead is the bug this replaced");
  },

  // The queue endpoint answers in 11ms warm, so a flat 80ms poll left ~69ms of
  // every cycle idle — the show could open and we would not notice for up to
  // 80ms. Asking hard for the whole 15s window instead is ~50 requests/second
  // against the gateway that answers GATEWAY_ABUSE_BLOCKED with a ~165s
  // lockout, at the one moment a lockout cannot be recovered from. So the
  // density goes only where it buys something.
  "the queue poll is dense only across the open, not for the whole window"() {
    const { race } = sandbox.window.PureClick;
    const at = (ms) => race.waitingIntervalAt(ms);

    // Before the open the answer cannot be yes; these requests only keep the
    // connection warm and prove the session is good while there is time to act.
    assert.equal(at(-400), 100, "cheap while it cannot succeed");
    assert.equal(at(-101), 100);

    // The window that decides the queue position.
    assert.equal(at(-100), 20, "dense from just before the open");
    assert.equal(at(0), 20);
    assert.equal(at(599), 20);

    // It did not open on time; settle down rather than hammer for 15 seconds.
    assert.equal(at(600), 80, "backs off after the boundary");
    assert.equal(at(14000), 80);

    // The dense window must be short enough to bound the burst.
    const dense = race.WAITING_POLL_SHAPE.find(([, , ms]) => ms === 20);
    assert.ok(dense, "there is a dense band");
    const requests = (dense[1] - dense[0]) / dense[2];
    assert.ok(requests <= 60, `burst is ${requests} requests; keep it bounded`);
  },

  // What this endpoint returns *before* a show opens has never been observed,
  // and the two possibilities imply opposite strategies: if it hands out a
  // queue URL early then arriving at the open is already too late. The log is
  // what settles that.
  "every queue attempt is recorded with its offset from the open"() {
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    const arm = sandbox.window.PureClick.race;
    // armState is reachable through the published status.
    const armState = sandbox.window.PureClick.status().arm;
    assert.ok("waitingLog" in armState, "the log crosses the bridge with the arm");

    assert.equal(race.describeWaitingAnswer(""), "(빈 응답)");
    assert.equal(race.describeWaitingAnswer(null), "(빈 응답)");
    assert.equal(race.describeWaitingAnswer("N"), "N (대기열 없음)");
    assert.equal(race.describeWaitingAnswer("NP"), "NP (선예매 인증 필요)");
    assert.equal(race.describeWaitingAnswer("BL"), "BL (차단)");
    assert.match(race.describeWaitingAnswer("https://queue.example.com/x"),
                 /대기열 queue\.example\.com/);
  },

  "the attempt log keeps the boundary, not the tail"() {
    // A 15-second window at 20ms would push the entries around the flip off the
    // end of any fixed-size buffer read from the front. The flip is the only
    // part worth keeping.
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    const armState = sandbox.window.PureClick.status().arm;
    void armState;
    // Drive the recorder directly; it writes into the live arm state.
    sandbox.window.PureClick.race.noteWaitingAttempt(-400, "(빈 응답)", 11);
    const limit = race.WAITING_LOG_LIMIT;
    for (let i = 0; i < limit + 25; i += 1) {
      race.noteWaitingAttempt(i * 20 - 100, "(빈 응답)", 11);
    }
    race.noteWaitingAttempt(1234, "대기열 queue.example.com", 12);
    const log = sandbox.window.PureClick.status().arm.waitingLog;
    assert.equal(log.length, limit, `capped at ${limit}`);
    assert.equal(log[log.length - 1].outcome, "대기열 queue.example.com",
                 "the newest entry — the flip — must survive");
    assert.equal(log[log.length - 1].offsetMs, 1234, "with its offset from the open");
  },

  // The /waiting request is warm by the open — the 400ms lead sees to that —
  // but the navigation that follows goes to a different host, and that one is
  // cold at the exact moment it is claiming your place in line. Measured: a
  // cold TCP+TLS handshake to these hosts costs ~37ms.
  "the queue host is learned from a real entry and warmed on the next one"() {
    const { race } = sandbox.window.PureClick;
    const store = sandbox.window.localStorage;
    const before = store.getItem(race.QUEUE_HOST_KEY);
    try {
      store.removeItem(race.QUEUE_HOST_KEY);
      // Nothing learned yet: a first run cannot know the host, and must not guess.
      assert.equal(race.preconnectQueueHost(), "", "no host until one is seen");

      race.rememberQueueHost("https://queue.interpark.com/waiting?token=abc");
      assert.equal(store.getItem(race.QUEUE_HOST_KEY), "https://queue.interpark.com",
                   "the origin, not the whole URL");
      assert.equal(race.preconnectQueueHost(), "https://queue.interpark.com");

      // The non-URL answers are the common ones and none of them is a host.
      for (const answer of ["N", "NP", "BL", "", null, undefined]) {
        race.rememberQueueHost(answer);
        assert.equal(store.getItem(race.QUEUE_HOST_KEY), "https://queue.interpark.com",
                     `${answer} must not overwrite a known host`);
      }
    } finally {
      if (before === null) store.removeItem(race.QUEUE_HOST_KEY);
      else store.setItem(race.QUEUE_HOST_KEY, before);
    }
  },

  // Measured on a live session: armState came back all zeros — syncMs 0,
  // clockQuality "", no attempts, no error — because the 예매 창 was on the seat
  // map at step=price, where there is nothing to enter. Every early return in
  // runArmScheduler was silent, so an arm that refused looked exactly like one
  // that had never been asked.
  "an arm that refuses says why"() {
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const fn = source.slice(source.indexOf("async function runArmScheduler("));
    const body = fn.slice(0, fn.indexOf("\n  }\n"));
    const head = body.slice(0, body.indexOf("armState.running = true;"));

    // Every guard before the run starts must set a reason. armState.running is
    // the one exception: a second call while one is in flight is not a refusal.
    const returns = head.match(/^\s*if \(.*\) return[^;]*;/gm) || [];
    assert.ok(returns.length >= 4, `expected several guards, found ${returns.length}`);
    for (const line of returns) {
      if (/armState\.running/.test(line)) continue;
      assert.match(line, /refuse\(|armState\.lastError|return;$/,
                   `silent guard: ${line.trim()}`);
    }

    // And each attempt must start clean, or one refusal sits on screen forever
    // — lastError is otherwise only cleared inside fireEntry, which a refused
    // arm never reaches.
    assert.ok(head.indexOf('armState.lastError = ""') >= 0,
              "the reason from a previous attempt must be cleared");
    assert.ok(head.indexOf('armState.lastError = ""') < head.indexOf("refuse("),
              "cleared before any new reason is set");
  },

  "an entry is refused where there is nothing to enter"() {
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const fn = source.slice(source.indexOf("async function runArmScheduler("));
    const head = fn.slice(0, fn.indexOf("armState.running = true;"));
    assert.match(head, /isNolProductPage\(\)[^]*isGoodsPage\(\)/,
                 "the arm must check it is on a page an entry can happen from");
    assert.match(head, /isSeatPage\(\)/, "and name the seat map, which is where this was hit");
  },

  // localStorage is per-origin and the booking flow crosses two of them. The
  // script used to boot before the config was written, so bootRoute() read an
  // empty config on every fresh origin and nothing re-ran it — the 400ms
  // watcher only fires on a URL change, which had just happened.
  "config reaches the page before the route is decided"() {
    const host = readFileSync(resolve(here, "../mac/browser_host.py"), "utf8");
    const fn = host.slice(host.indexOf("def inject_autopilot("));
    const body = fn.slice(0, fn.indexOf("\ndef "));
    const applied = body.indexOf("apply_state(window)");
    const loaded = body.indexOf("evaluate_js(load_script())");
    assert.ok(applied >= 0 && loaded >= 0, "both steps must be present");
    assert.ok(applied < loaded, "storage must be written before the script boots");

    // And a config that lands later must be able to wake the route.
    assert.match(host, /configApplied/, "a late config must be able to re-decide the route");
  },

  "an entry with no target time has no start time to compute"() {
    const { race } = sandbox.window.PureClick;
    for (const arm of [{}, { target_server_unix: null }, { target_server_unix: "soon" }]) {
      assert.equal(race.armEntryStartUnix(arm), null, JSON.stringify(arm));
    }
  },

  "grade preference still outranks the seat order"() {
    const seats = [
      // Both on the left, so the side filter cannot be what decides it.
      { seatInfoId: "cheap", seatGrade: "2", seatGradeName: "S석", rowNo: "1열", seatNo: "1",
        blockKey: "b", seatGroupId: null, posLeft: 0, posTop: 0 },
      { seatInfoId: "vip", seatGrade: "1", seatGradeName: "VIP석", rowNo: "9열", seatNo: "9",
        blockKey: "b", seatGroupId: null, posLeft: 10, posTop: 90 },
    ];
    const ranked = picker.rankCandidates(seats, ["VIP석"], [], { strategy: "left", centerX: 50 });
    assert.equal(ranked[0].seatInfoId, "vip", "seat order only sorts within a grade tier");
  },

  "매수 books that many seats or none at all"() {
    // Both grouping paths ended in a slice, so with one seat free and 매수 2
    // they returned that single seat — and the caller clicked it and pressed
    // 선택 완료, booking one seat for someone who asked for two. In 취켓팅 that
    // is the *normal* case, because a cancellation frees one seat at a time,
    // so 매수 2 would have quietly booked one nearly every time it fired.
    const seat = (row, no) => ({
      seatInfoId: `${row}-${no}`, seatGrade: "1", seatGradeName: "R석",
      rowNo: row, seatNo: String(no), blockKey: "b", seatGroupId: null,
      posLeft: no * 10, posTop: 0,
    });
    const pick = (seats, qty) => picker.selectSeatUnit(seats, qty, true);

    // Enough seats: exactly the number asked for, consecutive where possible.
    assert.deepEqual(
      pick([seat("A", 1), seat("A", 2), seat("A", 3)], 2).map((s) => s.seatNo),
      ["1", "2"],
    );
    assert.equal(pick([seat("A", 1), seat("A", 2), seat("A", 3)], 3).length, 3);

    // Not enough: nothing, so the run waits for a real pair rather than
    // booking the wrong number.
    assert.deepEqual(pick([seat("A", 1)], 2), [], "one seat cannot satisfy 매수 2");
    assert.deepEqual(pick([seat("A", 1), seat("A", 2)], 4), [], "two cannot satisfy 매수 4");

    // 매수 1 is unaffected.
    assert.equal(pick([seat("A", 1)], 1).length, 1);
  },

  "seats without coordinates fall back to the old row order"() {
    const seats = [
      seat("b", 1, "R석", "2열", "1"),
      seat("a", 1, "R석", "1열", "1"),
    ];
    // No posLeft/posTop anywhere: every strategy must degrade to row/seat order
    // rather than sorting on undefined.
    for (const strategy of picker.SEAT_STRATEGIES.filter((s) => s !== "random")) {
      assert.deepEqual(
        picker.rankCandidates(seats, [], [], { strategy }).map((s) => s.seatInfoId),
        ["a", "b"],
        `${strategy} must fall back cleanly`,
      );
    }
  },

  "positioned seats lead, positionless ones trail"() {
    const mixed = [
      seat("noPos", 1, "R석", "1열", "1"),
      { seatInfoId: "hasPos", seatGrade: "1", seatGradeName: "R석", rowNo: "9열",
        seatNo: "9", blockKey: "b", seatGroupId: null, posLeft: 5, posTop: 5 },
    ];
    const ranked = picker.rankCandidates(mixed, [], [], { strategy: "left" });
    assert.deepEqual(ranked.map((s) => s.seatInfoId), ["hasPos", "noPos"]);
  },

  // A NaN sort key poisons every comparison it touches, and undefined is
  // indistinguishable from a real 0 coordinate.
  "coordinates are carried through as numbers or null, never NaN"() {
    const raw = {
      seatInfoId: "26005128:1:001:1", seatGrade: "1", seatGradeName: "VIP석",
      rowNo: "1열", seatNo: "1", posLeft: 976.002, posTop: 1168.039, floor: "1층",
    };
    const made = picker.toCandidate(raw, "001:001");
    assert.equal(made.posLeft, 976.002);
    assert.equal(made.posTop, 1168.039);
    assert.equal(made.floor, "1층");

    for (const bad of [undefined, null, "", "abc", NaN]) {
      assert.equal(picker.numOrNull(bad), null, `${String(bad)} must become null`);
    }
    assert.equal(picker.numOrNull(0), 0, "a real zero coordinate must survive");
  },

  // Measured on a live seat map with the countdown still running: __NEXT_DATA__
  // carried no initData and sessionStorage had no "interpark/context", so the
  // run aborted before doing anything and the panel showed nothing at all.
  "a booking session is recognised by its shape, not one storage key"() {
    const ok = picker.looksLikeBookingContext;
    assert.equal(ok({ sessionId: "26008861_M000…", goods: { goodsCode: "26008861" } }), true);
    // Both halves are required — a sessionId alone is some other feature's.
    assert.equal(ok({ sessionId: "abc" }), false);
    assert.equal(ok({ goods: { goodsCode: "1" } }), false);
    assert.equal(ok(null), false);
    assert.equal(ok("a string containing sessionId"), false);
  },

  // Captured from a live run. The gateway throttles by account and answers
  // FORBIDDEN with a countdown; preselect then fails, and the select reports
  // 좌석 요청이 잘못 되었습니다 because the seat was never actually held. Missing
  // this made a rate-limit look like a seat-picking bug, and every retry made
  // the block longer.
  "a gateway abuse block is recognised and carries its countdown"() {
    const real = [
      {
        message: "Your request could not be processed. Please try again later.",
        locations: [],
        extensions: {
          errorCode: "GATEWAY_ABUSE_BLOCKED",
          abuseStage: "BLOCKED",
          retryAfterMs: 165470,
          classification: "FORBIDDEN",
        },
      },
    ];
    assert.equal(picker.readGatewayBlock(real), 165470);

    // An ordinary seat refusal must not be mistaken for a block.
    assert.equal(
      picker.readGatewayBlock([
        { message: "좌석 요청이 잘못 되었습니다.", extensions: { backendErrorCode: "P40021" } },
      ]),
      -1,
    );
    assert.equal(picker.readGatewayBlock([{ message: "nope" }]), -1);
    assert.equal(picker.readGatewayBlock(null), -1);

    // Blocked with no countdown given is still blocked — and used to return 0,
    // which sets blockedUntil to a moment already past and lets every loop
    // carry straight on. A block of unknown length gets the observed one.
    assert.equal(
      picker.readGatewayBlock([{ extensions: { abuseStage: "BLOCKED" } }]),
      picker.BLOCK_FALLBACK_MS,
    );

    // --- the shapes that used to be invisible ----------------------------
    //
    // Only /onestop/gql carries a GraphQL envelope. seatMeta, seatStatus,
    // block-data, grades and the queue API do not, so a block on any of them
    // was read as a plain HTTP error and the loop kept asking. seatStatus is
    // ~4 requests a second for as long as a watch runs.

    // REST: the same fields, at the top level rather than under extensions.
    assert.equal(
      picker.readGatewayBlock({ errorCode: "GATEWAY_ABUSE_BLOCKED", retryAfterMs: 165470 }),
      165470,
    );

    // The queue API's whole vocabulary is one string. BL is 비정상 예매 차단.
    assert.equal(picker.readGatewayBlock("BL"), picker.BLOCK_FALLBACK_MS);
    assert.equal(picker.readGatewayBlock("N"), -1, "no queue is not a block");
    assert.equal(picker.readGatewayBlock("NP"), -1, "presale auth is not a block");

    // Status alone, when the body says nothing we can read.
    const headers = (value) => ({ get: (name) => (name === "Retry-After" ? value : null) });
    assert.equal(picker.readGatewayBlock(null, { status: 429, headers: headers("90") }), 90000);
    assert.equal(picker.readGatewayBlock(null, { status: 403, headers: headers(null) }),
                 picker.BLOCK_FALLBACK_MS);
    assert.equal(picker.readGatewayBlock(null, { status: 500, headers: headers(null) }), -1,
                 "a server error is not a block");
    assert.equal(picker.readGatewayBlock(null, { status: 401, headers: headers(null) }), -1,
                 "logged out is not a block");
  },

  "a block stops the watch on the next tick, not on the next 감시 시작"() {
    // The check happened once, before the first tick. A block arriving mid-watch
    // was invisible until the run was restarted by hand, so 취켓팅 polled through
    // the whole lockout — and the code's own note says retrying through one can
    // only extend it. Structural: the loop needs a live page to execute.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const loop = source.slice(source.indexOf("while (seatState.attempts < maxAttempts"));
    const check = loop.search(/gatewayBlockRemainingMs\(\)/);
    assert.ok(check >= 0, "the loop must ask whether it is blocked");

    // And ask before it spends anything.
    for (const [label, pattern] of [
      ["polling for freed seats", /pollFreedSeats\(/],
      ["clicking a seat", /selectSeatUnit\(/],
    ]) {
      assert.ok(loop.search(pattern) > check,
                `${label} must not happen while blocked`);
    }
  },

  "an arm will not fire while a block is running"() {
    // The queue path neither set nor checked a block, so a lockout earned by
    // the seat path let an arm fire straight into it — and every attempt can
    // push the block past the open, which is the one moment it cannot be
    // recovered from.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const fn = source.slice(source.indexOf("async function runArmScheduler("));
    const body = fn.slice(0, fn.indexOf("\n  }\n"));
    const check = body.search(/gatewayBlockRemainingMs\(\)/);
    assert.ok(check >= 0, "the scheduler must ask whether it is blocked");
    assert.ok(body.search(/syncServerClock\(/) > check,
              "and ask before it starts syncing toward a fire it cannot make");
  },

  "a block from any endpoint stops everything and says which one"() {
    // The question this session could not answer from the repo: which call was
    // blocked, the queue or the seat path. It is recorded now.
    const { race } = sandbox.window.PureClick;
    const state = race.state;
    const before = { until: state.blockedUntil, endpoint: state.blockedEndpoint };
    try {
      state.blockedUntil = 0;
      state.blockedEndpoint = "";
      const error = race.noteGatewayBlock(165470, "/onestop/api/seatStatus");
      assert.ok(state.blockedUntil > Date.now() + 160000, "the cooldown is recorded");
      assert.equal(state.blockedEndpoint, "/onestop/api/seatStatus");
      assert.equal(error.gatewayBlockedMs, 165470);
      assert.equal(error.blockedEndpoint, "/onestop/api/seatStatus");

      // A shorter block must not shorten a longer one already running.
      const far = state.blockedUntil;
      race.noteGatewayBlock(1000, race.WAITING_ENDPOINT);
      assert.equal(state.blockedUntil, far, "the longest block wins");
      assert.equal(state.blockedEndpoint, "/onestop/api/seatStatus", "and keeps its cause");
    } finally {
      state.blockedUntil = before.until;
      state.blockedEndpoint = before.endpoint;
    }
  },

  // Measured on a live seat map: the circles carry onPointerDown/onPointerUp and
  // no onClick at all. A MouseEvent('click') reached nothing, so the page never
  // registered the seat, 선택 좌석 stayed empty and the step could not advance.
  "selecting a seat fires pointer events, not just a click"() {
    const fired = [];
    const node = {
      getBoundingClientRect: () => ({ left: 100, top: 200, width: 10, height: 10 }),
      dispatchEvent(event) {
        fired.push({ type: event.type, ctor: event.constructor.name, x: event.clientX, y: event.clientY });
        return true;
      },
    };
    sandbox.window.PureClick.picker.firePointerSelect(node);

    const types = fired.map((e) => e.type);
    assert.deepEqual(types, ["pointerdown", "pointerup"]);
    // No extra click. The seat carries onPointerDown/onPointerUp and an
    // ancestor carries onClick, so sending both delivered two actions from one
    // intended press — which on a toggle map undoes or conflicts with the
    // selection, and is why the error appeared only when the macro was used.
    assert.ok(!types.includes("click"), "one press must not become two actions");
    assert.equal(fired.find((e) => e.type === "pointerdown").ctor, "PointerEvent");
    // Aimed at the seat's centre, not 0,0 — the map hit-tests by coordinate.
    assert.deepEqual(
      { x: fired[0].x, y: fired[0].y },
      { x: 105, y: 205 },
    );
  },

  // A live seat map full of free seats must never leave the watcher parked.
  // The cap on live attempts exists so we stop hammering seats the map still
  // shows as free but the server has sold; it has to lift the moment the free
  // set changes, or 취켓팅 sits at the cap ignoring a map with 126 seats on it.
  "the live-seat cap lifts as soon as the free set changes"() {
    const s = (id) => ({ seatInfoId: id });
    const before = picker.liveSignature([s("a"), s("b"), s("c")]);

    assert.equal(picker.liveSignature([s("a"), s("b"), s("c")]), before,
      "an unchanged map must keep the same signature, so the cap still applies");
    assert.notEqual(picker.liveSignature([s("a"), s("b")]), before, "count changed");
    assert.notEqual(picker.liveSignature([s("z"), s("b"), s("c")]), before, "first seat changed");
    assert.notEqual(picker.liveSignature([s("a"), s("b"), s("z")]), before, "last seat changed");
    assert.equal(picker.liveSignature([]), "0");
  },

  "the watcher says which of the three reasons it is idle for"() {
    const live = [{ seatInfoId: "a" }, { seatInfoId: "b" }];
    const text = (l, free, exhausted) => picker.catchStatusText(l, free, 400, exhausted);

    assert.match(text([], 0, false), /빈 좌석 0석/);
    // Seats exist but none match the chosen grades — the case that looked
    // identical to "no seats at all" before.
    assert.match(text([], 126, false), /내 조건에 맞는 등급이 없음/);
    assert.match(text(live, 126, true), /남이 먼저 가져감/);
    assert.match(text(live, 126, true), /자동으로 다시 시도/);
    assert.match(text(live, 126, false), /후보 2석/);
    // The old text reported a block cursor that is always 0 on a 2-block venue.
    for (const variant of [text([], 0, false), text([], 126, false), text(live, 126, true)]) {
      assert.doesNotMatch(variant, /구역 0\//);
    }
  },

  // A seat lost to someone faster comes back as HTTP 400 P40021 with the
  // rejected ids nested under data.data. Missing them means the caller retries
  // the same dead seats until it runs out of attempts.
  "the rejected-seat list is found wherever the API nests it"() {
    const four_hundred = {
      data: {
        code: 400,
        backendErrorCode: "P40021",
        message: "좌석 요청이 잘못 되었습니다.",
        data: {
          unselectableSeatInfoIds: ["26008861:26000679:046:107", "26008861:26000679:046:108"],
        },
      },
    };
    assert.deepEqual(picker.readUnselectable(four_hundred), [
      "26008861:26000679:046:107",
      "26008861:26000679:046:108",
    ]);

    // Top level, as a 200 reports it.
    assert.deepEqual(
      picker.readUnselectable({ unselectableSeatInfoIds: ["a"] }),
      ["a"],
    );
    // A clean success must not look like a rejection.
    assert.deepEqual(picker.readUnselectable({ data: { seats: [] } }), []);
    assert.deepEqual(picker.readUnselectable(null), []);
  },

  // The popup shim is what makes 예매하기 reach the seat map at all: the embedded
  // WebView refuses to create the BookingPop window, so every popup has to be
  // folded back into this one. These mirror NOL's openPCOnestop() exactly.
  "window.open with a url navigates this window instead of a popup"() {
    navigations.length = 0;
    const win = sandbox.window.open(
      "https://tickets.interpark.com/onestop/seat",
      "BookingPop",
      "width=900,height=682",
    );
    assert.deepEqual(navigations, [{ assign: "https://tickets.interpark.com/onestop/seat" }]);
    assert.equal(typeof win.focus, "function", "callers do win.focus() and must not crash");
  },

  "a form posted at a popup name is retargeted to this window"() {
    navigations.length = 0;
    // openPCOnestop: window.open('', 'BookingPop'); form.target='BookingPop'; form.submit()
    sandbox.window.open("", "BookingPop", "width=900,height=682");
    const form = new HTMLFormElement();
    form.target = "BookingPop";
    form.action = "https://tickets.interpark.com/onestop/gates";
    form.method = "post";
    form.submit();
    assert.equal(form.target, "_self", "the POST must land in this window");
    assert.ok(form.submitted, "the native submit still has to run");
    assert.deepEqual(navigations.at(-1), {
      post: "https://tickets.interpark.com/onestop/gates",
      target: "_self",
    });
  },

  "the queue path survives window.self.close()"() {
    navigations.length = 0;
    // openPCOnestop: window.self.close(); win.location.replace(waitingUrl)
    const win = sandbox.window.open("", "BookingPop", "width=900");
    sandbox.window.self.close();
    win.location.replace("https://tickets.interpark.com/gates/waiting/26011315");
    assert.deepEqual(
      navigations,
      [{ replace: "https://tickets.interpark.com/gates/waiting/26011315" }],
      "close() must be a no-op and the queue url must open here",
    );
  },
  "a block on a picture-only venue maps onto the drawing": () => {
    // 26011315 (Maroon 5): 43 blocks and 28,932 seats in the API, zero seat
    // circles in the DOM, because the first screen is a bitmap of the venue.
    // The block has to be opened by clicking it, and block-data gives boxes
    // without saying what space they are in — they reach x=1475 while the
    // overlay viewBox is 1214 wide, so they are not overlay coordinates.
    //
    // The mapping used lays each block's position *as a fraction of all the
    // blocks' extent* onto the drawing, which does not care about units. These
    // are the venue's real measured numbers.
    const { race } = sandbox.window.PureClick;
    const { blockClickPoint } = race;
    const IMAGE = { x: 12, y: 126, width: 877, height: 830 };

    // Real extent from block-data on that show.
    race.state.discoveredBlocks = [
      { blockKey: "001:001", absoluteLeft: 393, absoluteTop: 80, absoluteRight: 519, absoluteBottom: 161 },
      { blockKey: "corner", absoluteLeft: 116, absoluteTop: 80, absoluteRight: 200, absoluteBottom: 160 },
      { blockKey: "far", absoluteLeft: 1400, absoluteTop: 1000, absoluteRight: 1475, absoluteBottom: 1109 },
    ];

    const originalQsa = sandbox.document.querySelectorAll;
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel) === "img"
          ? [{ getBoundingClientRect: () => IMAGE }]
          : [];

      const point = blockClickPoint(race.state.discoveredBlocks[0], "extent-to-image");
      assert.ok(point, "a block with a box must produce a click point");
      // It must land on the drawing, which is the whole test: a mapping that
      // sends clicks off the image can never open anything.
      assert.ok(
        point.clientX >= IMAGE.x && point.clientX <= IMAGE.x + IMAGE.width,
        `x ${point.clientX} must be on the venue image`,
      );
      assert.ok(
        point.clientY >= IMAGE.y && point.clientY <= IMAGE.y + IMAGE.height,
        `y ${point.clientY} must be on the venue image`,
      );

      // Corners must stay corners, or blocks would be entered at random.
      const near = blockClickPoint(race.state.discoveredBlocks[1], "extent-to-image");
      const far = blockClickPoint(race.state.discoveredBlocks[2], "extent-to-image");
      assert.ok(near.clientX < far.clientX, "left block stays left of the right one");
      assert.ok(near.clientY < far.clientY, "top block stays above the bottom one");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      race.state.discoveredBlocks = null;
    }
  },

  "the venue-to-screen mapping is measured from seats on screen": () => {
    // A seat off screen has no DOM node, so aiming at it needs venue -> screen.
    // Deriving that from the SVG viewBox would tie us to markup we do not
    // control; seats already drawn carry both coordinate systems at once, so
    // the mapping can simply be measured — and re-measured after every move.
    const { calibrateVenueToScreen } = sandbox.window.PureClick.race;

    // A venue laid out at 3.5x with a (120, 40) offset on screen.
    const SCALE = 3.5;
    const OX = 120;
    const OY = 40;
    const nodes = [];
    for (let col = 0; col < 6; col += 1) {
      for (let row = 0; row < 4; row += 1) {
        const vx = 10 + col * 5;
        const vy = 20 + row * 5;
        nodes.push({
          __seat: { seatInfoId: `s${col}-${row}`, posLeft: vx, posTop: vy },
          getAttribute: () => "3",
          getBoundingClientRect: () => ({
            left: OX + vx * SCALE - 3,
            top: OY + vy * SCALE - 3,
            width: 6,
            height: 6,
          }),
        });
      }
    }

    const originalQsa = sandbox.document.querySelectorAll;
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? nodes : [];
      const calibration = calibrateVenueToScreen((node) => node.__seat);
      assert.ok(calibration, "seats on screen are enough to calibrate");
      assert.ok(
        Math.abs(calibration.scale - SCALE) < 0.05,
        `scale ${calibration.scale} should recover ${SCALE}`,
      );
      // And it must place an off-screen seat correctly, which is the point.
      const far = calibration.toScreen(200, 300);
      assert.ok(Math.abs(far.x - (OX + 200 * SCALE)) < 2, "x placement");
      assert.ok(Math.abs(far.y - (OY + 300 * SCALE)) < 2, "y placement");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
    }
  },

  "aiming does nothing for a seat already on screen": async () => {
    // The view must not move when it does not need to; that jumpiness is what
    // made the previous zoom unusable.
    const { ensureSeatRendered } = sandbox.window.PureClick.race;
    const originalQsa = sandbox.document.querySelectorAll;
    const node = {
      __seat: { seatInfoId: "here", posLeft: 10, posTop: 10 },
      getAttribute: () => "3",
      getBoundingClientRect: () => ({ left: 100, top: 100, width: 6, height: 6 }),
    };
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? [node] : [];
      const result = await ensureSeatRendered(
        "here",
        { posLeft: 10, posTop: 10 },
        { readSeat: (n) => n.__seat },
      );
      assert.equal(result.via, "already", "an on-screen seat must not move the view");
      assert.equal(result.ok, true);
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
    }
  },

  "the round on screen wins over the one the page loaded with": async () => {
    // The bug that made a selling show look sold out.
    //
    // initData is captured at page load and never refreshed, so changing 회차
    // in place leaves __NEXT_DATA__ holding the old round. Every round-keyed
    // call — block-data, seatMeta, seatStatus — then asks about a round nobody
    // is looking at. Measured live: initData said playSeq 017 and we polled
    // 017:001/017:002 while the drawn seats carried blockKey 022:001 with 40 of
    // them free. seatStatus answered an all-zero mask, so 취켓팅 reported
    // 빈 좌석 0석 forever with seats plainly available on screen.
    //
    // Block keys are `${playSeq}:${block}`, so the seats say which round is up,
    // and unlike initData they cannot be stale.
    const { race } = sandbox.window.PureClick;
    // The real shape, measured on a live venue: blockKey sits BESIDE `seat`,
    // not inside it, so returning props.seat alone loses the block entirely.
    const seat = (blockKey, id) => ({
      __reactProps$test: { seat: { seatInfoId: id, seatGrade: "2" }, blockKey },
      getAttribute: () => "3",
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 6, height: 6 }),
    });
    const nodes = [seat("022:001", "a"), seat("022:001", "b"), seat("022:001", "c")];

    const originalQsa = sandbox.document.querySelectorAll;
    const originalNext = sandbox.window.__NEXT_DATA__;
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? nodes : [];

      assert.equal(race.currentPlaySeqFromDom(), "022", "the drawn 구역 names the round");

      const corrected = race.withLivePlaySeq({
        goods: { goodsCode: "26011611", placeCode: "19001312" },
        playSeq: { playSeq: "017" },
      });
      assert.equal(
        String(corrected.playSeq.playSeq),
        "022",
        "a stale initData round must be overridden by what is drawn",
      );

      // Agreement must not rewrite anything.
      const same = race.withLivePlaySeq({
        goods: { goodsCode: "26011611" },
        playSeq: { playSeq: "022" },
      });
      assert.equal(String(same.playSeq.playSeq), "022");

      // Nothing drawn is not evidence; leave initData alone.
      sandbox.document.querySelectorAll = () => [];
      const untouched = race.withLivePlaySeq({ goods: {}, playSeq: { playSeq: "017" } });
      assert.equal(String(untouched.playSeq.playSeq), "017", "no seats means no correction");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      sandbox.window.__NEXT_DATA__ = originalNext;
    }
  },

  "the poll itself notices a 일정 change": async () => {
    // The fix that was correct and unreachable.
    //
    // The reset on a round change worked, but nothing ever triggered it while
    // idle: ensureSeatCatalog returns early once a catalog exists, and the
    // invalidation lived only on run paths. So changing the date while simply
    // browsing was never detected and the panel went on describing the old
    // round. readShowCatalog is polled continuously, which makes it the one
    // place that can notice.
    const api = sandbox.window.PureClick;
    const state = api.race.state;

    const seat = (blockKey, id) => {
      const node = {
        __reactProps$test: { seat: { seatInfoId: id, seatGrade: "2" }, blockKey },
        getAttribute: () => "3",
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 6, height: 6 }),
      };
      return node;
    };
    const nodes = [seat("022:001", "a"), seat("022:001", "b"), seat("022:001", "c")];

    const originalQsa = sandbox.document.querySelectorAll;
    const originalNext = sandbox.window.__NEXT_DATA__;
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? nodes : [];
      sandbox.window.__NEXT_DATA__ = {
        props: { pageProps: { initData: { goods: { goodsCode: "SHOW" } } } },
      };

      // Holding the previous round's work.
      state.blocksKey = "SHOW:017";
      state.discoveredBlocks = [{ blockKey: "017:001" }];
      state.lastBlocks = [{ blockKey: "017:001", mask: [true] }];
      state.showCatalog = { sketch: [{ k: "017:001", x: 1, y: 1 }] };
      state.blockEntered = "017:001";

      api.readShowCatalog();

      assert.equal(state.blocksKey, "SHOW:022", "the poll must adopt the round on screen");
      assert.equal(state.discoveredBlocks, null, "and drop the previous round's blocks");
      assert.deepEqual(state.lastBlocks, [], "and its availability read");
      assert.equal(state.blockEntered, "", "and the 구역 it had open");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      sandbox.window.__NEXT_DATA__ = originalNext;
      state.blocksKey = "";
      state.discoveredBlocks = null;
      state.lastBlocks = [];
      state.showCatalog = null;
    }
  },

  "a new 회차 drops everything keyed to the old one": () => {
    // Block keys embed the round — the same venue is 017:001 on one round and
    // 022:001 on the next — so a block list, an availability read or an opened
    // 구역 from the previous round describes seats that no longer exist.
    // Measured live: the page drew round 022 with 40 selectable seats while the
    // macro polled round 017 and read 0 free, which is indistinguishable from a
    // sold-out show and is why nothing was ever caught.
    const { race } = sandbox.window.PureClick;
    const state = race.state;

    state.blocksKey = "";
    state.discoveredBlocks = [{ blockKey: "017:001" }];
    state.lastBlocks = [{ blockKey: "017:001", mask: [true] }];
    state.showCatalog = { sketch: [{ k: "017:001", x: 1, y: 1 }] };
    state.blockEntered = "017:001";

    // First sighting is not a change; there is nothing yet to invalidate.
    assert.equal(race.adoptBlocksKey("SHOW:017"), false, "adopting a first round is not a change");
    assert.ok(state.discoveredBlocks, "and must not throw the blocks away");

    // Same round again: still nothing to do.
    assert.equal(race.adoptBlocksKey("SHOW:017"), false, "an unchanged round is not a change");
    assert.ok(state.discoveredBlocks);

    // A different round invalidates everything keyed to the old one.
    assert.equal(race.adoptBlocksKey("SHOW:022"), true);
    assert.equal(state.discoveredBlocks, null, "the old round's blocks must go");
    assert.deepEqual(state.lastBlocks, [], "and its availability read");
    assert.equal(state.showCatalog, null, "and its catalog");
    assert.equal(state.blockEntered, "", "and the 구역 it had open");
    assert.equal(state.blocksKey, "SHOW:022");

    state.blocksKey = "";
    state.lastBlocks = [];
  },

  "the round on screen is read from a few seats, not all of them": () => {
    // This runs on every host snapshot and a venue can have tens of thousands
    // of circles; walking them four times a second to answer a question a dozen
    // seats already answer would make the poll the most expensive thing on the
    // page.
    const { race } = sandbox.window.PureClick;
    let reads = 0;
    const seat = (blockKey, id) => ({
      get __seat() {
        reads += 1;
        return { seatInfoId: id, blockKey };
      },
      getAttribute: () => "3",
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 6, height: 6 }),
    });
    const nodes = [];
    for (let i = 0; i < 500; i += 1) nodes.push(seat("022:001", `s${i}`));
    for (const node of nodes) node.__reactProps$test = { seat: { seatInfoId: "x", seatGrade: "2" }, blockKey: "022:001" };

    const originalQsa = sandbox.document.querySelectorAll;
    const originalNext = sandbox.window.__NEXT_DATA__;
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? nodes : [];
      sandbox.window.__NEXT_DATA__ = {
        props: { pageProps: { initData: { goods: { goodsCode: "SHOW" } } } },
      };
      assert.equal(race.sampledRoundKey(), "SHOW:022");

      // Too few circles drawn is not evidence of anything.
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("circle") ? nodes.slice(0, 2) : [];
      assert.equal(race.sampledRoundKey(), null, "a couple of stray seats name no round");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      sandbox.window.__NEXT_DATA__ = originalNext;
    }
  },

  "a drawn seat with no block of its own is looked up in seatMeta": () => {
    // Measured live: all 273 rendered seats came back with blockKey undefined,
    // which silently turned off every "which 구역 am I in" decision downstream.
    const { race } = sandbox.window.PureClick;
    race.state.lastBlocks = [
      { blockKey: "022:001", seats: [{ seatInfoId: "S1" }, { seatInfoId: "S2" }] },
      { blockKey: "022:002", seats: [{ seatInfoId: "S9" }] },
    ];
    try {
      assert.equal(race.blockKeyForSeatId("S2"), "022:001");
      assert.equal(race.blockKeyForSeatId("S9"), "022:002");
      assert.equal(race.blockKeyForSeatId("nope"), null, "an unknown seat has no block");

      // And currentOpenBlock must use it rather than giving up.
      const seat = (id) => ({
        __seat: { seatInfoId: id },
        getAttribute: () => "3",
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 6, height: 6 }),
      });
      const nodes = [seat("S1"), seat("S2"), seat("S9")];
      const originalQsa = sandbox.document.querySelectorAll;
      try {
        sandbox.document.querySelectorAll = (sel) =>
          String(sel).includes("circle") ? nodes : [];
        assert.equal(
          race.currentOpenBlock((n) => n.__seat),
          "022:001",
          "the 구역 is recoverable even when the circles do not name it",
        );
      } finally {
        sandbox.document.querySelectorAll = originalQsa;
      }
    } finally {
      race.state.lastBlocks = [];
    }
  },

  "starting a run retires the one already going": () => {
    // Generations were only bumped on script reload and by stopAll, so two
    // loops could drive the same seatState at once. Live, the auto-seat toggle
    // started a 좌석 잡기 on arriving at the seat map; pressing 감시 시작 added a
    // second loop beside it, and the first one hitting its 80-attempt cap set
    // running = false under the watch. It looked exactly like a stall.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const start = source.indexOf("async function runSeatAutopilot(");
    const head = source.slice(start, start + 1200);
    assert.match(
      head,
      /const runGen = \(window\.__pureclickRunGen = \(window\.__pureclickRunGen \|\| 0\) \+ 1\)/,
      "a starting run must claim a new generation so older loops retire",
    );
    // And the loop must actually honour it.
    const loop = source.slice(source.indexOf("while (seatState.attempts < maxAttempts"));
    assert.match(loop.slice(0, 400), /runWasSuperseded\(runGen\)/);
  },

  "a sold-out show stops 좌석 잡기 instead of grinding out the cap": () => {
    // 좌석 잡기 on a full venue can never find anything, but it spent all 80
    // attempts discovering that and then reported 선점 실패 (80회) — which reads
    // as a broken macro rather than a full house. 취켓팅 is the thing that waits.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const branch = source.slice(source.indexOf("if (!candidates.length) {", source.indexOf("while (seatState.attempts < maxAttempts")));
    const head = branch.slice(0, 900);
    assert.match(head, /!isCatch && \(seatState\.lastBlocks \|\| \[\]\)\.length && freeSeatCount\(\) === 0/,
      "a full venue must be detected before burning attempts");
    assert.match(head, /soldOut/, "and recorded as such");
    assert.match(head, /감시 시작/, "and point the user at 취켓팅");
  },

  "the open 구역 is measured from the map, not remembered": () => {
    // seatState.blockEntered was only ever set when *we* opened a block, and
    // the user normally opens it themselves — so "am I in the right 구역" read
    // false forever and the switch never fired. That is the ordinary case:
    // sitting in one block while a seat frees in another, unreachable. Every
    // rendered seat carries props.blockKey, so the answer is already on screen.
    const { currentOpenBlock } = sandbox.window.PureClick.race;
    const seat = (blockKey, id) => ({
      __seat: { seatInfoId: id, blockKey },
      getAttribute: () => "3",
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 6, height: 6 }),
    });
    const read = (node) => node.__seat;

    const originalQsa = sandbox.document.querySelectorAll;
    // Stable nodes: collectSeatCircles runs four selectors and de-duplicates by
    // identity, so a stub that builds fresh objects per call would turn one
    // seat into four.
    const serve = (nodes) => (sel) => (String(sel).includes("circle") ? nodes : []);
    try {
      // A block open: the majority key wins, and a few stragglers from a
      // neighbouring block do not confuse it.
      sandbox.document.querySelectorAll = serve([
        seat("001:017", "a"), seat("001:017", "b"), seat("001:017", "c"),
        seat("001:017", "d"), seat("001:002", "e"),
      ]);
      assert.equal(currentOpenBlock(read), "001:017");

      // A stadium's picture-only screen: nothing drawn, so nothing to claim.
      sandbox.document.querySelectorAll = serve([]);
      assert.equal(currentOpenBlock(read), null, "no seats drawn means no 구역 open");

      // One stray circle is not a block.
      sandbox.document.querySelectorAll = serve([seat("001:001", "z")]);
      assert.equal(currentOpenBlock(read), null, "a stray seat must not count as a 구역");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
    }
  },

  "the hunt loop clears modals on every pass": () => {
    // The whole loop used to run behind a dialog: dismissBlockingDialogs was
    // only reachable from a navigation helper, and the 취켓팅 wait — where a
    // sold-out show spends all its time — returned before anything could clear
    // one. Structural, because the loop needs a live page to execute.
    // Asserted by order, not by a byte offset. This used to slice the first
    // 1400 characters of the loop, so adding a comment above the check failed a
    // working change — the same trap test_panel_entry_test.py fell into with a
    // 600-byte slice.
    const source = readFileSync(resolve(here, "../browser/pureclick_autopilot.js"), "utf8");
    const loop = source.slice(source.indexOf("while (seatState.attempts < maxAttempts"));
    const clear = loop.search(/blockingOverlayNodes\(\)\.length\s*\)\s*dismissBlockingDialogs\(\)/);
    assert.ok(clear >= 0, "the loop must clear a blocking modal at all");

    // Everything that touches the page or the network has to come after it.
    for (const [label, pattern] of [
      ["reading the cart", /selectedSeatCount\(\)/],
      ["polling for freed seats", /pollFreedSeats\(/],
      ["clicking a seat", /selectSeatUnit\(/],
    ]) {
      const at = loop.search(pattern);
      assert.ok(at > clear, `${label} must not happen before the modal is cleared`);
    }
  },

  "a modal nobody named still gets cleared off the map": async () => {
    // Measured live: every computed block click landed on
    // DIV.nds-e-dialog__overlay, a 1320x956 full-screen backdrop. The clicks
    // never reached the venue at all. The phrase-matched detectors could not
    // see it because its text is not one of the phrases we know — but an
    // unnamed modal blocks the map exactly as completely as a known one, so
    // the test here is structural: a dialog that owns a dismiss button.
    const { race } = sandbox.window.PureClick;
    let pressed = null;

    const overlay = {
      innerText: "점검 안내\n잠시 후 다시 시도해 주세요.",
      getAttribute: (name) => (name === "class" ? "nds-e-dialog__overlay" : ""),
      getBoundingClientRect: () => ({ width: 1320, height: 956 }),
      querySelectorAll: () => [{ textContent: "확인", click() { pressed = "확인"; } }],
    };
    // A bigger, wordier container that also matches the class filter: the
    // smallest owner must win, so we never press 예매 or 결제 on a modal page.
    const wrapper = {
      innerText: "x".repeat(400),
      getAttribute: (name) => (name === "class" ? "pageModalRoot" : ""),
      getBoundingClientRect: () => ({ width: 1320, height: 956 }),
      querySelectorAll: () => [{ textContent: "예매하기", click() { pressed = "예매하기"; } }],
    };

    const originalQsa = sandbox.document.querySelectorAll;
    race.state.unknownDialog = "";
    try {
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("dialog") ? [wrapper, overlay] : [];

      const seen = race.describeBlockingOverlay();
      assert.ok(seen, "a full-screen modal must be visible to the macro");

      assert.equal(race.dismissAnyBlockingOverlay(), true, "and must be dismissed");
      assert.equal(pressed, "확인", "press the dismiss button, never 예매하기");
      assert.ok(
        race.state.unknownDialog.includes("점검"),
        "its text is recorded so an unknown modal is diagnosable",
      );
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      race.state.unknownDialog = "";
    }
  },

  "losing at 선택 완료 keeps hunting instead of ending the run": async () => {
    // Observed live: the seat was clicked, 선택 완료 was pressed, another buyer
    // confirmed first, and NOL answered 이미 선점된 좌석입니다. The run reported
    // 중단됨 and told the user to close the booking window and start over —
    // throwing away a working session for something that only needed the next
    // seat. A lost race must be a conflict, never a broken session.
    const { race } = sandbox.window.PureClick;
    const confirmButton = { textContent: "확인", click() {} };
    const modal = {
      innerText: "이미 선점된 좌석입니다.",
      getBoundingClientRect: () => ({ width: 310, height: 130 }),
      querySelectorAll: () => [confirmButton],
    };

    const originalQsa = sandbox.document.querySelectorAll;
    race.state.takenUntil.clear();
    race.state.heldSeatIds.clear();
    race.state.lastError = "";
    try {
      // Role-less on purpose: NOL's box has no role, which is why a
      // [role=dialog]-only search never saw it.
      sandbox.document.querySelectorAll = (sel) =>
        String(sel).includes("div") ? [modal] : [];
      const result = await race.recoverFailedConfirm("[R석] 13열 2", "net");

      assert.equal(result.takenConflict, true, "a lost race is a conflict");
      assert.ok(!result.confirmFailed, "and must not be reported as a session failure");
      assert.equal(
        race.state.lastError,
        "",
        "no lastError: setting it is what stops the run",
      );
      assert.equal(race.state.locked, false, "the seat is not ours, so release the lock");
    } finally {
      sandbox.document.querySelectorAll = originalQsa;
      race.state.takenUntil.clear();
    }
  },

  "이미 선점된 좌석입니다 is a lost race, not a seat error": () => {
    // The two mean opposite things. The errors already matched mean "something
    // went wrong, back off"; this means "that one seat is gone, take the next
    // one now". It matched no pattern at all, so the select wait ran to its 6s
    // timeout while the modal — which is modal — blocked every later click.
    const { SEAT_TAKEN_DIALOG, SEAT_ERROR_DIALOG } = sandbox.window.PureClick.race;
    const taken = "이미 선점된 좌석입니다";

    assert.ok(SEAT_TAKEN_DIALOG.test(taken), "the lost-race modal must be recognised");
    assert.ok(!SEAT_ERROR_DIALOG.test(taken), "and must not read as a generic fault");

    // The existing errors keep their meaning.
    for (const copy of [
      "좌석 선택 도중 오류가 발생했습니다",
      "좌석 요청이 잘못되었습니다",
      "선택 가능한 매수를 초과하였습니다",
    ]) {
      assert.ok(SEAT_ERROR_DIALOG.test(copy), `${copy} is still an error`);
      assert.ok(!SEAT_TAKEN_DIALOG.test(copy), `${copy} is not a lost race`);
    }
  },

  "only a real alert box counts, not a page that mentions it": () => {
    // The first version of this detector clicked 확인 inside anything whose text
    // contained the phrase, which on a seat map is most of the document once an
    // alert has appeared. The size guard is why that stopped.
    const { seatTakenDialogVisible } = sandbox.window.PureClick.race;
    const confirmButton = { textContent: "확인" };
    const dialog = (text, { width = 300, height = 120, buttons = [confirmButton] } = {}) => ({
      innerText: text,
      getBoundingClientRect: () => ({ width, height }),
      querySelectorAll: () => buttons,
    });

    const original = sandbox.document.querySelectorAll;
    // Selector-aware: the real modal carries no role, so a search limited to
    // [role=dialog] must find nothing. Ignoring the selector here made this
    // test pass against the very code that missed the live modal.
    const serve = (nodes) => (sel) => (String(sel).includes("div") ? nodes : []);
    try {
      sandbox.document.querySelectorAll = serve([dialog("이미 선점된 좌석입니다")]);
      assert.equal(seatTakenDialogVisible(), true, "a role-less modal with 확인 is the alert");

      sandbox.document.querySelectorAll = serve([
        dialog("이미 선점된 좌석입니다" + "x".repeat(400)),
      ]);
      assert.equal(seatTakenDialogVisible(), false, "a whole page merely containing it is not");

      sandbox.document.querySelectorAll = serve([
        dialog("이미 선점된 좌석입니다", { width: 10, height: 5 }),
      ]);
      assert.equal(seatTakenDialogVisible(), false, "an invisible node is not an alert");

      // The search is no longer limited to [role=dialog] — NOL's box carries no
      // role — so owning a 확인 button is what keeps it from matching prose.
      sandbox.document.querySelectorAll = serve([
        dialog("이미 선점된 좌석입니다", { buttons: [] }),
      ]);
      assert.equal(
        seatTakenDialogVisible(),
        false,
        "text without a dismiss button is not an alert",
      );
    } finally {
      sandbox.document.querySelectorAll = original;
    }
  },

  "a seat lost to someone else cools down rather than being blacklisted": () => {
    // Holds expire and carts are abandoned, so a seat taken from under us can
    // genuinely come back — but re-offering it immediately just races the same
    // person again for a seat they are actively holding.
    const { markSeatTaken, seatInCooldown, sweepTakenCooldowns, state, TAKEN_COOLDOWN_MS } =
      sandbox.window.PureClick.race;
    state.takenUntil.clear();

    markSeatTaken("seat-1");
    assert.equal(seatInCooldown("seat-1"), true, "just lost: keep out of the pool");
    assert.equal(seatInCooldown("seat-2"), false, "other seats are unaffected");
    assert.ok(TAKEN_COOLDOWN_MS >= 10000, "a cooldown shorter than a hold is pointless");

    // Expired: it must come back, not stay blacklisted for the run.
    state.takenUntil.set("seat-1", Date.now() - 1);
    assert.equal(seatInCooldown("seat-1"), false, "an expired hold rejoins the pool");

    // 취켓팅 runs unbounded, so the map must not grow for the whole sitting.
    state.takenUntil.set("old", Date.now() - 1);
    state.takenUntil.set("live", Date.now() + TAKEN_COOLDOWN_MS);
    sweepTakenCooldowns();
    assert.equal(state.takenUntil.has("old"), false, "expired entries are swept");
    assert.equal(state.takenUntil.has("live"), true, "live ones are kept");
    state.takenUntil.clear();
  },

  "an unrecognised modal is recorded instead of silently blocking": () => {
    // Both of this session's blocking bugs were invisible from outside the
    // browser. An unmatched modal stops the map just as hard as a known one.
    const { unknownBlockingDialogText } = sandbox.window.PureClick.race;
    const dialog = (text) => ({
      innerText: text,
      getBoundingClientRect: () => ({ width: 300, height: 120 }),
      querySelectorAll: () => [],
    });
    const original = sandbox.document.querySelectorAll;
    try {
      sandbox.document.querySelectorAll = () => [dialog("점검 중입니다")];
      assert.equal(unknownBlockingDialogText(), "점검 중입니다");

      // Something we already understand is not "unknown".
      sandbox.document.querySelectorAll = () => [dialog("이미 선점된 좌석입니다")];
      assert.equal(unknownBlockingDialogText(), null, "a known modal is not reported as unknown");
    } finally {
      sandbox.document.querySelectorAll = original;
    }
  },

  "furniture outside the room is dropped, unsellable seats inside it are kept": () => {
    // Having no takeable seat does not by itself make a block furniture.
    // 26012217 has three: 306 and 307 are the small L/R groups NOL draws at the
    // sides of the 3rd floor, and 308 is 100 seats with no floor, no row label
    // and no grade parked at x 223..250 while every real seat sits inside
    // x 33..203. Dropping all three erased two groups the venue displays and
    // reported 1913 seats for a 1935-seat room; keeping all three stretched the
    // frame around empty space. Only the one outside the room goes.
    const pick = sandbox.window.PureClick.seatingBlocks;
    assert.equal(typeof pick, "function", "seatingBlocks must be exported");

    const seat = (x, y, exposable) => ({ posLeft: x, posTop: y, isExposable: exposable });
    const kept = pick([
      { blockKey: "house", seats: [seat(40, 80, true), seat(200, 250, true)] },
      // Unsellable, but standing inside the room.
      { blockKey: "sideL", seats: [seat(45, 240, false), seat(50, 240, false)] },
      // Unsellable and parked well outside it.
      { blockKey: "furniture", seats: [seat(230, 120, false), seat(245, 130, false)] },
    ]).map((block) => block.blockKey);

    assert.ok(kept.includes("house"), "the house is always drawn");
    assert.ok(kept.includes("sideL"), "an unsellable group inside the room is still part of it");
    assert.ok(!kept.includes("furniture"), "a block outside the room must not stretch the frame");
  },

  "a sold-out round still draws its venue": () => {
    // With nothing takeable anywhere there is no room to measure against, and
    // erasing the map would leave nothing to drag over.
    const pick = sandbox.window.PureClick.seatingBlocks;
    const blocks = [
      { blockKey: "a", seats: [{ posLeft: 10, posTop: 10, isExposable: false }] },
      { blockKey: "b", seats: [{ posLeft: 20, posTop: 10, isExposable: false }] },
    ];
    assert.equal(pick(blocks).length, 2, "draw the venue rather than nothing");
  },

  "a second show never inherits the first show's seat map": () => {
    // The parked sketch is an expensive thing to rebuild, so it survives
    // navigation. It used to survive a change of *show* too: the cache only
    // overwrote on a non-empty list and never cleared, so opening show B found
    // an empty catalog, had show A's sketch injected into it, and the enrich
    // step then saw a non-empty sketch and skipped fetching B entirely. The
    // picker drew A's venue while claiming to be B.
    const cache = sandbox.window.PureClick.sketchCache;
    assert.ok(cache, "sketch cache must be exported");

    const asShow = (code) => {
      sandbox.window.__NEXT_DATA__ = {
        props: { pageProps: { initData: { goods: { goodsCode: code } } } },
      };
    };
    const sketchA = [{ k: "a", x: 1, y: 1 }];

    asShow("AAA111");
    cache.parkSketch(sketchA);
    assert.equal(cache.currentSketchKey(), "AAA111");
    assert.deepEqual(cache.parkedSketchFor("AAA111"), sketchA, "same show may reuse it");

    asShow("BBB222");
    assert.deepEqual(cache.parkedSketchFor("BBB222"), [], "a different show must not reuse it");
    const restored = cache.restoreParkedSketch(null);
    assert.ok(
      !restored || !(restored.sketch || []).length,
      "show B must not be handed show A's sketch",
    );

    // Returning to the first show may still use the cached copy.
    asShow("AAA111");
    assert.deepEqual(cache.parkedSketchFor("AAA111"), sketchA, "returning to A reuses A");
  },

  "the zone sketch samples by position, keeping every block": () => {
    // The picker draws this sketch, and a seat that is not drawn cannot be
    // dragged over. Keeping every Nth seat in array order can skip a whole
    // block: seats arrive block-by-block, so a block shorter than the stride
    // that straddles no multiple of it vanishes. 60000 + 5 seats gives a
    // stride of 11, and the 5-seat block occupies indices 60000..60004 —
    // none divisible by 11. Grid sampling keeps it because it samples space.
    const down = sandbox.window.PureClick.downsampleSketch;
    assert.equal(typeof down, "function", "downsampleSketch must be exported");

    const points = [];
    for (let i = 0; i < 60000; i += 1) {
      points.push({ k: "big", x: i % 300, y: Math.floor(i / 300) });
    }
    for (let i = 0; i < 5; i += 1) points.push({ k: "island", x: 900 + i, y: 500 });

    const out = down(points);
    assert.ok(out.length < points.length, "a venue over the cap must shrink");
    assert.ok(
      out.some((row) => row.k === "island"),
      "a far block must survive sampling or it cannot be selected",
    );

    // Bounds must hold, or a drag along the venue edge misses seats.
    const span = (rows, axis) => [
      Math.min(...rows.map((r) => r[axis])),
      Math.max(...rows.map((r) => r[axis])),
    ];
    assert.deepEqual(span(out, "x"), span(points, "x"), "x bounds must be preserved");
    assert.deepEqual(span(out, "y"), span(points, "y"), "y bounds must be preserved");
  },

  "a small venue passes through the sampler untouched": () => {
    const points = [{ k: "a", x: 1, y: 1 }, { k: "a", x: 2, y: 2 }];
    assert.equal(sandbox.window.PureClick.downsampleSketch(points), points, "under the cap, do not copy or thin");
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    // Await the result: a test that returns a promise used to be counted as
    // passing the moment it was called, so every assertion inside it was
    // silently discarded. An async test that cannot fail is worse than none.
    await fn();
    console.log(`ok   ${name}`);
  } catch (error) {
    failed += 1;
    console.error(`FAIL ${name}\n     ${error.message}`);
  }
}
console.log(`\n${Object.keys(tests).length - failed}/${Object.keys(tests).length} passed`);
process.exit(failed ? 1 : 0);
