(() => {
  "use strict";

  const SEAT_STORAGE_KEY = "pureclick_seat_v1";
  const ARM_STORAGE_KEY = "pureclick_arm_v1";
  const SYNC_URL = "https://poticket.interpark.com/Book/BookMain.asp";
  const DEFAULT_SEAT_CONFIG = {
    enabled: true,
    grade_order: ["2", "3", "4", "1"],
    max_attempts: 80,
    retry_ms: 20,
    poll_ms: 40,
  };

  const seatState = {
    running: false,
    locked: false,
    attempts: 0,
    lastError: "",
    lastSeat: "",
  };

  const armState = {
    running: false,
    fired: false,
    lastError: "",
    waitingUrl: "",
  };

  const clockState = {
    offsetSeconds: 0,
    syncedAt: 0,
  };

  function log(...args) {
    console.log("[PureClick]", ...args);
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
    localStorage.setItem(SEAT_STORAGE_KEY, JSON.stringify(config, null, 2));
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
    localStorage.setItem(ARM_STORAGE_KEY, JSON.stringify(config, null, 2));
  }

  function parseHttpDate(value) {
    return new Date(value).getTime() / 1000;
  }

  function serverTimeUnix() {
    return Date.now() / 1000 + clockState.offsetSeconds;
  }

  async function syncServerClock(fallbackOffset = 0) {
    const startUnix = Date.now() / 1000;
    const startPerf = performance.now();
    try {
      const response = await fetch(SYNC_URL, { method: "HEAD", cache: "no-store" });
      const rawDate = response.headers.get("Date");
      if (!rawDate) throw new Error("missing Date header");
      const endPerf = performance.now();
      const rtt = (endPerf - startPerf) / 1000;
      const localMid = startUnix + rtt / 2;
      clockState.offsetSeconds = parseHttpDate(rawDate) - localMid;
      clockState.syncedAt = Date.now();
      log("Clock synced", { offsetMs: clockState.offsetSeconds * 1000, rttMs: rtt * 1000 });
      return clockState.offsetSeconds;
    } catch (error) {
      clockState.offsetSeconds = fallbackOffset;
      clockState.syncedAt = Date.now();
      log("Clock sync fell back to desktop offset", fallbackOffset, error);
      return fallbackOffset;
    }
  }

  async function waitUntilServerUnix(targetUnix) {
    while (serverTimeUnix() < targetUnix) {
      const remainingMs = (targetUnix - serverTimeUnix()) * 1000;
      await sleep(Math.min(20, Math.max(1, remainingMs - 4)));
    }
  }

  function updateOverlay(message, tone = "info") {
    let root = document.getElementById("pureclick-overlay");
    if (!root) {
      root = document.createElement("div");
      root.id = "pureclick-overlay";
      root.style.cssText = [
        "position:fixed",
        "right:16px",
        "bottom:16px",
        "z-index:2147483647",
        "background:#111827",
        "color:#fff",
        "padding:12px 14px",
        "border-radius:10px",
        "font:13px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
        "box-shadow:0 8px 24px rgba(0,0,0,.28)",
        "max-width:340px",
      ].join(";");
      document.body.appendChild(root);
    }
    const colors = {
      info: "#93c5fd",
      ok: "#86efac",
      warn: "#fcd34d",
      error: "#fca5a5",
    };
    root.innerHTML = `<strong style="color:${colors[tone] || colors.info}">PureClick</strong><br>${message}`;
  }

  function getInitData() {
    return window.__NEXT_DATA__?.props?.pageProps?.initData || null;
  }

  function parseGoodsCodeFromPath() {
    const match = location.pathname.match(/\/goods\/([A-Z0-9]+)/i);
    return match ? match[1].toUpperCase() : null;
  }

  async function fetchWaitingUrl(arm) {
    const params = new URLSearchParams({
      GroupCode: arm.goods_code,
      Tiki: "N",
      Point: "N",
      PlayDate: arm.play_date,
      PlaySeq: arm.play_seq,
      action: "https://poticket.interpark.com/Book/BookSession.asp",
    });
    const url = `https://api-ticketfront.interpark.com/v1/goods/${arm.goods_code}/waiting?${params}`;
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) {
      throw new Error(`waiting HTTP ${response.status}`);
    }
    const payload = await response.json();
    return payload?.data || payload?.waitingUrl || null;
  }

  function clickReserveButton() {
    const nodes = [...document.querySelectorAll("a,button")];
    const target = nodes.find((el) => el.textContent?.trim() === "예매하기");
    if (!target) return false;
    target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    return true;
  }

  async function fireEntry(arm) {
    const firedAt = serverTimeUnix();
    const latenessMs = (firedAt - arm.target_server_unix) * 1000;
    log("Entry fire", { firedAt, target: arm.target_server_unix, latenessMs });

    if (arm.dry_run) {
      updateOverlay(`Dry run entry at ${latenessMs >= 0 ? "+" : ""}${latenessMs.toFixed(2)} ms`, "ok");
      return { dryRun: true, latenessMs };
    }

    let waitingUrl = null;
    if (arm.use_waiting_api !== false) {
      try {
        waitingUrl = await fetchWaitingUrl(arm);
        armState.waitingUrl = waitingUrl || "";
        log("waiting API", waitingUrl);
      } catch (error) {
        armState.lastError = String(error);
        log("waiting API failed", error);
      }
    }

    const clicked = clickReserveButton();
    if (waitingUrl) {
      updateOverlay("Entering queue...", "info");
      location.href = waitingUrl;
      return { waitingUrl, clicked, latenessMs };
    }
    if (clicked) {
      updateOverlay("Clicked 예매하기 (API fallback)", "warn");
      return { clicked, latenessMs };
    }
    throw new Error(armState.lastError || "waiting API and 예매하기 click both failed");
  }

  async function runArmScheduler(arm) {
    if (!arm?.enabled || arm.fired || armState.running) return;
    if (armState.fired) return;

    armState.running = true;
    updateOverlay("Syncing clock before entry...", "info");
    await syncServerClock(Number(arm.offset_seconds || 0));

    const remaining = arm.target_server_unix - serverTimeUnix();
    updateOverlay(
      `Armed for ${arm.dry_run ? "dry-run " : ""}entry<br>${Math.max(0, remaining).toFixed(1)}s remaining`,
      "info",
    );

    await waitUntilServerUnix(Number(arm.target_server_unix));

    try {
      await fireEntry(arm);
      arm.fired = true;
      armState.fired = true;
      saveArmConfig({ ...arm, fired: true });
    } catch (error) {
      armState.lastError = String(error);
      updateOverlay(`Entry failed: ${error}`, "error");
      log("Entry failed", error);
    } finally {
      armState.running = false;
    }
  }

  function rankGrade(seatGrade, gradeOrder) {
    const index = gradeOrder.indexOf(String(seatGrade));
    return index === -1 ? gradeOrder.length : index;
  }

  function rankCandidates(candidates, gradeOrder) {
    return candidates.sort((a, b) => {
      const gradeDiff = rankGrade(a.seatGrade, gradeOrder) - rankGrade(b.seatGrade, gradeOrder);
      if (gradeDiff !== 0) return gradeDiff;
      if (a.rowNo !== b.rowNo) return a.rowNo.localeCompare(b.rowNo, "ko");
      if (a.seatNo !== b.seatNo) return a.seatNo.localeCompare(b.seatNo, "ko");
      return a.seatInfoId.localeCompare(b.seatInfoId);
    });
  }

  function seatFromFiber(el) {
    const fiberKey = Object.keys(el).find((key) => key.startsWith("__reactFiber"));
    if (!fiberKey) return null;
    let fiber = el[fiberKey];
    for (let depth = 0; depth < 12 && fiber; depth += 1) {
      const props = fiber.memoizedProps || fiber.pendingProps;
      if (props?.seat) return props.seat;
      fiber = fiber.return;
    }
    return null;
  }

  function collectDomCandidates(gradeOrder) {
    const nodes = [...document.querySelectorAll("circle.js-seat")].filter(
      (node) => !node.classList.contains("SeatMap_disabled__AZO_T"),
    );
    const candidates = [];
    for (const node of nodes) {
      const seat = seatFromFiber(node);
      if (!seat?.seatInfoId || !seat?.seatGrade) continue;
      if (!seat.isExposable || seat.seatGroupId) continue;
      candidates.push({
        seatInfoId: seat.seatInfoId,
        seatGrade: String(seat.seatGrade),
        seatGradeName: seat.seatGradeName || "",
        rowNo: seat.rowNo || "",
        seatNo: seat.seatNo || "",
        label: `[${seat.seatGradeName || seat.seatGrade}] ${seat.rowNo || ""} ${seat.seatNo || ""}`.trim(),
      });
    }
    return rankCandidates(candidates, gradeOrder);
  }

  async function fetchJson(url) {
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
    return response.json();
  }

  async function collectApiCandidates(initData, gradeOrder) {
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const blockParams = new URLSearchParams({
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
    });
    const blockPayload = await fetchJson(`/onestop/api/seats/block-data?${blockParams}`);
    const blocks = blockPayload?.blocks || blockPayload?.data?.blocks || blockPayload;
    if (!Array.isArray(blocks) || !blocks.length) return [];

    const blockKeys = blocks
      .map((block) => block?.blockKey || block?.key || block)
      .filter(Boolean)
      .map(String);

    const metaParams = new URLSearchParams({
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
      bizCode: initData.bizCode || "WEBBR",
    });
    for (const blockKey of blockKeys.slice(0, 4)) {
      metaParams.append("blockKeys", blockKey);
    }
    const metaBlocks = await fetchJson(`/onestop/api/seatMeta?${metaParams}`);
    const rows = Array.isArray(metaBlocks) ? metaBlocks : metaBlocks?.data || [];
    const candidates = [];
    for (const block of rows) {
      for (const seat of block?.seats || []) {
        if (!seat?.isExposable || seat?.seatGroupId || !seat?.seatGrade || !seat?.seatInfoId) continue;
        candidates.push({
          seatInfoId: seat.seatInfoId,
          seatGrade: String(seat.seatGrade),
          seatGradeName: seat.seatGradeName || "",
          rowNo: seat.rowNo || "",
          seatNo: seat.seatNo || "",
          label: `[${seat.seatGradeName || seat.seatGrade}] ${seat.rowNo || ""} ${seat.seatNo || ""}`.trim(),
        });
      }
    }
    return rankCandidates(candidates, gradeOrder);
  }

  async function selectSeat(initData, seat, { dryRun = false } = {}) {
    if (dryRun) return { dryRun: true, seat };
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const seatType = goods.kindOfGoods === "01007" && goods.isSportsGroup ? "SPORTS" : "DEFAULT";
    const body = {
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
      sessionId: initData.sessionId,
      seatType,
      autoAssign: false,
      seats: [{ seatGrade: seat.seatGrade, seatInfoId: seat.seatInfoId }],
    };
    const response = await fetch("/onestop/api/seats/select", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`select HTTP ${response.status}`);
    return response.json();
  }

  async function runSeatAutopilot(config, { probe = false } = {}) {
    if (seatState.running || seatState.locked) return;
    if (!config.enabled) return;

    const initData = getInitData();
    if (!initData?.sessionId || !initData?.goods || !initData?.playSeq) return;
    if (!initData.goods.reservedSeat) {
      updateOverlay("Not reserved seating. Seat autopilot skipped.", "warn");
      return;
    }

    seatState.running = true;
    seatState.attempts = 0;
    updateOverlay(probe ? "Probing seats..." : "Scanning seats...", "info");

    const gradeOrder = (config.grade_order || DEFAULT_SEAT_CONFIG.grade_order).map(String);
    let candidates = collectDomCandidates(gradeOrder);
    if (!candidates.length) {
      try {
        candidates = await collectApiCandidates(initData, gradeOrder);
      } catch (error) {
        log("seat API prefetch failed", error);
      }
    }

    if (probe) {
      seatState.running = false;
      const preview = candidates.slice(0, 5).map((seat) => seat.label).join("<br>");
      updateOverlay(
        `Probe only — no seat locked.<br>${candidates.length} candidates.<br>${preview || "No seats yet."}`,
        "ok",
      );
      return { count: candidates.length, top: candidates.slice(0, 5) };
    }

    while (seatState.attempts < config.max_attempts && !seatState.locked) {
      if (!candidates.length) {
        candidates = collectDomCandidates(gradeOrder);
        if (!candidates.length) {
          try {
            candidates = await collectApiCandidates(initData, gradeOrder);
          } catch (error) {
            seatState.lastError = String(error);
          }
        }
      }
      if (!candidates.length) {
        updateOverlay(`Waiting for seat map... (${seatState.attempts}/${config.max_attempts})`, "info");
        seatState.attempts += 1;
        await sleep(config.poll_ms);
        continue;
      }

      const seat = candidates.shift();
      seatState.attempts += 1;
      updateOverlay(`Locking ${seat.label}<br>Attempt ${seatState.attempts}/${config.max_attempts}`, "info");

      try {
        const result = await selectSeat(initData, seat);
        const blocked = result?.unselectableSeatInfoIds || [];
        if (blocked.length) {
          await sleep(config.retry_ms);
          continue;
        }
        seatState.locked = true;
        seatState.lastSeat = seat.label;
        updateOverlay(`Locked ${seat.label}<br>Finish payment manually.`, "ok");
        if (location.pathname.endsWith("/onestop/seat") && !location.search.includes("step=price")) {
          location.href = "/onestop/seat?step=price";
        }
        return;
      } catch (error) {
        seatState.lastError = String(error);
        await sleep(config.retry_ms);
      }
    }

    if (!seatState.locked) {
      updateOverlay(
        `No seat locked after ${seatState.attempts} attempts.${seatState.lastError ? `<br>${seatState.lastError}` : ""}`,
        "error",
      );
    }
    seatState.running = false;
  }

  function readShowContext() {
    const context = {
      goods_code: null,
      play_date: null,
      play_seq: null,
      goods_name: null,
      ready: false,
      url: location.href,
    };

    const pathMatch = location.pathname.match(/\/goods\/([A-Z0-9]+)/i);
    if (pathMatch) context.goods_code = pathMatch[1].toUpperCase();

    const initData = getInitData();
    if (initData?.goods?.goodsCode) context.goods_code = initData.goods.goodsCode;
    if (initData?.goods?.goodsName) context.goods_name = initData.goods.goodsName;
    if (initData?.playSeq?.playSeq) context.play_seq = initData.playSeq.playSeq;
    if (initData?.playSeq?.playDate) context.play_date = String(initData.playSeq.playDate);

    const pageProps = window.__NEXT_DATA__?.props?.pageProps;
    const blob = JSON.stringify(pageProps || {});
    if (!context.play_date) {
      const playDateMatch = blob.match(/"playDate":"(\d{8})"/);
      if (playDateMatch) context.play_date = playDateMatch[1];
    }
    if (!context.play_seq) {
      const playSeqMatch = blob.match(/"playSeq":"(\d{3})"/);
      if (playSeqMatch) context.play_seq = playSeqMatch[1];
    }
    if (!context.goods_name) {
      const nameMatch = blob.match(/"goodsName":"([^"]+)"/);
      if (nameMatch) context.goods_name = nameMatch[1];
    }

    context.ready = !!(context.goods_code && context.play_date && context.play_seq);
    return context;
  }
    return location.hostname.includes("tickets.interpark.com") && location.pathname.includes("/goods/");
  }

  function isSeatPage() {
    return location.hostname.includes("tickets.interpark.com") && location.pathname.startsWith("/onestop/seat");
  }

  function bootRoute() {
    const arm = loadArmConfig();
    const seat = loadSeatConfig();

    if (isGoodsPage() && arm?.enabled && !arm.fired) {
      runArmScheduler(arm);
      return;
    }

    if (isSeatPage()) {
      if (location.search.includes("step=price")) {
        updateOverlay("Seat selected. Complete payment manually.", "ok");
        seatState.locked = true;
        return;
      }
      if (seat.enabled) runSeatAutopilot(seat);
    }
  }

  function boot() {
    const seat = loadSeatConfig();
    saveSeatConfig(seat);

    window.PureClick = {
      seatConfig: seat,
      armConfig: loadArmConfig,
      loadSeatConfig,
      saveSeatConfig,
      loadArmConfig,
      saveArmConfig,
      syncServerClock,
      serverTimeUnix,
      status: () => ({ seat: { ...seatState }, arm: { ...armState }, clock: { ...clockState } }),
      runEntry: () => runArmScheduler(loadArmConfig()),
      runSeats: () => runSeatAutopilot(loadSeatConfig()),
      probeSeats: () => runSeatAutopilot(loadSeatConfig(), { probe: true }),
      readShowContext,
      resetArm() {
        const arm = loadArmConfig();
        if (!arm) return null;
        const next = { ...arm, fired: false };
        saveArmConfig(next);
        armState.fired = false;
        return next;
      },
    };

    log("PureClick autopilot loaded");
    bootRoute();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  let lastPath = location.href;
  setInterval(() => {
    if (location.href === lastPath) return;
    lastPath = location.href;
    seatState.running = false;
    bootRoute();
  }, 250);
})();
