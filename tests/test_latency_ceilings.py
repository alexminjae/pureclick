"""Hard latency ceilings and hot-path invariants, guarded statically.

These are the promises the engine makes about millisecond behaviour. They are
asserted against the source (not the network) so a regression that reintroduces
a timer in a hot path, or loosens a cadence, fails here rather than in a drop.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "browser" / "nolsniper_autopilot.js").read_text(encoding="utf-8")
ARM = (ROOT / "core" / "arm.py").read_text(encoding="utf-8")


def _const(name: str, text: str = JS) -> int:
    m = re.search(rf"const {name} = (\d+);", text) or re.search(rf"{name} = (\d+)\b", text)
    assert m, f"{name} not found"
    return int(m.group(1))


def _slice(start_marker: str, end_marker: str) -> str:
    a = JS.index(start_marker)
    b = JS.index(end_marker, a)
    return JS[a:b]


class CatchCadenceCeilings(unittest.TestCase):
    def test_focus_cadence_is_30ms_or_faster(self) -> None:
        self.assertLessEqual(_const("CATCH_FOCUS_POLL_MS"), 30)
        self.assertLessEqual(_const("CATCH_FAST_POLL_MS"), 30)

    def test_request_rate_is_capped(self) -> None:
        # <=60 req/s: the gateway's abuse block (~165s, measured) has no
        # measured threshold and 60/s is the highest rate that has run whole
        # watches without one. The stream is made gapless UNDER this, never by
        # raising it.
        self.assertLessEqual(_const("CATCH_MAX_REQUESTS_PER_SEC"), 60)
        self.assertGreaterEqual(_const("FOCUS_WORKERS"), 3)

    def test_focus_floor_is_15ms(self) -> None:
        self.assertEqual(_const("CATCH_FOCUS_POLL_MS"), 15)
        self.assertEqual(_const("CATCH_FAST_POLL_MS"), 15)

    def test_sends_are_evenly_spaced_at_the_cap(self) -> None:
        # Both guards on a send: spacing (one cap period since the last send)
        # and the trailing-second window. Spacing alone is what stops three
        # workers on a 10ms RTT firing 60 probes in 200ms and then sitting
        # silent for 800ms (measured before the pacer: gap max 761ms).
        can = _slice("function focusPollerCanSend(", "function focusPollerNextSlotMs(")
        self.assertIn("focusPoller.lastSentAt < FOCUS_SEND_PERIOD_MS", can)
        self.assertIn("focusPoller.sent.length < CATCH_MAX_REQUESTS_PER_SEC", can)
        nxt = _slice("function focusPollerNextSlotMs(", "function onPriceStep(")
        self.assertIn("Math.max(0, spacing, window)", nxt)
        # The idle branch waits exactly until the next slot, never a fixed period.
        idle = _slice("} else {\n        // Rate-capped", "focusPoller.inFlight = Math.max(0, focusPoller.inFlight);")
        self.assertIn("await pauseFor(Math.min(FOCUS_YIELD_MS, Math.max(1, focusPollerNextSlotMs())))", idle)
        self.assertNotIn("await sleep(", idle)

    def test_conflict_snap_tries_several_seats_with_no_sleep(self) -> None:
        seq = _slice("async function pressSequence(", "function startFocusPoller(")
        self.assertGreaterEqual(_const("PRESS_SNAP_MAX"), 2)
        # markSeatTaken then continue to the next seat — never a sleep between.
        self.assertRegex(seq, r"markSeatTaken\(seat\.seatInfoId\);")
        self.assertNotIn("await sleep(", seq)


class HoldLifecycleInvariants(unittest.TestCase):
    """Caught → held → (let go | touched | price step) leaves the engine PAUSED.

    The dynamic walk is tests/journey_hold_lifecycle.mjs (run from
    test_recovery_journeys.py); these are the shapes it relies on.
    """

    def test_the_focus_poller_dies_on_the_price_step(self) -> None:
        alive = _slice("function focusPollerAlive(", "// The floor on one worker's poll period.")
        self.assertIn("&& !onPriceStep()", alive)

    def test_the_run_loop_pauses_on_the_price_step(self) -> None:
        self.assertRegex(JS, r'if \(onPriceStep\(\)\) \{\s*pauseWatch\("priceStep"\);\s*return;')

    def test_a_pause_never_clears_the_map_or_releases_a_hold(self) -> None:
        fn = _slice("function pauseWatch(", "const HOLD_GUARD_MS")
        self.assertNotIn("clearSelectedSeats", fn)
        self.assertNotIn("releasePreselected", fn)
        # But it is a real stop: the sweep, the parked press, and the URL
        # watcher's ability to restart are all gone.
        for must in ("stopFocusPoller();", "abortSeatNetWaiters();", "seatState.haltedByUser = true;",
                     "window.__nolsniperRunGen = (window.__nolsniperRunGen || 0) + 1;"):
            self.assertIn(must, fn)

    def test_only_a_trusted_press_on_a_seat_counts_as_the_users_hand(self) -> None:
        fn = _slice("function onHumanPointer(", "function installHumanTouchGuard(")
        self.assertIn("event.isTrusted !== true) return;", fn)
        self.assertIn("if (!isSeatCircle(event.target)) return;", fn)
        self.assertIn('pauseWatch("humanTouch")', fn)
        install = _slice("function installHumanTouchGuard(", "// How long a freshly entered block")
        self.assertIn('document.addEventListener("pointerdown"', install)
        self.assertIn(", true);", install, "capture phase: before the page's own handler")

    def test_a_catch_starts_the_hold_guard_and_the_button_stops_it(self) -> None:
        seq = _slice("async function pressSequence(", "// ── Hold lifecycle")
        self.assertGreaterEqual(seq.count("startHoldGuard();"), 2, "both lock exits guard the hold")
        start = _slice("async function runSeatAutopilot(", "const blockedFor = gatewayBlockRemainingMs();")
        self.assertRegex(start, r'if \(userInitiated\) \{[\s\S]{0,200}seatState\.haltedByUser = false;[\s\S]{0,200}seatState\.pauseReason = "";')
        self.assertIn("stopHoldGuard();", start)

    def test_a_user_deselect_drops_the_lock_but_a_touch_keeps_the_selection(self) -> None:
        fn = _slice("function pauseWatch(", "const HOLD_GUARD_MS")
        self.assertRegex(fn, r'if \(reason === "userDeselect"\) \{[\s\S]{0,300}seatState\.heldSeatIds\.clear\(\);')
        self.assertNotRegex(fn, r'if \(reason === "humanTouch"\)')

    def test_the_press_path_is_warmed_before_the_first_probe(self) -> None:
        start = _slice("function startFocusPoller(", "// How long a freshly entered block")
        self.assertLess(start.index("warmPressPath(config, gradeOrder, blockKeys);"), start.index("void focusWorker("))

    def test_yield_fast_settles_one_waiter_per_message(self) -> None:
        y = _slice("const fastChannel = ", "// Resolve the moment")
        self.assertIn("fastWaiters.shift()", y)
        self.assertIn("fastWaiters.push(resolve)", y)


class AuditFindingsAreClosed(unittest.TestCase):
    """docs/FINAL_COMPREHENSIVE_SYSTEM_AUDIT.md, findings F1/F2/F4-F7, M1-M4, T1-T3, L1."""

    def test_f1_our_own_seatstatus_polls_bypass_our_own_fetch_hook(self) -> None:
        fn = _slice("async function fetchJson(", "if (!response.ok) {")
        self.assertIn("window.__nolsniperNativeFetch", fn)
        self.assertIn("/onestop\\/api\\/seatStatus/", fn)
        # …but only while window.fetch is our wrapper; a replaced fetch is honoured.
        self.assertIn("window.fetch === window.__nolsniperWrappedFetch", fn)
        self.assertIn("window.__nolsniperWrappedFetch = window.fetch = async function nolsniperFetch", JS)

    def test_f2_a_disabled_circle_is_pressed_through_the_pages_handler(self) -> None:
        fn = _slice("function clickSeatOnMap(", "function seatNodeDisabled(")
        handler_at = fn.index('if (via === "handler" || seatNodeDisabled(node))')
        pointer_at = fn.index('seatState.lastPressVia = "pointer";')
        self.assertLess(handler_at, pointer_at, "the handler is tried before the pointer press")
        self.assertIn("if (handlerOk()) return true;", fn)
        self.assertIn('traceClickAttempt(seatInfoId, node, "node-disabled");', fn, "the pointer fallback still refuses a disabled circle")
        walk = _slice("function pageHandlerFor(", "function pageSeatObject(")
        self.assertIn('typeof props.seatSelectHandler === "function"', walk)
        self.assertIn('typeof props.onSeatClick === "function" && props.blockKey && props.seatMeta', walk)
        self.assertNotRegex(walk, r"props\.(em|ed|ea|eb)\b", "keyed on shape, never on minified names")
        press = _slice("function pressViaHandler(", "function handlerReachable(")
        self.assertIn("handler.fn(true, found.seat, key, Boolean(handler.goods?.isInterlocking), undefined, undefined)", press)
        self.assertIn("handler.fn(found.seat, false, key)", press)

    def test_f2_the_page_is_nudged_on_every_flip_and_at_most_every_2s(self) -> None:
        apply = _slice("function applyBlockMask(", "function nudgePageRefresh(")
        self.assertIn("if (freed.length) nudgePageRefresh();", apply)
        nudge = _slice("function nudgePageRefresh(", "function notePageSeatStatus(")
        self.assertIn('window.dispatchEvent(new Event("online"))', nudge)
        self.assertIn('document.visibilityState === "hidden") return false', nudge)
        self.assertGreaterEqual(_const("SWR_NUDGE_MIN_GAP_MS"), 2000)

    def test_f4_f5_f6_the_queue_api_is_warmed_before_the_burst(self) -> None:
        self.assertLessEqual(_const("QUEUE_WARM_LEAD_MS"), 5000, "inside the browser's 5s preflight cache")
        self.assertGreaterEqual(_const("QUEUE_WARM_MIN_GAP_MS"), 4000)
        sched = _slice("async function runArmScheduler(", "async function parkInWatchedBlock(")
        self.assertIn("preconnectEntWaiting();", sched)
        self.assertIn("void premintOnLanding(arm);", sched)
        self.assertLess(sched.index('await warmQueueApi("scheduled")'), sched.index("await waitUntilServerUnix(entryStart, { cancelled });"))
        warm = _slice("async function warmQueueApi(", "function stopMintRefresh(")
        self.assertIn("post(SECURE_URL_PATH, {})", warm)
        self.assertIn('post(LINE_UP_PATH, { key: "" })', warm)
        pre = _slice("function preconnectEntWaiting(", "async function warmQueueApi(")
        self.assertIn('"dns-prefetch", "preconnect"', pre)
        self.assertIn("ENT_WAITING_ORIGIN", pre)
        landing = _slice("// Parked on the goods page for 지금 진입", "if (isNolProductPage()) {")
        self.assertIn('warmQueueApi("landing")', landing)

    def test_f7_the_fire_path_never_waits_on_a_bare_timer(self) -> None:
        fn = _slice("async function waitUntilServerUnix(", "// The same state machine as core/mode.py")
        self.assertNotIn("await sleep(", fn)
        self.assertIn("await pauseFor(Math.min(20, remainingMs - 4));", fn)
        burst = _slice("async function enterViaSecureUrlWithRetries(", "async function enterViaSecureUrl(")
        self.assertIn("await pauseFor(Math.max(0, interval - (performance.now() - startedPerf)));", burst)
        self.assertNotIn("await sleep(Math.max(0, interval", burst)

    def test_m1_m4_modal_classification(self) -> None:
        taken = re.search(r"const SEAT_TAKEN_DIALOG =\s*/(.+?)/;", JS, re.S).group(1)
        self.assertRegex("좌석 상태가 변경되었습니다.", taken)
        self.assertRegex("이미 선점된 좌석입니다.", taken)
        self.assertEqual(_const("HOLD_LIFETIME_MS"), 420000)
        expired = re.search(r"const HOLD_EXPIRED_DIALOG = /(.+?)/;", JS).group(1)
        self.assertRegex("좌석을 선택할 수 있는 시간 10분이 종료되었어요", expired)
        session = re.search(r"const SESSION_EXPIRED_DIALOG = /(.+?)/;", JS).group(1)
        self.assertRegex("세션이 만료되었습니다.", session)
        self.assertRegex("예매를 진행할 수 없습니다.", session)
        self.assertIn('NEVER_DISMISS_DIALOG = new Set(["captcha", "sessionExpired", "holdExpired"])', JS)
        dismiss = _slice("function dismissAnyBlockingOverlay(", "function dismissBlockingDialogs(")
        self.assertIn("if (NEVER_DISMISS_DIALOG.has(kind))", dismiss)
        captcha = re.search(r"function isCaptchaPageCopy\(text\) \{.*?return /(.+?)/\.test", JS, re.S).group(1)
        self.assertRegex("화살표를 밀어 퍼즐을 맞춰주세요", captcha)
        self.assertNotIn("동시 접속", JS, "no matcher for a string the site never shows")
        watch = _slice("function installDialogWatch(", "function onHoldExpired(")
        self.assertIn("observer.observe(document.body, { childList: true, subtree: true })", watch)
        self.assertIn("seatIndex.root.contains?.(record.target)) continue;", watch, "the seat map's own churn is skipped")

    def test_t3_p1_hot_path_pruning(self) -> None:
        captcha = _slice("function captchaPresent(", "function findCaptchaModal(")
        self.assertLess(captcha.index("captchaShapePresent()"), captcha.index("findCaptchaModal()"), "one querySelector before any innerText walk")
        self.assertIn("CAPTCHA_CHECK_TTL_MS", captcha)
        count = _slice("function selectedSeatCount(", "async function pageRegisteredSelection(")
        self.assertIn("if (seatCountNode) watchSeatCountNode(seatCountNode);", count)
        self.assertIn("seatCountObserver ? SEAT_COUNT_REVERIFY_WATCHED_EVERY : SEAT_COUNT_REVERIFY_EVERY", count)
        self.assertGreaterEqual(_const("SEAT_COUNT_REVERIFY_WATCHED_EVERY"), 100)

    def test_t1_t2_state_traps(self) -> None:
        self.assertIn("if (heldOnPage === 0 || (userInitiated && heldOnPage <= 0)) {", JS)
        self.assertIn("(pageHidden() ? HIDDEN_WATCH_TEXT : \"\")", JS)

    def test_l1_reload_hygiene(self) -> None:
        self.assertIn("if (window.__nolsniperOverlayHeadId) clearInterval(window.__nolsniperOverlayHeadId);", JS)
        self.assertIn("window.__nolsniperOverlayHeadId = setInterval(", JS)
        self.assertIn("window.__nolsniperSeatObserver?.disconnect();", JS)
        self.assertIn("window.__nolsniperSeatObserver = seatIndex.observer;", JS)
        self.assertIn("window.__nolsniperDialogObserver?.disconnect();", JS)


class ReviewBlockersAreClosed(unittest.TestCase):
    def test_blocker1_one_macrotask_then_confirm_then_a_120ms_commit_watchdog(self) -> None:
        seq = _slice("async function pressSequence(", "function startFocusPoller(")
        pre = seq.index('await waitForSeatNet("preselect", since, 2500);')
        hop = seq.index("await yieldFast();\n      if (halted()) return bail(lat);\n      // Then confirm")
        click = seq.index("if (clickConfirmSelect()) { confirmed = true; break; }")
        dog = seq.index("await waitForSelectSent(since, CONFIRM_WATCHDOG_MS);")
        self.assertTrue(pre < hop < click < dog)
        self.assertEqual(_const("CONFIRM_WATCHDOG_MS"), 120)
        self.assertIn("pressed: clickConfirmSelect()", seq, "the second press is decisive, not a wait")
        self.assertIn("noteSelectSent(sent)", JS)
        self.assertIn("noteSelectSent(sentAt)", JS)
        wd = _slice("async function waitForSelectSent(", "async function pressSequence(")
        self.assertNotIn("setTimeout", wd)
        self.assertIn("await yieldFast();", wd)

    def test_blocker2_root_handler_only_for_an_empty_cart_and_one_seat(self) -> None:
        fn = _slice("function pressViaHandler(", "function handlerReachable(")
        self.assertIn('if (handler?.kind === "root" && !((Number(quantity) || 1) === 1 && selectedSeatCount() === 0))', fn)
        self.assertIn("handler = handler.block || null;", fn)
        self.assertIn("quantity: Number((config || seatState.pressConfig || {}).quantity) || 1", JS)

    def test_expiry_modals_are_never_auto_pressed(self) -> None:
        self.assertIn('NEVER_DISMISS_DIALOG = new Set(["captcha", "sessionExpired", "holdExpired"])', JS)
        mounted = _slice("function onDialogMounted(", "function installDialogWatch(")
        self.assertIn("if (NEVER_DISMISS_DIALOG.has(kind) || !INFORMATIONAL_DIALOG.has(kind)) return false;", mounted)
        self.assertIn('if (kind === "unknown" && sessionClockExpired()) kind = "sessionExpired";', mounted)

    def test_chrome_is_launched_without_background_throttling(self) -> None:
        cdp = (ROOT / "mac" / "cdp.py").read_text(encoding="utf-8")
        self.assertIn('"--disable-background-timer-throttling",', cdp)
        self.assertIn('"--disable-backgrounding-occluded-windows",', cdp)


class HotPathsAreTimerFree(unittest.TestCase):
    def test_focus_poller_send_path_uses_no_timers(self) -> None:
        # The send path (fetch → diff → press) must never sit behind a timer;
        # a background window would clamp it. The one sleep is on the explicit
        # rate-limited idle branch, which is meant to wait.
        send = _slice("if (focusPollerCanSend()) {", "} else {")
        for banned in ("setTimeout", "setInterval", "requestIdleCallback", "await sleep("):
            self.assertNotIn(banned, send, banned)
        # The poller's own bookkeeping helpers never use timers either.
        helpers = _slice("function focusPollerCanSend(", "async function focusWorker(")
        for banned in ("setTimeout", "setInterval", "requestIdleCallback"):
            self.assertNotIn(banned, helpers, banned)

    def test_press_sequence_uses_no_timers(self) -> None:
        seq = _slice("async function pressSequence(", "function startFocusPoller(")
        for banned in ("setTimeout", "setInterval", "requestIdleCallback"):
            self.assertNotIn(banned, seq, banned)

    def test_short_waits_use_a_messagechannel_not_a_timer(self) -> None:
        y = _slice("function yieldFast() {", "// Resolve the moment")
        self.assertIn("postMessage", y)
        self.assertNotIn("setTimeout", y)


class EntryLeadCeilings(unittest.TestCase):
    def test_default_lead_is_150ms(self) -> None:
        self.assertEqual(_const("DEFAULT_ENTRY_LEAD_MS", ARM), 150)

    def test_lead_is_capped(self) -> None:
        self.assertLessEqual(_const("MAX_ENTRY_LEAD_MS", ARM), 600)


class ScheduleStepIsPrompt(unittest.TestCase):
    def test_no_15s_idle_budget_before_the_advance(self) -> None:
        fn = _slice("async function chooseRoundOnSchedule(", "function shownRoundOnSeatPage(")
        # The date/time/next presses are native and verified with short, bounded
        # windows — never SCHEDULE_STEP_TIMEOUT_MS idling on the whole step.
        self.assertIn("nativePress(cell)", fn)
        self.assertIn("nativePress(next)", fn)
        self.assertNotIn("SCHEDULE_STEP_TIMEOUT_MS", fn)

    def test_the_round_is_reconciled_by_seq_not_a_stale_date(self) -> None:
        boot = _slice("if (onSchedulePage()) {", "if ((isNolProductPage() || isGoodsPage())")
        self.assertIn("roundBySeq(", boot)


if __name__ == "__main__":
    unittest.main()


class SejongFloorGateInvariant(unittest.TestCase):
    """floorRank must derive a floor even when the seat carries only a block key
    — otherwise the 1층>2층 gate is a no-op on sketch-shaped data (측정:
    세종문화회관 3022-seat sketch has {k,x,y} and no floor field)."""

    def test_floorRank_has_a_block_key_fallback(self) -> None:
        fn = _slice("function floorRank(seat) {", "function rankCandidates(")
        self.assertIn("blockKey", fn, "floorRank must fall back to the block key")
        self.assertIn(r"/:(\d)\d\d", fn, "and read the floor digit from it")

    def test_the_floor_gate_sits_between_grade_and_distance(self) -> None:
        sort = _slice("const sorted = ranked.sort(", "recordSeatOrder(sorted")
        self.assertLess(sort.index("_rank"), sort.index("_floor"), "grade before floor")
        self.assertLess(sort.index("_floor"), sort.index("_posA"), "floor before distance")


class CatchWorkerYieldsToRenderer(unittest.TestCase):
    """focusWorker must yield a macrotask every iteration or two workers spin on
    microtasks and Chrome shows 페이지 응답 없음 (measured with fast-failing
    fetches). It must also floor the poll period so the yield actually spaces
    the loop, while never adding delay to a fetch that already took ~20ms."""

    def test_every_iteration_yields_a_real_macrotask(self) -> None:
        fn = _slice("async function focusWorker(", "focusPoller.inFlight = Math.max(0, focusPoller.inFlight);")
        # A MessageChannel hop after every send — unclamped, hands the renderer
        # a frame — so a fast-failing fetch cannot spin two workers into a freeze.
        self.assertGreaterEqual(fn.count("await yieldFast();"), 2, "yield on send and idle paths")
        self.assertNotIn("if (!freed.length) continue;", fn, "no bare continue without a yield")
        # No setTimeout floor on the send path (a background window clamps it to
        # ~1s and would collapse the rate); the fetch RTT paces sending instead.
        send = fn[: fn.index("} else {")]
        self.assertNotIn("await sleep(", send, "the send path is timer-free; the fetch paces it")

    def test_the_period_floor_is_about_20ms(self) -> None:
        self.assertLessEqual(_const("FOCUS_YIELD_MS"), 20)
        self.assertGreaterEqual(_const("FOCUS_YIELD_MS"), 10)
