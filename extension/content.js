let settings = globalThis.lccNormalizeSettings({});
try {
  if (chrome.storage && chrome.storage.local) {
    chrome.storage.local.get("lcc-settings").then((r) => {
      if (r["lcc-settings"]) { settings = globalThis.lccNormalizeSettings({ ...settings, ...r["lcc-settings"] }); applySettings(); }
    });
  }
  if (chrome.storage && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && ch["lcc-settings"] && ch["lcc-settings"].newValue) {
        const oldOffset = settings.syncOffsetMs || 0;
        settings = globalThis.lccNormalizeSettings({ ...settings, ...ch["lcc-settings"].newValue });
        console.log("[lcc] settings → bottom", settings.bottomPct, "left", settings.leftPct, "size", settings.fontSize);
        applySettings();
        if ((settings.syncOffsetMs || 0) !== oldOffset) lccReclockPending();
      }
    });
  }
} catch (_) {}

// #7 Editable glossary loop: a small bar (Alt+G) pins "source term = translation" into the live glossary.
// It reuses the popup's hot-reload path — write storage 'lcc-settings'.glossary + fire popup-config-update,
// which background/offscreen push to the bridge so it applies from the next utterance. No new wiring.
let lccGlossBar = null;
async function lccAddGlossary(term, tr) {
  term = (term || "").trim(); tr = (tr || "").trim();
  if (!term || !tr) return { ok: false, error: "원문·번역 둘 다 필요" };
  try {
    const s = (await chrome.storage.local.get("lcc-settings"))["lcc-settings"] || {};
    const lines = (s.glossary || "").split("\n").map((l) => l.trim()).filter(Boolean);
    const kept = lines.filter((l) => { const i = l.indexOf("="); const k = (i < 0 ? l : l.slice(0, i)).trim().toLowerCase(); return k !== term.toLowerCase(); });
    kept.push(`${term}=${tr}`);                                  // last wins: re-pinning a term replaces it
    s.glossary = kept.join("\n");
    await chrome.storage.local.set({ "lcc-settings": s });
    const pushed = await chrome.runtime.sendMessage({ type: "popup-config-update", resetTranslationContext: false });
    if (pushed && pushed.ok === false) throw new Error(pushed.error || "브릿지 설정 반영 실패");
    return { ok: true };
  } catch (e) { return { ok: false, error: e && e.message || "브릿지 설정 반영 실패" }; }
}
function lccEnsureGlossBar() {
  if (lccGlossBar && lccGlossBar.isConnected) return lccGlossBar;
  const bar = document.createElement("div");
  bar.id = "lcc-gloss-bar";
  bar.style.cssText = "position:fixed;top:8%;left:50%;transform:translateX(-50%);z-index:2147483647;display:none;" +
    "gap:6px;align-items:center;background:rgba(20,20,24,.95);color:#fff;padding:8px 10px;border-radius:10px;" +
    "font:14px/1.3 system-ui,-apple-system,sans-serif;box-shadow:0 4px 22px rgba(0,0,0,.45);";
  const mk = (ph, w) => { const i = document.createElement("input"); i.type = "text"; i.placeholder = ph;
    i.style.cssText = `width:${w}px;padding:5px 7px;border:1px solid #555;border-radius:6px;background:#111;color:#fff;font:inherit;`; return i; };
  const src = mk("원문 용어", 150);
  const arrow = document.createElement("span"); arrow.textContent = "→"; arrow.style.opacity = ".7";
  const tgt = mk("번역", 130);
  const add = document.createElement("button"); add.textContent = "추가";
  add.style.cssText = "padding:5px 12px;border:0;border-radius:6px;background:#3b82f6;color:#fff;font:inherit;cursor:pointer;";
  const msg = document.createElement("span"); msg.style.cssText = "opacity:.85;margin-left:4px;white-space:nowrap;";
  bar.append(src, arrow, tgt, add, msg);
  host().appendChild(bar);
  const submit = async () => {
    const res = await lccAddGlossary(src.value, tgt.value);
    if (res.ok) { msg.textContent = `✓ '${src.value.trim()}' 추가 (다음 발화부터)`; src.value = ""; tgt.value = ""; setTimeout(lccCloseGlossBar, 1100); }
    else { msg.textContent = res.error || "원문·번역 둘 다 필요"; }
  };
  add.addEventListener("click", (e) => { if (e.isTrusted) submit(); });   // page-synthesized clicks must not pin glossary into storage
  src.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); tgt.focus(); } });
  tgt.addEventListener("keydown", (e) => { if (e.isTrusted && e.key === "Enter") { e.preventDefault(); submit(); } });
  bar.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); lccCloseGlossBar(); } });
  bar._fields = { src, tgt, msg };
  lccGlossBar = bar;
  return bar;
}
function lccCloseGlossBar() { if (lccGlossBar) lccGlossBar.style.display = "none"; }
function lccToggleGlossBar() {
  if (!lccShouldRender()) return;   // only the frame that renders captions has lccLastSrc/data — never pin the bar in a focused iframe
  const bar = lccEnsureGlossBar();
  if (bar.style.display === "flex") { lccCloseGlossBar(); return; }
  bar._fields.msg.textContent = "";
  bar._fields.src.value = lccLastSrc || "";
  bar._fields.tgt.value = "";
  bar.style.display = "flex";
  bar._fields.src.focus(); bar._fields.src.select();
}
try {
  // Alt+G (e.code, layout-independent) toggles the glossary bar. Chrome-targeted; Atlas hooks Alt away.
  window.addEventListener("keydown", (e) => {
    if (e.isTrusted && e.altKey && e.code === "KeyG" && !lccEditableTarget(e.target)) { e.preventDefault(); lccToggleGlossBar(); }
  }, true);
} catch (_) {}

// #3 "방금 뭐랬지": Alt+R toggles a panel of the recent captions (text DVR — no audio replay). Reads
// lccTranscript (committed finals), newest at the bottom. Snapshot on open; reopen to refresh.
let lccRecentPanel = null;
function lccCloseRecent() { if (lccRecentPanel) lccRecentPanel.style.display = "none"; }
function lccShowRecent() {
  if (!lccRecentPanel || !lccRecentPanel.isConnected) {
    const p = document.createElement("div");
    p.id = "lcc-recent";
    p.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2147483646;display:none;" +
      "width:min(760px,86vw);max-height:64vh;overflow-y:auto;background:rgba(15,15,20,.96);color:#fff;padding:16px 20px;" +
      "border-radius:14px;box-shadow:0 8px 40px rgba(0,0,0,.55);font-family:system-ui,-apple-system,'Apple SD Gothic Neo',sans-serif;";
    host().appendChild(p);
    lccRecentPanel = p;
  }
  const p = lccRecentPanel;
  p.textContent = "";
  const head = document.createElement("div");
  head.textContent = "방금 자막 다시보기  ·  Alt+R / Esc 닫기";
  head.style.cssText = "font-size:13px;opacity:.55;margin-bottom:12px;";
  p.appendChild(head);
  const items = lccTranscript.slice(-14);
  if (!items.length) {
    const e = document.createElement("div"); e.textContent = "(아직 자막 기록이 없어요)"; e.style.opacity = ".6"; p.appendChild(e);
  } else {
    const now = Date.now();
    for (const it of items) {
      const row = document.createElement("div");
      row.style.cssText = "margin:10px 0;padding-left:11px;border-left:2px solid rgba(255,255,255,.13);";
      const ko = document.createElement("div");
      ko.style.cssText = "font-size:19px;font-weight:600;line-height:1.45;";
      const secs = Math.max(0, Math.round((now - (it.t || now)) / 1000));
      const ago = document.createElement("span");
      ago.textContent = (secs < 1 ? "방금" : "-" + secs + "s") + "  ";
      ago.style.cssText = "font-size:11px;opacity:.4;font-weight:400;";
      ko.appendChild(ago);
      ko.appendChild(document.createTextNode(it.ko || ""));
      row.appendChild(ko);
      if (it.source) {
        const src = document.createElement("div");
        src.textContent = it.source;
        src.style.cssText = "font-size:13px;opacity:.5;margin-top:3px;";
        row.appendChild(src);
      }
      p.appendChild(row);
    }
  }
  p.style.display = "block";
  p.scrollTop = p.scrollHeight;   // newest (bottom) into view
}
function lccToggleRecent() {
  if (!lccShouldRender()) return;   // transcript accumulates only in the render frame — opening here in a focused iframe shows an empty panel
  if (lccRecentPanel && lccRecentPanel.style.display === "block") { lccCloseRecent(); return; }
  lccShowRecent();
}
try {
  // Alt+R toggles the recent-captions panel; Esc closes it. Chrome-targeted.
  window.addEventListener("keydown", (e) => {
    if (e.altKey && e.code === "KeyR" && !lccEditableTarget(e.target)) { e.preventDefault(); lccToggleRecent(); }
    else if (e.key === "Escape" && lccRecentPanel && lccRecentPanel.style.display === "block") lccCloseRecent();
  }, true);
} catch (_) {}

function lccHandleBridgeMessage(msg) {
  if (msg && msg.end_ms != null) lccStreamResetIfRewound(Number(msg.end_ms));
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
    lccApplySpeaker(msg);
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
          number_uncertain: !!msg.number_uncertain, risk: msg.risk,
        });
      }
    }
  } else if (msg.type === "caption") {
    lccApplySpeaker(msg);
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
        number_uncertain: !!msg.number_uncertain, risk: msg.risk,
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
  } else if (msg.type === "dom_translate_partial") {
    lccPageTranslatePartial(msg);
  } else if (msg.type === "dom_translate_done") {
    lccPageTranslateDone(msg);
  } else if (msg.type === "dom_translate_busy") {
    lccPageTranslateRetry(msg);
  } else if (msg.type === "dom_translate_err") {
    lccPageTranslateDrop(msg);
  } else if (msg.type === "input_translate_result" || msg.type === "input_translate_err") {
    lccWbHandleResult(msg);
  } else if (msg.type === "ocr_translate_result" || msg.type === "ocr_translate_err") {
    lccOcrHandleResult(msg);
  } else if (msg.type === "page-translate-wsstate") {
    // The page-translate tab's own bridge-state signal (captions use "wsstate"). In-flight requests
    // self-heal via their retry timers on a drop; on reconnect, kick a scan so pending nodes resume
    // promptly instead of waiting for the next mutation/scroll.
    if (msg.open && lccPageTranslateOn) lccPageScheduleScan(0);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "lcc-ping") { if (LCC_IS_TOP) sendResponse({ ok: true }); return; }   // injection probe (background)
  if (msg.type === "page-context-get") {
    if (!LCC_IS_TOP) return;
    lccUpdateContext();
    sendResponse({ context: lccLastCtx || lccPageContext() });
    return true;
  }
  lccHandleBridgeMessage(msg);
});

// shared with delay.js (B-2 video-delay mode) so it can render captions through this overlay
window.__lccOverlay = {
  setLines,
  setLinesSplit,
  koSplitInto: lccKoSplitInto,
  setSrc,
  debugEnabled: () => !!settings.debugSync,   // delay.js builds its cue-clock debug line only when on
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
    lccFlushTranscript();        // persist any debounced-but-unwritten captions before we drop the in-memory copy
    lccTranscript.length = 0;
    lccPageTranslateStop(false);
    if (box) { box.remove(); box = null; }
  }, { once: true });
} catch (_) {}

lccResumePageTranslateIfActive();

// ---- write-back input translation: my draft -> the page's language, on demand only ----
// While page translation is on, focusing a text field shows a small "⇄" chip; clicking it (or Alt+T)
// sends the draft to the bridge's MAIN translator and replaces the field text with the rendering. Never
// automatic, never on page-synthesized events (isTrusted gates), and one-click revert for ~6s. The page
// language comes from <html lang> (unknown -> English); when it equals the user's target language the
// chip never appears (nothing to write back toward).
let lccWbBtn = null;
let lccWbField = null;
let lccWbSeq = 0;
const lccWbRequests = new Map();   // requestId -> { el, prev, timer }
const LCC_WB_MAX_CHARS = 4000;
const LCC_WB_REVERT_MS = 6000;

function lccWbEnabled() {
  return lccPageTranslateOn && lccPageTranslateSettings.writeBack !== false;
}
function lccWbPageLang() {
  const fromDoc = globalThis.lccLangNameFromCode(document.documentElement.lang || "");
  return fromDoc || "English";
}
function lccWbFieldEligible(el) {
  if (!el || !el.tagName) return false;
  if (el.isContentEditable === true) return true;
  if (el.tagName === "TEXTAREA") return !el.readOnly && !el.disabled;
  if (el.tagName !== "INPUT") return false;
  const type = (el.type || "text").toLowerCase();
  return (type === "text" || type === "search") && !el.readOnly && !el.disabled;
}
function lccWbFieldText(el) {
  return el.isContentEditable === true ? (el.innerText || "") : String(el.value || "");
}
function lccWbApply(el, text) {
  if (!el || !el.isConnected) return false;
  if (el.isContentEditable === true) {
    try {
      el.focus();
      document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, text);   // keeps the page's undo stack usable
      return true;
    } catch (_) { return false; }
  }
  el.value = text;
  try { el.dispatchEvent(new Event("input", { bubbles: true })); } catch (_) {}   // frameworks watch 'input'
  return true;
}
function lccWbEnsureBtn() {
  if (lccWbBtn && lccWbBtn.isConnected) return lccWbBtn;
  const b = document.createElement("button");
  b.id = "lcc-writeback";
  b.type = "button";
  b.style.cssText = "position:fixed;z-index:2147483647;display:none;padding:3px 9px;border:0;border-radius:7px;" +
    "background:rgba(30,30,36,.94);color:#fff;font:12px/1.4 system-ui,-apple-system,sans-serif;cursor:pointer;" +
    "box-shadow:0 2px 10px rgba(0,0,0,.4);";
  // mousedown steals focus from the field, which would hide the chip before click lands -> prevent it
  b.addEventListener("mousedown", (e) => e.preventDefault());
  b.addEventListener("click", (e) => { if (e.isTrusted) lccWbTrigger(); });
  (document.body || document.documentElement).appendChild(b);
  lccWbBtn = b;
  return b;
}
function lccWbHide() {
  if (lccWbBtn) lccWbBtn.style.display = "none";
  lccWbField = null;
}
function lccWbShowFor(el) {
  const lang = lccWbPageLang();
  if (lang === lccPageTranslateSettings.targetLang) return;   // already writing in the page's language
  const b = lccWbEnsureBtn();
  const rect = el.getBoundingClientRect();
  if (!rect || (rect.width === 0 && rect.height === 0)) return;
  lccWbField = el;
  delete b.dataset.lccRevert;
  b.textContent = "⇄ " + lang + " (Alt+T)";
  b.title = "입력한 글을 " + lang + "로 바꿔서 채워줍니다 (로컬 번역)";
  const vw = window.innerWidth || 0;
  b.style.display = "block";
  b.style.left = Math.max(4, Math.min(rect.right - (b.offsetWidth || 90), vw - (b.offsetWidth || 90) - 4)) + "px";
  b.style.top = Math.max(4, rect.top - (b.offsetHeight || 22) - 4) + "px";
}
function lccWbTrigger() {
  const el = lccWbField;
  if (!el || !lccWbEnabled()) return;
  const b = lccWbEnsureBtn();
  if (b.dataset.lccRevert) {                       // second press inside the window: restore the draft
    const req = lccWbRequests.get(b.dataset.lccRevert);
    if (req && lccWbApply(req.el, req.prev)) lccWbRequests.delete(b.dataset.lccRevert);
    delete b.dataset.lccRevert;
    b.textContent = "⇄ " + lccWbPageLang() + " (Alt+T)";
    return;
  }
  const draft = lccWbFieldText(el).slice(0, LCC_WB_MAX_CHARS);
  if (!draft.trim()) return;
  const requestId = "wb" + LCC_PAGE_FRAME_TAG + "-" + (++lccWbSeq);
  const timer = setTimeout(() => {
    lccWbRequests.delete(requestId);
    if (lccWbBtn && lccWbBtn.style.display !== "none") lccWbBtn.textContent = "⇄ 시간 초과";
  }, 25000);
  lccWbRequests.set(requestId, { el, prev: draft, timer });
  b.textContent = "번역 중…";
  try {
    chrome.runtime.sendMessage({ type: "input-translate", requestId, text: draft, targetLang: lccWbPageLang() },
      (res) => {
        if (chrome.runtime.lastError || (res && res.ok === false)) {
          clearTimeout(timer);
          lccWbRequests.delete(requestId);
          if (lccWbBtn) lccWbBtn.textContent = "⇄ 실패 — 브릿지 확인";
        }
      });
  } catch (_) {
    clearTimeout(timer);
    lccWbRequests.delete(requestId);
  }
}
function lccWbHandleResult(msg) {
  const requestId = String(msg.request_id || "");
  const req = lccWbRequests.get(requestId);
  if (!req) return;                                 // another frame's request (or expired)
  clearTimeout(req.timer);
  const out = String(msg.text || "").trim();
  if (msg.type === "input_translate_err" || !out || !lccWbApply(req.el, out)) {
    lccWbRequests.delete(requestId);
    if (lccWbBtn && lccWbBtn.style.display !== "none") lccWbBtn.textContent = "⇄ 실패";
    return;
  }
  if (lccWbBtn && lccWbField === req.el) {          // offer the one-click revert window
    lccWbBtn.dataset.lccRevert = requestId;
    lccWbBtn.textContent = "↩ 되돌리기";
    setTimeout(() => {
      if (lccWbBtn && lccWbBtn.dataset.lccRevert === requestId) {
        delete lccWbBtn.dataset.lccRevert;
        lccWbRequests.delete(requestId);
        if (lccWbField) lccWbBtn.textContent = "⇄ " + lccWbPageLang() + " (Alt+T)";
      }
    }, LCC_WB_REVERT_MS);
  } else {
    lccWbRequests.delete(requestId);
  }
}
try {
  document.addEventListener("focusin", (e) => {
    if (!lccWbEnabled()) return;
    if (lccWbFieldEligible(e.target)) lccWbShowFor(e.target);
    else lccWbHide();
  }, true);
  document.addEventListener("focusout", (e) => {
    // focus moving to the chip itself is prevented (mousedown preventDefault); any real focus change hides it
    if (e.target === lccWbField) lccWbHide();
  }, true);
  window.addEventListener("scroll", () => lccWbHide(), { passive: true, capture: true });
  window.addEventListener("keydown", (e) => {
    if (e.isTrusted && e.altKey && e.code === "KeyT" && lccWbEnabled() && lccWbFieldEligible(e.target)) {
      e.preventDefault();
      if (lccWbField !== e.target) lccWbShowFor(e.target);
      lccWbTrigger();
    }
  }, true);
} catch (_) {}

// ---- image OCR translation: Alt+hover an image -> local Vision OCR -> translated overlay ----
// Top frame only (captureVisibleTab coordinates are top-viewport); opt-in (pageOcr). The chip appears
// while Alt is held over a big-enough <img>; clicking it captures the image's RENDERED pixels (works
// for CORS/auth-gated images too), the bridge OCRs them on the Apple Neural Engine, and the lines come
// back as positioned overlay boxes. Results cache per image element; click the overlay (or scroll) to
// dismiss, re-trigger to show instantly.
let lccOcrChip = null;
let lccOcrImg = null;
let lccOcrSeq = 0;
let lccOcrOverlay = null;
const lccOcrRequests = new Map();   // requestId -> { img, timer }
const lccOcrCache = new WeakMap();  // img element -> blocks

function lccOcrEnabled() {
  return LCC_IS_TOP && lccPageTranslateOn && lccPageTranslateSettings.pageOcr === true;
}
function lccOcrEligible(el) {
  if (!el || el.tagName !== "IMG" || !el.isConnected) return false;
  const r = el.getBoundingClientRect();
  return r.width >= 80 && r.height >= 50;
}
function lccOcrChipText(text) {
  if (lccOcrChip) lccOcrChip.textContent = text;
}
function lccOcrChipHide() {
  if (lccOcrChip) lccOcrChip.style.display = "none";
  lccOcrImg = null;
}
function lccOcrEnsureChip() {
  if (lccOcrChip && lccOcrChip.isConnected) return lccOcrChip;
  const b = document.createElement("button");
  b.id = "lcc-ocr-chip";
  b.type = "button";
  b.style.cssText = "position:fixed;z-index:2147483647;display:none;padding:3px 9px;border:0;border-radius:7px;" +
    "background:rgba(30,30,36,.94);color:#fff;font:12px/1.4 system-ui,-apple-system,sans-serif;cursor:pointer;" +
    "box-shadow:0 2px 10px rgba(0,0,0,.4);";
  b.addEventListener("mousedown", (e) => e.preventDefault());
  b.addEventListener("click", (e) => { if (e.isTrusted) lccOcrTrigger(); });
  (document.body || document.documentElement).appendChild(b);
  lccOcrChip = b;
  return b;
}
function lccOcrShowChipFor(img) {
  const b = lccOcrEnsureChip();
  const r = img.getBoundingClientRect();
  lccOcrImg = img;
  b.textContent = "이미지 번역";
  b.style.display = "block";
  b.style.left = Math.max(4, r.left + 6) + "px";
  b.style.top = Math.max(4, r.top + 6) + "px";
}
function lccOcrHideOverlay() {
  if (lccOcrOverlay) { try { lccOcrOverlay.remove(); } catch (_) {} lccOcrOverlay = null; }
}
function lccOcrShowOverlay(img, blocks) {
  lccOcrHideOverlay();
  const r = img.getBoundingClientRect();
  const ov = document.createElement("div");
  ov.id = "lcc-ocr-overlay";
  ov.title = "클릭해서 닫기";
  ov.style.cssText = "position:fixed;z-index:2147483646;cursor:pointer;" +
    `left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;`;
  for (const b of blocks) {
    if (!b || !Array.isArray(b.box) || b.box.length < 4 || !b.target) continue;
    const lineH = (Number(b.line_h) || b.box[3]) * r.height;   // blocks merge multiple lines -> size by LINE height
    const d = document.createElement("div");
    d.textContent = b.target;
    d.title = b.source || "";
    d.style.cssText = "position:absolute;background:rgba(10,10,14,.82);color:#fff;border-radius:3px;" +
      "padding:0 3px;line-height:1.25;white-space:pre-wrap;overflow:visible;" +
      `left:${(b.box[0] * 100).toFixed(2)}%;top:${(b.box[1] * 100).toFixed(2)}%;` +
      `min-width:${(b.box[2] * 100).toFixed(2)}%;max-width:100%;` +
      `font-size:${Math.max(10, Math.min(26, Math.round(lineH * 0.72)))}px;`;
    ov.appendChild(d);
  }
  ov.addEventListener("click", () => lccOcrHideOverlay());
  (document.body || document.documentElement).appendChild(ov);
  lccOcrOverlay = ov;
}
function lccOcrTrigger() {
  const img = lccOcrImg;
  if (!img || !lccOcrEnabled()) return;
  const cached = lccOcrCache.get(img);
  if (cached) { lccOcrShowOverlay(img, cached); lccOcrChipHide(); return; }
  const r = img.getBoundingClientRect();
  const x = Math.max(0, r.left);
  const y = Math.max(0, r.top);
  const w = Math.min(r.right, window.innerWidth || 0) - x;
  const h = Math.min(r.bottom, window.innerHeight || 0) - y;
  if (w < 40 || h < 30) { lccOcrChipText("이미지를 화면에 더 보이게 해주세요"); return; }
  const requestId = "ocr" + LCC_PAGE_FRAME_TAG + "-" + (++lccOcrSeq);
  const timer = setTimeout(() => { lccOcrRequests.delete(requestId); lccOcrChipText("OCR 시간 초과"); }, 30000);
  lccOcrRequests.set(requestId, { img, timer });
  lccOcrChipText("읽는 중…");
  try {
    chrome.runtime.sendMessage(
      { type: "ocr-capture", requestId, rect: { x, y, w, h }, dpr: window.devicePixelRatio || 1 },
      (res) => {
        if (chrome.runtime.lastError || (res && res.ok === false)) {
          clearTimeout(timer);
          lccOcrRequests.delete(requestId);
          lccOcrChipText("실패: " + ((res && res.error) || "캡처 권한 — 팝업에서 다시 시작"));
        }
      });
  } catch (_) {
    clearTimeout(timer);
    lccOcrRequests.delete(requestId);
  }
}
function lccOcrHandleResult(msg) {
  const requestId = String(msg.request_id || "");
  const req = lccOcrRequests.get(requestId);
  if (!req) return;                              // another frame's request, or expired
  clearTimeout(req.timer);
  lccOcrRequests.delete(requestId);
  if (msg.type === "ocr_translate_err") { lccOcrChipText("OCR 실패: " + (msg.text || "")); return; }
  const blocks = Array.isArray(msg.blocks) ? msg.blocks : [];
  if (!blocks.length) { lccOcrChipText("글자를 못 찾았어요"); return; }
  lccOcrCache.set(req.img, blocks);
  if (req.img.isConnected) lccOcrShowOverlay(req.img, blocks);
  lccOcrChipHide();
}
// Detection is coordinate-based, not event-target-based: real sites cover images with links/overlay
// divs (e.target is never the IMG), and mouseover doesn't re-fire when Alt is pressed over an already-
// hovered image. So we track the pointer, and on Alt (keydown or move) walk elementsFromPoint for the
// topmost eligible IMG. The chip itself in the stack keeps the current target (so it stays clickable).
const lccOcrMouse = { x: -1, y: -1 };
let lccOcrLastCheck = 0;
function lccOcrFindImgAt(x, y) {
  if (x < 0 || y < 0) return null;
  let els;
  try { els = document.elementsFromPoint(x, y) || []; } catch (_) { return null; }
  for (const el of els.slice(0, 10)) {
    if (el === lccOcrChip) return lccOcrImg;             // hovering the chip -> keep the current image
    if (el && el.tagName === "IMG" && lccOcrEligible(el)) return el;
  }
  return null;
}
function lccOcrHoverCheck(altHeld) {
  if (!lccOcrEnabled()) return;
  if (!altHeld) {
    if (lccOcrImg && !lccOcrRequests.size) lccOcrChipHide();
    return;
  }
  const img = lccOcrFindImgAt(lccOcrMouse.x, lccOcrMouse.y);
  if (img) {
    if (img !== lccOcrImg) lccOcrShowChipFor(img);
  } else if (lccOcrImg && !lccOcrRequests.size) {
    lccOcrChipHide();
  }
}
try {
  document.addEventListener("mousemove", (e) => {
    lccOcrMouse.x = e.clientX;
    lccOcrMouse.y = e.clientY;
    if (!lccOcrEnabled()) return;
    const now = Date.now();
    if (e.altKey && now - lccOcrLastCheck < 80) return;   // throttle elementsFromPoint while Alt is held
    lccOcrLastCheck = now;
    lccOcrHoverCheck(e.altKey);
  }, { passive: true, capture: true });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && lccOcrOverlay) { lccOcrHideOverlay(); return; }
    if (e.altKey) lccOcrHoverCheck(true);                 // Alt pressed while already hovering an image
  }, true);
  window.addEventListener("keyup", (e) => { if (!e.altKey) lccOcrHoverCheck(false); }, true);
  window.addEventListener("blur", () => lccOcrHoverCheck(false));
  window.addEventListener("scroll", () => { lccOcrChipHide(); lccOcrHideOverlay(); }, { passive: true, capture: true });
} catch (_) {}
