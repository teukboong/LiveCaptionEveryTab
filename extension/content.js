// Caption overlay over the YouTube/Twitch player. Display settings come from storage.local.
let box = null;
let settings = { fontSize: 25, bottomPct: 12, leftPct: 50, showSource: true, syncOffsetMs: 0, debugSync: false };
const LCC_IS_TOP = (window.top === window);
// With all_frames injection, exactly ONE frame renders captions: the video frame in video mode
// (it holds window.__lccVideoSub), otherwise the top frame. Prevents duplicate captions/transcript
// across iframes (and lets video mode reach a <video> inside a cross-origin iframe, e.g. Vimeo).
function lccShouldRender() { return (lccDelayMode === "video") ? !!window.__lccVideoSub : LCC_IS_TOP; }

function host() {
  return document.fullscreenElement || document.documentElement;
}
function ensureBox() {
  if (box && box.isConnected) return box;
  box = document.createElement("div");
  box.id = "lcc-overlay";
  box.innerHTML = '<div id="lcc-src"></div><div id="lcc-ko"></div><div id="lcc-debug"></div>';
  host().appendChild(box);
  applySettings();
  return box;
}
function applySettings() {
  if (!box) return;
  box.style.bottom = settings.bottomPct + "%";
  box.style.left = settings.leftPct + "%";
  box.style.right = "auto";
  box.style.transform = "translateX(-50%)";
  const ko = box.querySelector("#lcc-ko");
  const src = box.querySelector("#lcc-src");
  const dbg = box.querySelector("#lcc-debug");
  if (ko) ko.style.fontSize = settings.fontSize + "px";
  if (src) {
    src.style.fontSize = Math.round(settings.fontSize * 0.7) + "px";
    src.style.display = settings.showSource ? "block" : "none";
  }
  if (dbg) dbg.style.display = settings.debugSync ? "block" : "none";
}
function setSrc(text) {
  if (!lccShouldRender()) return;
  const b = ensureBox();
  b.style.display = "block";
  b.querySelector("#lcc-src").textContent = text || "";
  applySettings();
}
function setLines(srcText, koText, debugText, isDraft) {
  if (!lccShouldRender()) return;
  const b = ensureBox();
  b.style.display = "block";
  b.querySelector("#lcc-src").textContent = srcText || "";
  const ko = b.querySelector("#lcc-ko");
  ko.textContent = koText || "";
  // Optimistic captioning: an in-progress (draft) translation is dimmed+italic; once committed
  // (stable) it snaps to solid. Reads as "the caption is completing", not "the caption flickered".
  ko.style.transition = "opacity .15s ease";
  ko.style.opacity = isDraft ? "0.62" : "1";
  ko.style.fontStyle = isDraft ? "italic" : "normal";
  b.querySelector("#lcc-debug").textContent = settings.debugSync ? (debugText || "") : "";
  applySettings();
}

function setKoSplit(koEl, stable, draft) {
  // render the Korean line as a locked (solid) prefix + an in-progress (dim italic) suffix.
  koEl.textContent = "";
  if (stable) {
    const s = document.createElement("span");
    s.textContent = draft ? stable + " " : stable;
    s.style.opacity = "1";
    s.style.fontStyle = "normal";
    koEl.appendChild(s);
  }
  if (draft) {
    const d = document.createElement("span");
    d.textContent = draft;
    d.style.opacity = "0.62";
    d.style.fontStyle = "italic";
    koEl.appendChild(d);
  }
}
function setLinesSplit(srcText, koStable, koDraft, debugText) {
  if (!lccShouldRender()) return;
  const b = ensureBox();
  b.style.display = "block";
  b.querySelector("#lcc-src").textContent = srcText || "";
  const ko = b.querySelector("#lcc-ko");
  ko.style.transition = "opacity .15s ease";
  ko.style.opacity = "1";          // per-span opacity now carries the draft dim
  ko.style.fontStyle = "normal";
  setKoSplit(ko, koStable, koDraft);
  b.querySelector("#lcc-debug").textContent = settings.debugSync ? (debugText || "") : "";
  applySettings();
}

try {
  if (chrome.storage && chrome.storage.local) {
    chrome.storage.local.get("lcc-settings").then((r) => {
      if (r["lcc-settings"]) { settings = { ...settings, ...r["lcc-settings"] }; applySettings(); }
    });
  }
  if (chrome.storage && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && ch["lcc-settings"] && ch["lcc-settings"].newValue) {
        const oldOffset = settings.syncOffsetMs || 0;
        settings = { ...settings, ...ch["lcc-settings"].newValue };
        console.log("[lcc] settings → bottom", settings.bottomPct, "left", settings.leftPct, "size", settings.fontSize);
        applySettings();
        if ((settings.syncOffsetMs || 0) !== oldOffset) lccReclockPending();
      }
    });
  }
} catch (_) {}

// ---- caption display controller: committed captions are durable; source/preview are coalesced ----
const lccFinalQ = [];               // committed sentences waiting their turn on screen
const lccLatestRev = new Map();     // unit_id -> latest source/preview rev
const lccCommittedUnits = new Set();
const lccStreamedFinalUnits = new Map(); // unit_id -> perf time when a final_stream was actually rendered
let lccLivePartial = null;          // latest source/preview for the active unit
let lccHoldUntil = 0, lccShown = "", lccShownUnit = null, lccShownKind = "", lccStreamStartPerf = 0;
let lccDelayMode = "live", lccPlaybackDelayMs = 0;
const LCC_LAG_CAP_MS = 4000;   // start merging-to-catch-up at 4s lag (was 8s) so fast speech stays in sync
const LCC_CAPTION_LEAD_MS = 0;
const LCC_CAPTION_MAX_MS = 12000;   // sticky caption: clear only after this long with no new translation
const LCC_FINAL_STREAM_SEEN_TTL_MS = 30000; // if final_stream already rendered, don't replay its final later
const LCC_FINAL_QUEUE_CAP = 600;     // hard bound for very long/backlogged sessions
let lccLastKoT = 0;                 // perf time of the last translation shown (sticky timeout)
function lccReadMs(ko, preferred) { return preferred || Math.max(1300, Math.min(7000, (ko || "").length * 75)); }
function lccNow() { return performance.now(); }
function lccSyncOffsetMs() { return Number(settings.syncOffsetMs) || 0; }
function lccPerfFromWall(wallMs) {
  const wall = Number(wallMs);
  if (!Number.isFinite(wall) || wall <= 0) return lccNow();
  return lccNow() - Math.max(0, Date.now() - wall);
}
function lccStreamPerf(streamStartWall, streamStartPerf) {
  const perf = Number(streamStartPerf);
  if (Number.isFinite(perf) && perf > 0) return perf;
  return lccPerfFromWall(streamStartWall);
}
function lccLagMs(item, now) {
  const end = Number(item.end_ms);
  if (!lccStreamStartPerf || !Number.isFinite(end)) return now - (item.recvAtPerf || now);
  return now - (lccStreamStartPerf + end + lccPlaybackDelayMs + lccSyncOffsetMs());
}
function lccReadyCount(now) {
  return lccFinalQ.reduce((n, item) => n + ((item.dueAt || 0) <= now + 250 ? 1 : 0), 0);
}
function lccReclockPending() {
  for (const item of lccFinalQ) item.dueAt = lccDueAt(item);
  lccFinalQ.sort((a, b) => (a.dueAt || a.recvAtPerf || 0) - (b.dueAt || b.recvAtPerf || 0));
  if (lccLivePartial) lccLivePartial.dueAt = lccDueAt(lccLivePartial);
}
function lccSetPlaybackDelay(mode, delayMs, streamStartWall, streamStartPerf) {
  lccDelayMode = mode || "live";
  lccPlaybackDelayMs = Math.max(0, Number(delayMs) || 0);
  lccStreamStartPerf = lccStreamPerf(streamStartWall, streamStartPerf);
  lccReclockPending();
}
// Video mode renders captions as a subtitle track on delay.js's delayed canvas (it owns the matching
// clock), so we route captions there instead of the pacer. Audio mode (live video) has no such clock
// and keeps the pacer.
function lccVideoSub() { return (lccDelayMode === "video" && window.__lccVideoSub) ? window.__lccVideoSub : null; }
function lccMarkStreamClock(mode, delayMs, streamStartWall, streamStartPerf) {
  lccDelayMode = mode || lccDelayMode || "live";
  if (delayMs != null) lccPlaybackDelayMs = Math.max(0, Number(delayMs) || 0);
  lccStreamStartPerf = lccStreamPerf(streamStartWall, streamStartPerf);
  lccReclockPending();
  const v = lccVideoSub();
  if (v && v.reanchor) v.reanchor(lccStreamStartPerf);   // video reconnect: re-anchor the subtitle track (bridge audio_ms reset)
}
function lccDueAt(item) {
  if (!lccStreamStartPerf || item.start_ms == null) return item.recvAtPerf || lccNow();
  const start = Number(item.start_ms);
  if (!Number.isFinite(start)) return item.recvAtPerf || lccNow();
  const end = Number(item.end_ms);
  let anchor = start;
  if (Number.isFinite(end) && end > start) {
    anchor = end;
  }
  return lccStreamStartPerf + anchor + lccPlaybackDelayMs + lccSyncOffsetMs() - LCC_CAPTION_LEAD_MS;
}
function lccDecorateTiming(item) {
  item.recvAtPerf = item.recvAtPerf || lccNow();
  item.dueAt = lccDueAt(item);
  return item;
}
function lccDebugLine(item, now) {
  if (!settings.debugSync || !item) return "";
  const due = Number(item.dueAt);
  const lag = Number.isFinite(due) ? now - due : 0;
  const start = Number.isFinite(Number(item.start_ms)) ? Number(item.start_ms) : -1;
  const end = Number.isFinite(Number(item.end_ms)) ? Number(item.end_ms) : -1;
  return [
    item.kind || "?",
    "u=" + (item.unit ?? "-"),
    "s=" + Math.round(start),
    "e=" + Math.round(end),
    "due=" + Math.round(due),
    "now=" + Math.round(now),
    "lag=" + Math.round(lag) + "ms",
    "delay=" + Math.round(lccPlaybackDelayMs) + "ms",
    "off=" + Math.round(lccSyncOffsetMs()) + "ms",
    "q=" + lccFinalQ.length,
    item.tx_wait_ms == null ? "" : "txw=" + Math.round(Number(item.tx_wait_ms) || 0) + "ms",
    item.tx_backlog_ms == null ? "" : "txb=" + Math.round(Number(item.tx_backlog_ms) || 0) + "ms",
  ].filter(Boolean).join(" ");
}
function lccRememberFinalStream(unit, now) {
  if (!unit) return;
  lccStreamedFinalUnits.set(String(unit), now || lccNow());
  while (lccStreamedFinalUnits.size > 600) lccStreamedFinalUnits.delete(lccStreamedFinalUnits.keys().next().value);
}
function lccSeenFinalStream(unit, now) {
  if (!unit) return false;
  const t = lccStreamedFinalUnits.get(String(unit));
  if (!t) return false;
  if ((now || lccNow()) - t > LCC_FINAL_STREAM_SEEN_TTL_MS) {
    lccStreamedFinalUnits.delete(String(unit));
    return false;
  }
  return true;
}
function lccDropQueuedUnit(unit) {
  if (!unit) return;
  for (let i = lccFinalQ.length - 1; i >= 0; i--) {
    if (lccFinalQ[i] && lccFinalQ[i].unit === unit) lccFinalQ.splice(i, 1);
  }
}
// LocalAgreement (n=2) on the Korean stream: the word-prefix two consecutive renderings of the same
// unit agree on is "stable" (locked solid); the diverging tail is "draft" (dim). As a clause grows the
// confirmed head stops moving, so re-translations no longer repaint the whole line.
const lccKoState = { unit: null, prev: "", last: null, stableW: 0 };   // LocalAgreement split state (audio overlay)
function lccLcpWords(a, b) {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a[i] === b[i]) i++;
  return i;
}
// LocalAgreement n=2: confirm the word-prefix the previous DISTINCT hypothesis and the current one agree
// on. Re-rendering the same ko returns the same split (no spurious solidify); only a new, longer hypothesis
// promotes the dim tail to solid. `st` holds per-context state so video mode reuses this same function.
function lccNormKoHyp(s) {   // normalize so re-renders / spacing / Unicode noise don't read as a new hypothesis
  return (s || "").normalize("NFC").replace(/[\u200B-\u200D\uFEFF]/g, "").replace(/\s+/g, " ").trim();
}
function lccKoSplitInto(st, unit, ko) {
  const norm = lccNormKoHyp(ko);
  if (unit !== st.unit) { st.unit = unit; st.prev = ""; st.last = null; st.stableW = 0; }
  const cur = norm ? norm.split(" ") : [];
  if (norm === st.last) { const w = Math.min(st.stableW, cur.length); return { stable: cur.slice(0, w).join(" "), draft: cur.slice(w).join(" ") }; }
  const k = lccLcpWords(st.prev ? st.prev.split(" ") : [], cur);   // shrink allowed: solid must never contradict the current hypothesis
  st.prev = norm; st.last = norm; st.stableW = k;
  return { stable: cur.slice(0, k).join(" "), draft: cur.slice(k).join(" ") };
}
function lccShowSplit(src, koStable, koDraft, debugText) {
  const key = (src||"")+"|"+(koStable||"")+"\u22a5"+(koDraft||"")+"|S|"+(settings.debugSync?(debugText||""):"");
  if (key === lccShown) return;
  lccShown = key; setLinesSplit(src, koStable, koDraft, debugText);
}
function lccShow(src, ko, debugText, isDraft) {
  const key = (src||"")+"|"+(ko||"")+"|"+(isDraft?"D":"C")+"|"+(settings.debugSync?(debugText||""):"");
  if (key === lccShown) return;     // avoid redundant DOM writes
  lccShown = key; setLines(src, ko, debugText, isDraft);
}
function lccShowItem(item, now) {
  const debug = lccDebugLine(item, now);
  if (item.kind === "source") {
    setSrc(item.src);                  // update the source line only; keep the previous translation (sticky)
  } else if (item.kind === "preview" || item.kind === "final_stream") {
    const split = lccKoSplitInto(lccKoState, item.unit, item.ko);   // live stream: lock the confirmed head, dim the tail
    lccShowSplit(item.src, split.stable, split.draft, debug);
    if (item.ko) lccLastKoT = now;
  } else {
    const koShow = (item.degraded && item.ko) ? item.ko + " …" : item.ko;   // degraded = last KO partial on tx failure
    lccShow(item.src, koShow, debug, false);   // committed final: solid
    if (item.ko) lccLastKoT = now;     // a translation is on screen -> reset the sticky timeout
  }
  lccShownUnit = item && item.unit != null ? String(item.unit) : null;
  lccShownKind = item && item.kind ? String(item.kind) : "";
  if (item && item.kind === "final_stream" && item.ko) lccRememberFinalStream(lccShownUnit, now);
  if (settings.debugSync) console.log("[lcc-sync]", debug, item);
}
function lccUnit(msg) { return msg.unit_id == null ? null : String(msg.unit_id); }
function lccFresh(msg) {
  const unit = lccUnit(msg);
  if (!unit || msg.rev == null) return true;
  if (lccCommittedUnits.has(unit)) return false;
  const prev = lccLatestRev.get(unit) || 0;
  if (msg.rev < prev) return false;
  lccLatestRev.set(unit, msg.rev);
  if (lccLatestRev.size > 600) lccLatestRev.delete(lccLatestRev.keys().next().value);   // bound long sessions (Map keeps insertion order)
  return true;
}
function lccPaceReset() {
  lccFinalQ.length = 0;
  lccLatestRev.clear();
  lccCommittedUnits.clear();
  lccLivePartial = null;
  lccHoldUntil = 0;
  lccShown = ""; lccShownUnit = null; lccShownKind = "";
  lccStreamedFinalUnits.clear();
  lccKoState.unit = null; lccKoState.prev = ""; lccKoState.last = null; lccKoState.stableW = 0;
}
function lccScheduleFinal(item) {
  lccFinalQ.push(lccDecorateTiming(item));
  lccFinalQ.sort((a, b) => (a.dueAt || a.recvAtPerf || 0) - (b.dueAt || b.recvAtPerf || 0));
  while (lccFinalQ.length > LCC_FINAL_QUEUE_CAP) lccFinalQ.shift();
}
function lccTakeFinal(now) {
  const first = lccFinalQ[0];
  if (!first) return null;
  if ((first.dueAt || 0) > now) return null;
  lccFinalQ.shift();
  const shouldMerge = lccReadyCount(now) > 3 || lccLagMs(first, now) > LCC_LAG_CAP_MS;
  if (!shouldMerge) return first;
  const merged = { ...first };
  const parts = [first];
  while (parts.length < 4 && lccFinalQ.length) {
    const next = lccFinalQ[0];
    if ((next.dueAt || 0) > now + 250) break;
    if ((merged.ko.length + next.ko.length) > 220 && lccLagMs(next, now) <= LCC_LAG_CAP_MS) break;
    parts.push(lccFinalQ.shift());
  }
  if (parts.length === 1) return first;
  merged.src = parts.map((p) => p.src).filter(Boolean).join(" ");
  merged.ko = parts.map((p) => p.ko).filter(Boolean).join(" ");
  merged.display_ms = Math.max(...parts.map((p) => p.display_ms || 0), lccReadMs(merged.ko));
  merged.end_ms = parts[parts.length - 1].end_ms;
  return merged;
}
function lccPace() {
  const now = lccNow();
  if (lccLastKoT && now - lccLastKoT > LCC_CAPTION_MAX_MS && !lccFinalQ.length &&
      (!lccLivePartial || !lccLivePartial.ko)) {
    setLines("", "");          // sticky timeout: nothing shown for a long time -> clear
    lccLastKoT = 0;
  }
  if (now < lccHoldUntil) {
    // a fresh live source/partial that's already overdue cuts the hold short so it isn't blocked
    if (lccLivePartial && (lccLivePartial.dueAt || 0) <= now && lccHoldUntil > now + 220) lccHoldUntil = now + 200;
    return;
  }
  if (lccFinalQ.length) {
    const c = lccTakeFinal(now);
    if (c) {
      lccShowItem(c, now);
      const nextDue = lccFinalQ[0] && lccFinalQ[0].dueAt;
      const readMs = lccReadMs(c.ko, c.display_ms);
      const holdMs = nextDue ? Math.min(readMs, Math.max(650, nextDue - now - 80)) : readMs;
      lccHoldUntil = now + holdMs;
      return;
    }
  }
  if (lccLivePartial) {
    if (!lccLivePartial.dueAt || lccLivePartial.dueAt <= now) {
      lccShowItem(lccLivePartial, now);   // show source/preview when delayed playback reaches it
    }
  }
}
let lccPaceTimer = null;   // the 150ms pacer runs only during an active session, not on every tab forever
function lccStartPacer() { if (lccPaceTimer == null) lccPaceTimer = setInterval(lccPace, 150); }
function lccStopPacer() { if (lccPaceTimer != null) { clearInterval(lccPaceTimer); lccPaceTimer = null; } }

function lccHandleBridgeMessage(msg) {
  if (msg.type === "status") {
    if (msg.on) {
      lccSetPlaybackDelay(msg.mode || "live", msg.playbackDelayMs || 0);
      resetTranscript();
      lccPaceReset();
      lccStartPacer();
      setLines("", "● 자막 대기 중…");
    }
    else { lccStopPacer(); if (box) box.style.display = "none"; }
  } else if (msg.type === "vdelay-start") {
    lccSetPlaybackDelay("video", (Number(msg.delaySec) || 0) * 1000);
  } else if (msg.type === "stream-clock-start") {
    lccMarkStreamClock(msg.mode || lccDelayMode, msg.playbackDelayMs, msg.streamStartWall, msg.streamStartPerf);
  } else if (msg.type === "wsstate") {
    if (!msg.open) { lccPaceReset(); setLines("", "⚠ 브릿지 연결 끊김 — bridge/server.py 실행 확인"); }
  } else if (msg.type === "notice") {
    lccPaceReset(); setLines("", msg.text || "");
  } else if (msg.type === "translation-context-reset") {
    lccPaceReset();
    const v = lccVideoSub();
    if (v && v.reset) v.reset();
    setLines("", "");
  } else if (msg.type === "source") {
    if (lccFresh(msg)) {
      const v = lccVideoSub();
      if (v) v.live(msg);                          // video: subtitle track on the delayed canvas
      else lccLivePartial = lccDecorateTiming({
        kind: "source", unit: lccUnit(msg), rev: msg.rev || 0, src: msg.text || "", ko: "",
        start_ms: msg.start_ms, end_ms: msg.end_ms,
      });
    }
  } else if (msg.type === "caption_partial") {
    if (lccFresh(msg)) {
      const v = lccVideoSub();
      if (v) v.live(msg);
      else {
        const u = lccUnit(msg);
        const stale = msg.kind === "final_stream" && lccLivePartial && lccLivePartial.unit != null &&
                      u != null && Number(u) < Number(lccLivePartial.unit);   // older unit's final stream must not cover newer live source
        if (!stale) lccLivePartial = lccDecorateTiming({
          kind: msg.kind === "final_stream" ? "final_stream" : "preview", phase: msg.phase || msg.kind || "preview",
          unit: u, rev: msg.rev || 0, src: msg.source || "", ko: msg.ko || "",
          start_ms: msg.start_ms, end_ms: msg.end_ms, display_ms: msg.display_ms,
          tx_wait_ms: msg.translation_wait_ms, tx_backlog_ms: msg.translation_backlog_ms,
        });
      }
    }
  } else if (msg.type === "caption") {
    const unit = lccUnit(msg);
    if (unit) {
      lccCommittedUnits.add(unit);
      if (lccCommittedUnits.size > 600) lccCommittedUnits.delete(lccCommittedUnits.values().next().value);   // bound long sessions
    }
    if (lccShouldRender()) pushTranscript(msg.source, msg.ko);
    const v = lccVideoSub();
    if (v) v.final(msg);                           // video: cue on the delayed-canvas subtitle track (no pacer)
    else {
      const finalItem = lccDecorateTiming({
        kind: "final", unit, rev: msg.rev || 0, src: msg.source || "", ko: msg.ko || "",
        start_ms: msg.start_ms, end_ms: msg.end_ms, display_ms: msg.display_ms, degraded: !!msg.degraded,
        tx_wait_ms: msg.translation_wait_ms, tx_backlog_ms: msg.translation_backlog_ms,
      });
      const now = lccNow();
      const alreadyStreamed = lccSeenFinalStream(unit, now);
      const replacingVisibleStream = alreadyStreamed && unit && lccShownUnit === unit &&
                                     (lccShownKind === "final_stream" || lccShownKind === "preview" || lccShownKind === "source");
      if (lccLivePartial && lccLivePartial.unit === unit) lccLivePartial = null;
      lccDropQueuedUnit(unit);
      if (replacingVisibleStream) {
        // The user has just watched this unit arrive as a final_stream partial.  Do not enqueue the
        // committed caption as a brand-new subtitle; snap the visible line to its solid final form.
        lccShowItem(finalItem, now);
        lccHoldUntil = now + Math.max(500, Math.min(1400, lccReadMs(finalItem.ko, finalItem.display_ms)));
      } else if (!alreadyStreamed) {
        lccScheduleFinal(finalItem);    // audio mode: final caption not already streamed on-screen
      }
      if (unit) lccStreamedFinalUnits.delete(unit);
    }
  } else if (msg.type === "err") {
    lccPaceReset(); setLines("", "⚠ " + (msg.text || "오류"));
  } else if (msg.type === "transcript-clear") {
    resetTranscript();
  } else if (msg.type === "page-translate-start") {
    lccPageTranslateStart(msg.settings || {});
  } else if (msg.type === "page-translate-stop") {
    lccPageTranslateStop(true);
  } else if (msg.type === "page-translate-config") {
    lccPageTranslateConfig(msg.settings || {});
  } else if (msg.type === "dom_translate_result") {
    lccPageTranslateApply(msg);
  } else if (msg.type === "dom_translate_done") {
    lccPageTranslateDone(msg);
  } else if (msg.type === "dom_translate_busy") {
    lccPageTranslateRetry(msg);
  } else if (msg.type === "dom_translate_err") {
    lccPageTranslateDrop(msg);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "page-context-get") {
    if (!LCC_IS_TOP) return;
    lccUpdateContext();
    sendResponse({ context: lccLastCtx || lccPageContext() });
    return true;
  }
  lccHandleBridgeMessage(msg);
});

document.addEventListener("fullscreenchange", () => {
  if (!box) return;                          // re-host the caption overlay into the fullscreen subtree
  const visible = box.style.display !== "none";
  box.remove(); box = null;
  if (visible) ensureBox().style.display = "block";
});

// shared with delay.js (B-2 video-delay mode) so it can render captions through this overlay
window.__lccOverlay = {
  setLines,
  setLinesSplit,
  koSplitInto: lccKoSplitInto,
  setSrc,
  setPlaybackDelay: lccSetPlaybackDelay,
  markStreamClock: lccMarkStreamClock,
  handleBridgeMessage: lccHandleBridgeMessage,
};

// ---- auto-priming: feed the page/video title to ASR+translation as a term hint ----
// content scripts can read document.title freely (no permission); offscreen merges this into config.
function lccPageContext() {
  const strip = (s) => (s || "").replace(/\s*[-—|/]\s*(YouTube|Twitch|X|Twitter)\s*$/i, "").trim();
  const pick = (sel) => { const el = document.querySelector(sel); return (el && el.textContent || "").trim(); };
  const vid = pick("h1.ytd-watch-metadata__title, h1.title yt-formatted-string, #title h1");   // YouTube richer title
  const chan = pick("ytd-channel-name#channel-name a, ytd-channel-name a, #owner #channel-name a");
  let ctx = vid || strip(document.title);
  if (chan && !ctx.includes(chan)) ctx += " — " + chan;
  return ctx.replace(/\s+/g, " ").trim().slice(0, 120);
}
let lccLastCtx = "";
function lccUpdateContext() {
  const ctx = lccPageContext();
  if (ctx && ctx !== lccLastCtx) lccLastCtx = ctx;
}
lccUpdateContext();
setTimeout(lccUpdateContext, 2000);          // SPA titles often populate late
setTimeout(lccUpdateContext, 5000);
try {
  let lccTitleObserver = null;
  const tEl = document.querySelector("title");
  if (tEl) {
    lccTitleObserver = new MutationObserver(lccUpdateContext);
    lccTitleObserver.observe(tEl, { childList: true });
  }
  window.addEventListener("yt-navigate-finish", () => setTimeout(lccUpdateContext, 800));
  window.addEventListener("pagehide", () => {
    lccStopPacer();
    if (lccTitleObserver) lccTitleObserver.disconnect();
    lccPaceReset();
    lccTranscript.length = 0;
    lccPageTranslateStop(false);
    if (box) { box.remove(); box = null; }
  }, { once: true });
} catch (_) {}

// ---- page translation: direct DOM text replacement, driven by MutationObserver deltas ----
const LCC_PAGE_EXCLUDE_SELECTOR = [
  "script", "style", "noscript", "template", "svg", "canvas", "video", "audio",
  "input", "textarea", "select", "option", "pre", "code", "kbd", "samp",
  "[contenteditable='true']", "[contenteditable='']", "[aria-hidden='true']",
  "#lcc-overlay",
].join(",");
const LCC_PAGE_BATCH_POLICY = Object.freeze({
  page: Object.freeze({ batchSize: 8, batchChars: 3600, scanLimit: 150, maxInflight: 3, flushMs: 80 }),
  both: Object.freeze({ batchSize: 3, batchChars: 1600, scanLimit: 90, maxInflight: 1, flushMs: 140 }),
});
let lccPageTranslateOn = false;
let lccPageTranslateSettings = { ...globalThis.LCC_DEFAULT_SETTINGS };
let lccPageTranslateObserver = null;
let lccPageTranslateScrollHandler = null;
let lccPageTranslateFlushTimer = null;
let lccPageTranslateScanTimer = null;
let lccPageTranslateReqSeq = 0;
// Work is keyed by normalized source text, not by node: identical strings (the hundreds of repeated
// "Reply"/"Share"/"5h ago" on a real page) collapse to ONE model call and fan out to every node sharing
// the text. Two queues give viewport-first ordering — visible nodes translate before off-screen ones.
const lccPageHotQueue = [];                  // sourceKeys whose nodes are in the viewport now (drained first, reading order)
const lccPageColdQueue = [];                 // sourceKeys allowed but off-screen (drained only when hot is empty)
const lccPageWork = new Map();               // sourceKey -> { text, nodes:Set<node>, status:"queued"|"pending", hot:boolean }
const lccPageTranslateNodes = new Set();     // every node we hold state for (restore on stop)
const lccPageTranslateState = new WeakMap(); // node -> per-node DOM state
const lccPageTranslateRequests = new Map();  // requestId -> { keys:[sourceKey], timer }
const lccPageTranslateStats = { resultSeen: 0, applied: 0, dropNoNode: 0, dropSource: 0, dropChanged: 0, dropEmpty: 0 };
// Time-sliced scan: a persistent TreeWalker advanced in idle chunks so a huge DOM never blocks the main
// thread, plus a one-shot below-fold prefetch so scrolling lands on already-translated text.
const LCC_PAGE_SCAN_SLICE_MS = 6;            // main-thread budget per scan chunk
const LCC_PAGE_SCAN_CHUNK_NODES = 250;       // hard node cap per chunk (belt + suspenders)
const LCC_PAGE_PREFETCH_MAX_NODES = 2200;    // bound whole-page prefetch on pathological pages
let lccPageScanCursor = null;                // { walker, prefetch, seen } across chunks
let lccPageScanIdleId = 0;
let lccPagePrefetchTimer = 0;
let lccPagePrefetchDone = false;             // whole-doc prefetch already queued for this page
let lccPagePrefetchScanning = false;         // true only inside a prefetch chunk -> relax the viewport gate
// Cross-site UI-label cache: short labels learned anywhere (model-sourced) render instantly everywhere,
// across sessions. Keyed by target language only; bootstrapped from the static seed in page-seed.js.
const LCC_PAGE_LABEL_KEY = "lcc-page-label-cache-v1";
const LCC_PAGE_LABEL_MAX_CHARS = 24;
const LCC_PAGE_LABEL_MAX_ENTRIES = 600;
let lccPageLabelNs = "";
let lccPageLabelCache = new Map();
let lccPageLabelLoaded = false;
let lccPageLabelPersistTimer = null;
const LCC_PAGE_CACHE_KEY = "lcc-page-translation-cache-v1";
const LCC_PAGE_CACHE_MAX_PAGES = 12;
const LCC_PAGE_CACHE_MAX_ENTRIES = 900;
let lccPageTranslateCacheNs = "";
let lccPageTranslateCache = new Map();
let lccPageTranslateCacheLoaded = false;
let lccPageTranslateCacheSeq = 0;
let lccPageTranslateCachePersistTimer = null;
let lccPageTranslateCachePersistSnapshot = null;
let lccPageTranslateUrl = "";
let lccPageTranslateUrlTimer = null;
let lccPageTranslateLastContext = "";

function lccPageTranslatePolicy() {
  const mode = lccPageTranslateSettings && lccPageTranslateSettings.runMode;
  return globalThis.lccRunModeIncludesCaption(mode) ? LCC_PAGE_BATCH_POLICY.both : LCC_PAGE_BATCH_POLICY.page;
}
function lccPageHash(value) {
  const s = String(value || "").normalize("NFC");
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(36);
}
function lccPageUrlKey() {
  return String(location.href || "").split("#")[0].slice(0, 260);
}
function lccPageCacheNamespace() {
  const s = lccPageTranslateSettings || {};
  return [
    lccPageUrlKey(),
    s.targetLang || "",
    s.pageRegister || "",
    lccPageHash(s.pageContextHint || s.contextHint || ""),
    lccPageHash(s.pageGlossary || s.glossary || ""),
  ].join("|");
}
function lccPageSourceNorm(source) {
  return String(source || "").normalize("NFC").replace(/\s+/g, " ").trim();
}
function lccPageSourceKey(source) {
  const norm = lccPageSourceNorm(source);
  return norm.length + ":" + lccPageHash(norm);
}
function lccPageLabelNamespace() {
  return String((lccPageTranslateSettings && lccPageTranslateSettings.targetLang) || "");
}
async function lccPageLoadLabelCache() {
  const ns = lccPageLabelNamespace();
  if (lccPageLabelLoaded && lccPageLabelNs === ns) return;
  lccPageLabelNs = ns;
  lccPageLabelLoaded = false;
  lccPageLabelCache = new Map();
  try {
    const r = await chrome.storage.local.get(LCC_PAGE_LABEL_KEY);
    if (ns !== lccPageLabelNamespace()) return;          // target changed mid-load -> let the next call redo
    const lang = r[LCC_PAGE_LABEL_KEY] && r[LCC_PAGE_LABEL_KEY].langs && r[LCC_PAGE_LABEL_KEY].langs[ns];
    const entries = lang && lang.entries || {};
    for (const [key, rec] of Object.entries(entries)) {
      if (rec && typeof rec.source === "string" && typeof rec.target === "string") lccPageLabelCache.set(key, rec);
    }
  } catch (_) {}
  const seed = (globalThis.LCC_PAGE_SEED && globalThis.LCC_PAGE_SEED[ns]) || null;   // bootstrap; never overwrites learned
  if (seed) {
    for (const en of Object.keys(seed)) {
      const norm = lccPageSourceNorm(en);
      const rendered = String(seed[en] || "").trim();
      if (!norm || !rendered) continue;
      const key = lccPageSourceKey(norm);
      if (!lccPageLabelCache.has(key)) lccPageLabelCache.set(key, { source: norm, target: rendered, t: 0, seed: true });
    }
  }
  lccPageLabelLoaded = true;
}
function lccPageLabelRemember(norm, key, target) {
  if (!lccPageLabelLoaded || norm.length > LCC_PAGE_LABEL_MAX_CHARS) return;
  lccPageLabelCache.delete(key);
  lccPageLabelCache.set(key, { source: norm, target, t: Date.now() });
  while (lccPageLabelCache.size > LCC_PAGE_LABEL_MAX_ENTRIES) {
    lccPageLabelCache.delete(lccPageLabelCache.keys().next().value);
  }
  lccPageScheduleLabelPersist();
}
function lccPageScheduleLabelPersist() {
  if (!lccPageLabelNs || lccPageLabelPersistTimer) return;
  lccPageLabelPersistTimer = setTimeout(async () => {
    lccPageLabelPersistTimer = null;
    const ns = lccPageLabelNs;
    const rows = [...lccPageLabelCache.entries()].filter(([, rec]) => rec && !rec.seed)   // seed re-applies on load
      .sort((a, b) => (b[1].t || 0) - (a[1].t || 0)).slice(0, LCC_PAGE_LABEL_MAX_ENTRIES);
    try {
      const r = await chrome.storage.local.get(LCC_PAGE_LABEL_KEY);
      const store = r[LCC_PAGE_LABEL_KEY] && typeof r[LCC_PAGE_LABEL_KEY] === "object" ? r[LCC_PAGE_LABEL_KEY] : {};
      const langs = store.langs && typeof store.langs === "object" ? store.langs : {};
      const entries = {};
      for (const [key, rec] of rows) entries[key] = { source: rec.source, target: rec.target, t: rec.t };
      langs[ns] = { t: Date.now(), entries };
      const keep = Object.entries(langs).sort((a, b) => ((b[1] && b[1].t) || 0) - ((a[1] && a[1].t) || 0)).slice(0, 12);
      await chrome.storage.local.set({ [LCC_PAGE_LABEL_KEY]: { version: 1, langs: Object.fromEntries(keep) } });
    } catch (_) {}
  }, 400);
}
async function lccPageLoadCache() {
  await lccPageLoadLabelCache();
  const ns = lccPageCacheNamespace();
  if (lccPageTranslateCacheLoaded && lccPageTranslateCacheNs === ns) return;
  const seq = ++lccPageTranslateCacheSeq;
  lccPageTranslateCacheNs = ns;
  lccPageTranslateCacheLoaded = false;
  lccPageTranslateCache = new Map();
  try {
    const r = await chrome.storage.local.get(LCC_PAGE_CACHE_KEY);
    if (seq !== lccPageTranslateCacheSeq) return;
    const page = r[LCC_PAGE_CACHE_KEY] && r[LCC_PAGE_CACHE_KEY].pages && r[LCC_PAGE_CACHE_KEY].pages[ns];
    const entries = page && page.entries || {};
    for (const [key, rec] of Object.entries(entries)) {
      if (rec && typeof rec.source === "string" && typeof rec.target === "string") {
        lccPageTranslateCache.set(key, rec);
      }
    }
  } catch (_) {}
  lccPageTranslateCacheLoaded = true;
}
function lccPageScheduleCachePersist() {
  if (!lccPageTranslateCacheNs) return;
  lccPageTranslateCachePersistSnapshot = {
    ns: lccPageTranslateCacheNs,
    rows: [...lccPageTranslateCache.entries()].sort((a, b) => (b[1].t || 0) - (a[1].t || 0)).slice(0, LCC_PAGE_CACHE_MAX_ENTRIES),
  };
  if (lccPageTranslateCachePersistTimer) return;
  lccPageTranslateCachePersistTimer = setTimeout(async () => {
    lccPageTranslateCachePersistTimer = null;
    const snap = lccPageTranslateCachePersistSnapshot;
    if (!snap || !snap.ns) return;
    try {
      const r = await chrome.storage.local.get(LCC_PAGE_CACHE_KEY);
      const store = r[LCC_PAGE_CACHE_KEY] && typeof r[LCC_PAGE_CACHE_KEY] === "object" ? r[LCC_PAGE_CACHE_KEY] : {};
      const pages = store.pages && typeof store.pages === "object" ? store.pages : {};
      const entries = {};
      for (const [key, rec] of snap.rows) entries[key] = rec;
      pages[snap.ns] = { t: Date.now(), entries };
      const keep = Object.entries(pages).sort((a, b) => ((b[1] && b[1].t) || 0) - ((a[1] && a[1].t) || 0)).slice(0, LCC_PAGE_CACHE_MAX_PAGES);
      await chrome.storage.local.set({ [LCC_PAGE_CACHE_KEY]: { version: 1, pages: Object.fromEntries(keep) } });
    } catch (_) {}
  }, 250);
}
function lccPageCachedTarget(source) {
  const norm = lccPageSourceNorm(source);
  const key = lccPageSourceKey(norm);
  if (lccPageTranslateCacheLoaded) {                       // this page's cache first (most context-specific)
    const rec = lccPageTranslateCache.get(key);
    if (rec && rec.source === norm) return rec.target;
  }
  if (lccPageLabelLoaded) {                                // then the cross-site label cache + seed
    const rec = lccPageLabelCache.get(key);
    if (rec && rec.source === norm) return rec.target;
  }
  return "";
}
function lccPageRememberCache(source, target) {
  const norm = lccPageSourceNorm(source);
  const rendered = String(target || "").trim();
  if (!norm || !rendered) return;
  const key = lccPageSourceKey(norm);
  lccPageTranslateCache.delete(key);
  lccPageTranslateCache.set(key, { source: norm, target: rendered, t: Date.now() });
  while (lccPageTranslateCache.size > LCC_PAGE_CACHE_MAX_ENTRIES) {
    lccPageTranslateCache.delete(lccPageTranslateCache.keys().next().value);
  }
  lccPageScheduleCachePersist();
  if (norm.length <= LCC_PAGE_LABEL_MAX_CHARS) lccPageLabelRemember(norm, key, rendered);   // short -> cross-site
}
function lccPageTextParts(raw) {
  const text = String(raw || "");
  const pre = (text.match(/^\s*/) || [""])[0];
  const post = (text.match(/\s*$/) || [""])[0];
  const core = text.slice(pre.length, text.length - post.length);
  return { pre, core, post };
}
function lccPageHasLetters(text) {
  try { return /[\p{L}]/u.test(text); }
  catch (_) { return /[A-Za-z\u00c0-\uffff]/.test(text); }
}
function lccPageNodeStyled(parent) {
  if (!parent || !parent.isConnected) return false;
  const style = window.getComputedStyle(parent);
  if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
  const rect = parent.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function lccPageNearViewport(parent) {
  const rect = parent.getBoundingClientRect();
  return rect.bottom >= -200 && rect.top <= (window.innerHeight || 0) + 600;
}
function lccPageInViewport(parent) {
  if (!parent) return false;
  const rect = parent.getBoundingClientRect();
  const h = window.innerHeight || document.documentElement.clientHeight || 0;
  return rect.bottom >= 0 && rect.top <= h && rect.width > 0 && rect.height > 0;
}
// Distinctive-script targets let us skip nodes already written in the target language without a model
// round-trip. Latin-script targets (English/Spanish/French/...) can't be told apart by script alone, so
// they map to null and fall through to the model (which returns already-target text unchanged).
const LCC_PAGE_TARGET_SCRIPT = Object.freeze({
  Korean: /[가-힣ᄀ-ᇿ㄰-㆏]/g,
  Japanese: /[぀-ヿ一-鿿]/g,
  Chinese: /[一-鿿]/g,
  Russian: /[Ѐ-ӿ]/g, Ukrainian: /[Ѐ-ӿ]/g, Bulgarian: /[Ѐ-ӿ]/g, Serbian: /[Ѐ-ӿ]/g,
  Greek: /[Ͱ-Ͽ]/g,
  Arabic: /[؀-ۿ]/g, Persian: /[؀-ۿ]/g, Urdu: /[؀-ۿ]/g,
  Hebrew: /[֐-׿]/g,
  Thai: /[฀-๿]/g,
  Hindi: /[ऀ-ॿ]/g, Bengali: /[ঀ-৿]/g, Tamil: /[஀-௿]/g, Telugu: /[ఀ-౿]/g,
});
function lccPageAlreadyTarget(core) {
  const re = LCC_PAGE_TARGET_SCRIPT[lccPageTranslateSettings.targetLang];
  if (!re) return false;
  let letters;
  try { letters = (core.match(/\p{L}/gu) || []).length; }
  catch (_) { letters = (core.match(/[A-Za-zÀ-]/g) || []).length; }
  if (!letters) return false;
  return (core.match(re) || []).length / letters >= 0.6;
}
function lccPageNodeAllowed(node) {
  if (!lccPageTranslateOn || !LCC_IS_TOP || !node || node.nodeType !== Node.TEXT_NODE) return false;
  const parent = node.parentElement;
  if (!parent || parent.closest(LCC_PAGE_EXCLUDE_SELECTOR)) return false;
  const state = lccPageTranslateState.get(node);
  if (state && state.translatedFull && node.nodeValue === state.translatedFull) return false;
  if (!lccPageNodeStyled(parent)) return false;
  if (!lccPagePrefetchScanning && !lccPageNearViewport(parent)) return false;   // prefetch relaxes the window
  const { core } = lccPageTextParts(node.nodeValue);
  const minChars = Number(lccPageTranslateSettings.pageTranslateMinChars) || 2;
  const maxChars = Number(lccPageTranslateSettings.pageTranslateMaxChars) || 900;
  if (core.length < minChars || core.length > maxChars) return false;
  if (!lccPageHasLetters(core)) return false;
  if (/^[\d\s.,:%()+\-–—/\\]+$/.test(core)) return false;
  if (lccPageAlreadyTarget(core)) return false;     // already in a distinctive-script target -> no round-trip
  return true;
}
function lccPageStateFor(node) {
  let state = lccPageTranslateState.get(node);
  if (!state) {
    state = { pending: false, source: "", originalFull: "" };
    lccPageTranslateState.set(node, state);
    lccPageTranslateNodes.add(node);
  }
  return state;
}
function lccPagePruneWork(work) {
  // Keep only nodes still in the DOM holding the exact source we queued (untranslated, unchanged).
  for (const n of [...work.nodes]) {
    const st = lccPageTranslateState.get(n);
    if (!n.isConnected || !st || n.nodeValue !== st.expectedFull) work.nodes.delete(n);
  }
  return work.nodes.size > 0;
}
function lccPageApplyToNode(node, state, source, target, expectedFull, pre, post, originalFull) {
  state.source = source;
  state.pre = pre || "";
  state.post = post || "";
  state.expectedFull = expectedFull;
  state.originalFull = originalFull || expectedFull;
  state.pending = false;
  state.translated = target;
  state.translatedFull = state.pre + target + state.post;
  node.nodeValue = state.translatedFull;       // direct browser DOM replacement, not an overlay
}
function lccPageQueueNode(node) {
  if (!lccPageNodeAllowed(node)) return false;
  const state = lccPageStateFor(node);
  const expectedFull = node.nodeValue;
  let sourceFull = expectedFull;
  if (state.translatedFull && expectedFull.startsWith(state.translatedFull)) {
    sourceFull = (state.originalFull || "") + expectedFull.slice(state.translatedFull.length);
  }
  const parts = lccPageTextParts(sourceFull);
  const cachedTarget = lccPageCachedTarget(parts.core);
  if (cachedTarget) {
    lccPageApplyToNode(node, state, parts.core, cachedTarget, expectedFull, parts.pre, parts.post, sourceFull);
    lccPageTranslateStats.applied += 1;
    return false;
  }
  state.source = parts.core;
  state.pre = parts.pre;
  state.post = parts.post;
  state.expectedFull = expectedFull;
  state.originalFull = sourceFull;
  state.pending = false;
  const key = lccPageSourceKey(parts.core);
  const hot = lccPageInViewport(node.parentElement);
  let work = lccPageWork.get(key);
  if (work) {
    work.nodes.add(node);                 // another node with the same text -> rides the one in-flight call
    if (hot && !work.hot && work.status === "queued") {   // scrolled into view -> jump to the hot queue
      work.hot = true;
      lccPageHotQueue.push(key);
    }
    return false;
  }
  work = { text: parts.core, nodes: new Set([node]), status: "queued", hot };
  lccPageWork.set(key, work);
  (hot ? lccPageHotQueue : lccPageColdQueue).push(key);
  lccPageScheduleFlush();
  return true;
}
function lccPageScanNode(root, limit) {
  if (!root || !lccPageTranslateOn || !LCC_IS_TOP) return 0;
  limit = limit == null ? lccPageTranslatePolicy().scanLimit : limit;
  let count = 0;
  if (root.nodeType === Node.TEXT_NODE) {
    return lccPageQueueNode(root) ? 1 : 0;
  }
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node && count < limit) {
    if (lccPageQueueNode(node)) count += 1;
    node = walker.nextNode();
  }
  return count;
}
function lccPageRoot() {
  const selector = String(lccPageTranslateSettings.pageTranslateSelector || "body").trim() || "body";
  try { return document.querySelector(selector) || document.body || document.documentElement; }
  catch (_) { return document.body || document.documentElement; }
}
function lccPageRequestIdle(fn, timeout) {
  if (typeof requestIdleCallback === "function") return requestIdleCallback(fn, { timeout });
  return setTimeout(fn, 16);
}
function lccPageCancelIdle(id) {
  if (!id) return;
  if (typeof cancelIdleCallback === "function") { try { cancelIdleCallback(id); } catch (_) {} }
  clearTimeout(id);
}
function lccPageNow() {
  return (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
}
function lccPageStartScan(prefetch) {
  lccPageCancelIdle(lccPageScanIdleId); lccPageScanIdleId = 0;
  if (!lccPageTranslateOn || !LCC_IS_TOP) { lccPageScanCursor = null; return; }
  const root = lccPageRoot();
  if (!root) { lccPageScanCursor = null; return; }
  lccPageScanCursor = { walker: document.createTreeWalker(root, NodeFilter.SHOW_TEXT), prefetch: !!prefetch, seen: 0 };
  lccPageScanChunk();
}
function lccPageScanChunk() {
  lccPageScanIdleId = 0;
  const cur = lccPageScanCursor;
  if (!cur || !lccPageTranslateOn) { lccPageScanCursor = null; return; }
  const t0 = lccPageNow();
  let processed = 0;
  lccPagePrefetchScanning = cur.prefetch;     // lccPageNodeAllowed relaxes the viewport gate while this is set
  try {
    let node = cur.walker.nextNode();
    while (node) {
      lccPageQueueNode(node);
      cur.seen += 1;
      processed += 1;
      if (cur.prefetch && cur.seen >= LCC_PAGE_PREFETCH_MAX_NODES) { cur.capped = true; node = null; break; }
      if (processed >= LCC_PAGE_SCAN_CHUNK_NODES || (lccPageNow() - t0) > LCC_PAGE_SCAN_SLICE_MS) {
        lccPageScanIdleId = lccPageRequestIdle(lccPageScanChunk, 400);   // resume next idle tick
        return;                                                          // (finally restores the flag)
      }
      node = cur.walker.nextNode();
    }
  } finally {
    lccPagePrefetchScanning = false;
  }
  lccPageScanCursor = null;                    // walker exhausted (or prefetch hit its cap)
  if (cur.prefetch) {
    lccPagePrefetchDone = true;
    if (cur.capped) try { console.debug("[lcc] page prefetch capped at", LCC_PAGE_PREFETCH_MAX_NODES, "nodes"); } catch (_) {}
  } else {
    lccPageMaybePrefetch();                    // viewport pass done -> prefetch the rest when idle
  }
}
function lccPageMaybePrefetch() {
  if (!lccPageTranslateOn || !LCC_IS_TOP || lccPagePrefetchDone || lccPageScanCursor || lccPagePrefetchTimer) return;
  if (lccPageHotQueue.length || lccPageColdQueue.length || lccPageTranslateRequests.size) return;
  lccPagePrefetchTimer = lccPageRequestIdle(() => {
    lccPagePrefetchTimer = 0;
    if (lccPagePrefetchDone || lccPageScanCursor) return;
    if (lccPageHotQueue.length || lccPageColdQueue.length || lccPageTranslateRequests.size) return;   // still idle?
    lccPageStartScan(true);                    // whole-doc relaxed scan -> off-screen text queued as cold
  }, 1500);
}
function lccPageScheduleScan(ms = 180) {
  if (lccPageTranslateScanTimer) clearTimeout(lccPageTranslateScanTimer);
  lccPageTranslateScanTimer = setTimeout(() => {
    lccPageTranslateScanTimer = null;
    lccPageStartScan(false);                   // near-viewport, time-sliced; prefetch fires separately when idle
  }, ms);
}
function lccPageScheduleFlush(ms) {
  if (lccPageTranslateFlushTimer) return;
  ms = ms == null ? lccPageTranslatePolicy().flushMs : ms;
  lccPageTranslateFlushTimer = setTimeout(lccPageFlush, ms);
}
function lccPageFlush() {
  lccPageTranslateFlushTimer = null;
  if (!lccPageTranslateOn) return;
  const policy = lccPageTranslatePolicy();
  if (lccPageTranslateRequests.size >= policy.maxInflight) return;
  const items = [];
  let chars = 0;
  // Drain hot (in-viewport, reading order) before cold. Both arrays may hold stale keys (already batched, or
  // promoted hot->cold dupes) — those are skipped lazily. Returns false when the char budget stops the batch.
  const pull = (queue, wantHot) => {
    while (queue.length && items.length < policy.batchSize) {
      const key = queue[0];
      const work = lccPageWork.get(key);
      if (!work || work.status !== "queued" || work.hot !== wantHot) { queue.shift(); continue; }
      if (!lccPagePruneWork(work)) { queue.shift(); lccPageWork.delete(key); continue; }
      if (chars + work.text.length > policy.batchChars && items.length) return false;
      queue.shift();
      work.status = "pending";
      for (const n of work.nodes) { const st = lccPageTranslateState.get(n); if (st) st.pending = true; }
      items.push({ id: key, text: work.text });
      chars += work.text.length;
    }
    return true;
  };
  if (pull(lccPageHotQueue, true)) pull(lccPageColdQueue, false);
  if (items.length) {
    const requestId = "ptr" + (++lccPageTranslateReqSeq);
    const keys = items.map((it) => it.id);
    const timer = setTimeout(() => lccPageTranslateRetry({ request_id: requestId, retry_ms: 500 }), 30000);
    lccPageTranslateRequests.set(requestId, { keys, timer });
    try { chrome.runtime.sendMessage({ type: "page-translate-batch", requestId, items }); } catch (_) {}
  }
  if ((lccPageHotQueue.length || lccPageColdQueue.length) && lccPageTranslateRequests.size < policy.maxInflight) {
    lccPageScheduleFlush(Math.max(160, policy.flushMs * 2));
  }
}
function lccPageClearTransient(restore) {
  if (lccPageTranslateFlushTimer) { clearTimeout(lccPageTranslateFlushTimer); lccPageTranslateFlushTimer = null; }
  if (lccPageTranslateScanTimer) { clearTimeout(lccPageTranslateScanTimer); lccPageTranslateScanTimer = null; }
  lccPageCancelIdle(lccPageScanIdleId); lccPageScanIdleId = 0;
  lccPageCancelIdle(lccPagePrefetchTimer); lccPagePrefetchTimer = 0;
  lccPageScanCursor = null;
  lccPagePrefetchScanning = false;
  lccPagePrefetchDone = false;
  lccPageHotQueue.length = 0;
  lccPageColdQueue.length = 0;
  lccPageWork.clear();
  for (const req of lccPageTranslateRequests.values()) {
    if (req && req.timer) clearTimeout(req.timer);
  }
  lccPageTranslateRequests.clear();
  if (restore) {
    for (const node of lccPageTranslateNodes) {
      const state = lccPageTranslateState.get(node);
      if (state && node.isConnected && state.translatedFull && node.nodeValue === state.translatedFull) {
        node.nodeValue = state.originalFull;
      }
      lccPageTranslateState.delete(node);
    }
  } else {
    for (const node of lccPageTranslateNodes) lccPageTranslateState.delete(node);
  }
  lccPageTranslateNodes.clear();
  lccPageTranslateReqSeq = 0;
}
function lccPageNotifyReady() {
  if (!LCC_IS_TOP) return;
  try {
    chrome.runtime.sendMessage({ type: "content-ready", pageContext: lccPageContext(), pageUrl: location.href }, (res) => {
      if (chrome.runtime.lastError || !res || !res.pageTranslating) return;
      lccPageTranslateStart(res.settings || {});
    });
  } catch (_) {}
}
function lccPageHandleUrlOrContextChange() {
  if (!lccPageTranslateOn || !LCC_IS_TOP) return;
  const url = location.href;
  const ctx = lccPageContext();
  const urlChanged = url !== lccPageTranslateUrl;
  const ctxChanged = ctx !== lccPageTranslateLastContext;
  if (!urlChanged && !ctxChanged) return;
  lccPageTranslateUrl = url;
  lccPageTranslateLastContext = ctx;
  if (urlChanged) lccPageClearTransient(false);
  lccPageNotifyReady();
  lccPageLoadCache().finally(() => lccPageScheduleScan(urlChanged ? 200 : 0));
}
function lccPageStartUrlWatch() {
  if (lccPageTranslateUrlTimer) return;
  lccPageTranslateUrlTimer = setInterval(lccPageHandleUrlOrContextChange, 700);
}
function lccPageStopUrlWatch() {
  if (lccPageTranslateUrlTimer) {
    clearInterval(lccPageTranslateUrlTimer);
    lccPageTranslateUrlTimer = null;
  }
}
function lccPageTranslateStart(rawSettings) {
  if (!LCC_IS_TOP) return;
  lccPageTranslateSettings = globalThis.lccNormalizeSettings({ ...lccPageTranslateSettings, ...(rawSettings || {}) });
  lccPageTranslateOn = true;
  lccPageTranslateUrl = location.href;
  lccPageTranslateLastContext = lccPageContext();
  document.documentElement.dataset.lccPageTranslate = "on";
  if (!lccPageTranslateObserver) {
    lccPageTranslateObserver = new MutationObserver((records) => {
      if (!lccPageTranslateOn) return;
      for (const r of records) {
        if (r.type === "characterData") lccPageQueueNode(r.target);
        else if (r.type === "childList") {
          for (const n of r.addedNodes) lccPageScanNode(n, 30);
        }
      }
    });
    lccPageTranslateObserver.observe(document.documentElement, { subtree: true, childList: true, characterData: true });
  }
  if (!lccPageTranslateScrollHandler) {
    lccPageTranslateScrollHandler = () => lccPageScheduleScan(220);
    window.addEventListener("scroll", lccPageTranslateScrollHandler, { passive: true });
    window.addEventListener("resize", lccPageTranslateScrollHandler, { passive: true });
  }
  lccPageStartUrlWatch();
  lccPageLoadCache().finally(() => lccPageScheduleScan(0));
}
function lccPageTranslateConfig(rawSettings) {
  if (!lccPageTranslateOn) return;
  lccPageTranslateSettings = globalThis.lccNormalizeSettings({ ...lccPageTranslateSettings, ...(rawSettings || {}) });
  lccPageLoadCache().finally(() => lccPageScheduleScan(0));
}
function lccPageTranslateStop(restore) {
  lccPageTranslateOn = false;
  delete document.documentElement.dataset.lccPageTranslate;
  if (lccPageTranslateObserver) { lccPageTranslateObserver.disconnect(); lccPageTranslateObserver = null; }
  if (lccPageTranslateScrollHandler) {
    window.removeEventListener("scroll", lccPageTranslateScrollHandler);
    window.removeEventListener("resize", lccPageTranslateScrollHandler);
    lccPageTranslateScrollHandler = null;
  }
  lccPageStopUrlWatch();
  lccPageClearTransient(restore);
}
function lccPageTranslateApply(msg) {
  if (!lccPageTranslateOn) return;
  lccPageTranslateStats.resultSeen += 1;
  const key = String(msg.item_id || "");
  const source = String(msg.source || "");
  const target = String(msg.target || "").trim();
  const work = lccPageWork.get(key);
  const nodes = work ? work.nodes : null;
  if (!nodes || !nodes.size) { lccPageTranslateStats.dropNoNode += 1; if (work) lccPageWork.delete(key); return; }
  if (!target) {
    lccPageTranslateStats.dropEmpty += 1;
    for (const n of nodes) { const st = lccPageTranslateState.get(n); if (st) st.pending = false; }
    lccPageWork.delete(key);
    return;
  }
  let applied = 0;
  for (const node of nodes) {                     // one result paints every node that shared the source text
    const state = lccPageTranslateState.get(node);
    if (!node || !state || !node.isConnected) { lccPageTranslateStats.dropNoNode += 1; continue; }
    if (state.source !== source) { lccPageTranslateStats.dropSource += 1; continue; }
    const currentParts = lccPageTextParts(node.nodeValue);
    if (node.nodeValue !== state.expectedFull && currentParts.core !== source) { lccPageTranslateStats.dropChanged += 1; continue; }
    const pre = node.nodeValue === state.expectedFull ? (state.pre || "") : currentParts.pre;
    const post = node.nodeValue === state.expectedFull ? (state.post || "") : currentParts.post;
    lccPageApplyToNode(node, state, source, target, node.nodeValue, pre, post);
    applied += 1;
  }
  lccPageRememberCache(source, target);
  lccPageTranslateStats.applied += applied;
  lccPageWork.delete(key);
}
function lccPageTranslateDrop(msg) {            // dom_translate_err for one item -> give up on it (no requeue)
  const key = String(msg.item_id || "");
  const work = lccPageWork.get(key);
  if (!work) return;
  for (const n of work.nodes) { const st = lccPageTranslateState.get(n); if (st) st.pending = false; }
  lccPageWork.delete(key);
}
function lccPageRequeueKey(key) {
  const work = lccPageWork.get(key);
  if (!work || work.status !== "pending") return;
  if (!lccPagePruneWork(work)) { lccPageWork.delete(key); return; }
  work.status = "queued";
  (work.hot ? lccPageHotQueue : lccPageColdQueue).push(key);
}
function lccPageTranslateDone(msg) {            // batch finished; any still-pending key got no result -> requeue
  const requestId = String(msg.request_id || "");
  const req = lccPageTranslateRequests.get(requestId);
  if (req && req.timer) clearTimeout(req.timer);
  if (req) for (const key of req.keys) lccPageRequeueKey(key);
  lccPageTranslateRequests.delete(requestId);
  if (lccPageHotQueue.length || lccPageColdQueue.length) lccPageScheduleFlush(120);
  else lccPageMaybePrefetch();                  // visible + queued work drained -> prefetch the rest when idle
}
function lccPageTranslateRetry(msg) {           // busy/timeout -> requeue the whole request with backoff
  const requestId = String(msg.request_id || "");
  const req = lccPageTranslateRequests.get(requestId);
  if (req && req.timer) clearTimeout(req.timer);
  lccPageTranslateRequests.delete(requestId);
  if (req) for (const key of req.keys) lccPageRequeueKey(key);
  lccPageScheduleFlush(Math.max(500, Math.min(5000, Number(msg.retry_ms) || 1600)));
}

function lccResumePageTranslateIfActive() {
  lccPageNotifyReady();
}
lccResumePageTranslateIfActive();

// ---- transcript accumulation -> storage.local (the popup renders history / export / summary / Q&A) ----
const lccTranscript = [];
let lccSessionStart = 0;
function resetTranscript() {
  lccTranscript.length = 0;
  lccSessionStart = 0;
  try { chrome.storage.local.remove(["lcc-transcript", "lcc-session"]); } catch (_) {}
}
function pushTranscript(source, ko) {
  if (!ko) return;
  if (!lccSessionStart) lccSessionStart = Date.now();
  lccTranscript.push({ t: Date.now(), source: source || "", ko: ko });
  if (lccTranscript.length > 1000) lccTranscript.shift();
  const title = (lccLastCtx || document.title || "Live Caption").replace(/\s*[-—|/]\s*(YouTube|Twitch|X|Twitter)\s*$/i, "").trim();
  try {
    chrome.storage.local.set({
      "lcc-transcript": lccTranscript.slice(-1000),
      "lcc-session": { start: lccSessionStart, title: title },
    });
  } catch (_) {}
}
