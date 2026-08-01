// ==UserScript==
// @name         PureClick Seat Autopilot
// @namespace    https://github.com/pureclick
// @version      1.0.0
// @description  Lock an Interpark onestop reserved seat after queue (Phase 2 MVP)
// @match        https://tickets.interpark.com/onestop/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
(() => {
  "use strict";

  const STORAGE_KEY = "pureclick_seat_v1";
  const DEFAULT_CONFIG = {
    enabled: true,
    grade_order: ["2", "3", "4", "1"],
    max_attempts: 80,
    retry_ms: 20,
    poll_ms: 40,
  };

  const state = {
    running: false,
    locked: false,
    attempts: 0,
    lastError: "",
    lastSeat: "",
  };

  function loadConfig() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return { ...DEFAULT_CONFIG };
      return { ...DEFAULT_CONFIG, ...JSON.parse(raw) };
    } catch {
      return { ...DEFAULT_CONFIG };
    }
  }

  function saveConfig(config) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config, null, 2));
  }

  function log(...args) {
    console.log("[PureClick Seat]", ...args);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function getInitData() {
    return window.__NEXT_DATA__?.props?.pageProps?.initData || null;
  }

  function parseGoodsCode() {
    const match = location.pathname.match(/\/goods\/([A-Z0-9]+)/i);
    return match ? match[1].toUpperCase() : null;
  }

  function rankGrade(seatGrade, gradeOrder) {
    const index = gradeOrder.indexOf(String(seatGrade));
    return index === -1 ? gradeOrder.length : index;
  }

  function rankCandidates(candidates, gradeOrder) {
    return candidates.sort((a, b) => {
      const gradeDiff =
        rankGrade(a.seatGrade, gradeOrder) - rankGrade(b.seatGrade, gradeOrder);
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
        blockKey: null,
        label: `[${seat.seatGradeName || seat.seatGrade}] ${seat.rowNo || ""} ${seat.seatNo || ""}`.trim(),
      });
    }
    return rankCandidates(candidates, gradeOrder);
  }

  async function fetchJson(url) {
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} for ${url}`);
    }
    return response.json();
  }

  async function fetchBlockKeys(initData) {
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const params = new URLSearchParams({
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
    });
    const payload = await fetchJson(`/onestop/api/seats/block-data?${params}`);
    const blocks = payload?.blocks || payload?.data?.blocks || payload;
    if (!Array.isArray(blocks)) return [];
    return blocks
      .map((block) => block?.blockKey || block?.key || block)
      .filter(Boolean)
      .map(String);
  }

  function chunkPairs(items, size) {
    const chunks = [];
    for (let index = 0; index < items.length; index += size) {
      chunks.push(items.slice(index, index + size));
    }
    return chunks;
  }

  async function fetchMetaBatch(initData, blockKeys) {
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const params = new URLSearchParams({
      goodsCode: goods.goodsCode,
      placeCode: goods.placeCode,
      playSeq: playSeq.playSeq,
      bizCode: initData.bizCode || "WEBBR",
    });
    for (const blockKey of blockKeys) {
      params.append("blockKeys", blockKey);
    }
    const payload = await fetchJson(`/onestop/api/seatMeta?${params}`);
    return Array.isArray(payload) ? payload : payload?.data || [];
  }

  async function collectApiCandidates(initData, gradeOrder) {
    const blockKeys = await fetchBlockKeys(initData);
    if (!blockKeys.length) return [];

    const batches = chunkPairs(blockKeys, 2);
    const metaBlocks = (
      await Promise.all(batches.map((batch) => fetchMetaBatch(initData, batch)))
    ).flat();

    const candidates = [];
    for (const block of metaBlocks) {
      const blockKey = block?.blockKey || null;
      for (const seat of block?.seats || []) {
        if (!seat?.isExposable || seat?.seatGroupId || !seat?.seatGrade || !seat?.seatInfoId) {
          continue;
        }
        candidates.push({
          seatInfoId: seat.seatInfoId,
          seatGrade: String(seat.seatGrade),
          seatGradeName: seat.seatGradeName || "",
          rowNo: seat.rowNo || "",
          seatNo: seat.seatNo || "",
          blockKey,
          label: `[${seat.seatGradeName || seat.seatGrade}] ${seat.rowNo || ""} ${seat.seatNo || ""}`.trim(),
        });
      }
    }
    return rankCandidates(candidates, gradeOrder);
  }

  async function selectSeat(initData, seat) {
    const goods = initData.goods;
    const playSeq = initData.playSeq;
    const seatType =
      goods.kindOfGoods === "01007" && goods.isSportsGroup ? "SPORTS" : "DEFAULT";
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
    if (!response.ok) {
      throw new Error(`select HTTP ${response.status}`);
    }
    return response.json();
  }

  function updateOverlay(message, tone = "info") {
    let root = document.getElementById("pureclick-seat-overlay");
    if (!root) {
      root = document.createElement("div");
      root.id = "pureclick-seat-overlay";
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
        "max-width:320px",
      ].join(";");
      document.body.appendChild(root);
    }
    const colors = {
      info: "#93c5fd",
      ok: "#86efac",
      warn: "#fcd34d",
      error: "#fca5a5",
    };
    root.innerHTML = `<strong style="color:${colors[tone] || colors.info}">PureClick Seat</strong><br>${message}`;
  }

  async function runAutopilot(config) {
    if (state.running || state.locked) return;
    if (!config.enabled) return;

    const initData = getInitData();
    if (!initData?.sessionId || !initData?.goods || !initData?.playSeq) {
      return;
    }
    if (!initData.goods.reservedSeat) {
      updateOverlay("This show is not reserved seating. Seat autopilot skipped.", "warn");
      return;
    }

    state.running = true;
    state.attempts = 0;
    updateOverlay("Scanning seats...", "info");

    const gradeOrder = (config.grade_order || DEFAULT_CONFIG.grade_order).map(String);
    let candidates = collectDomCandidates(gradeOrder);

    try {
      if (!candidates.length) {
        candidates = await collectApiCandidates(initData, gradeOrder);
      }
    } catch (error) {
      log("API candidate prefetch failed", error);
    }

    while (state.attempts < config.max_attempts && !state.locked) {
      if (!candidates.length) {
        candidates = collectDomCandidates(gradeOrder);
        if (!candidates.length) {
          try {
            candidates = await collectApiCandidates(initData, gradeOrder);
          } catch (error) {
            state.lastError = String(error);
          }
        }
      }
      if (!candidates.length) {
        updateOverlay(`Waiting for seat map... (${state.attempts}/${config.max_attempts})`, "info");
        state.attempts += 1;
        await sleep(config.poll_ms);
        continue;
      }

      const seat = candidates.shift();
      state.attempts += 1;
      updateOverlay(`Locking ${seat.label}<br>Attempt ${state.attempts}/${config.max_attempts}`, "info");

      try {
        const result = await selectSeat(initData, seat);
        const blocked = result?.unselectableSeatInfoIds || [];
        if (blocked.length) {
          log("Seat taken", seat.seatInfoId, blocked);
          await sleep(config.retry_ms);
          continue;
        }
        state.locked = true;
        state.lastSeat = seat.label;
        updateOverlay(`Locked ${seat.label}<br>Finish payment manually.`, "ok");
        log("Locked seat", seat);
        if (location.pathname.endsWith("/onestop/seat") && !location.search.includes("step=price")) {
          location.href = "/onestop/seat?step=price";
        }
        return;
      } catch (error) {
        state.lastError = String(error);
        log("Select failed", seat.seatInfoId, error);
        await sleep(config.retry_ms);
      }
    }

    if (!state.locked) {
      updateOverlay(
        `No seat locked after ${state.attempts} attempts.${state.lastError ? `<br>${state.lastError}` : ""}`,
        "error",
      );
    }
    state.running = false;
  }

  function shouldAutoRun() {
    if (!location.hostname.includes("tickets.interpark.com")) return false;
    if (location.pathname.startsWith("/onestop/seat")) {
      if (location.search.includes("step=price")) {
        updateOverlay("Seat already selected. Complete payment manually.", "ok");
        state.locked = true;
        return false;
      }
      return true;
    }
    return false;
  }

  function boot() {
    const config = loadConfig();
    window.PureClickSeat = {
      config,
      loadConfig,
      saveConfig,
      run: () => runAutopilot(loadConfig()),
      status: () => ({ ...state }),
      setEnabled(enabled) {
        const next = { ...loadConfig(), enabled: !!enabled };
        saveConfig(next);
        return next;
      },
      setGradeOrder(grades) {
        const next = { ...loadConfig(), grade_order: grades.map(String) };
        saveConfig(next);
        return next;
      },
    };
    saveConfig(config);
    log("Autopilot loaded", config);
    if (config.enabled && shouldAutoRun()) {
      runAutopilot(config);
    }
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
    const config = loadConfig();
    if (config.enabled && shouldAutoRun()) {
      state.running = false;
      runAutopilot(config);
    }
  }, 250);
})();
