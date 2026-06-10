// Service worker: single source of truth for capture state (storage.session). Routes both modes:
//  - "audio": offscreen tabCapture -> WS
//  - "video": delay.js in the captured tab (B-2)
// State {capturing, mode, capturedTabId, pendingStreamId} lives in session so it survives SW sleep.
importScripts("protocol.js");
importScripts("term-memory.js");
console.log("[lcc] background service worker loaded");

const LCC_OFFSCREEN_WARN_INTERVAL_MS = 2000;
const LCC_TAB_WARN_INTERVAL_MS = 2000;
let lccLastOffscreenWarnAt = 0;
let lccLastTabWarnAt = 0;

function lccErrorText(e) {
  return String(e && e.message || e || "unknown error");
}

function respondError(sendResponse, e) {
  sendResponse({ ok: false, error: lccErrorText(e) });
}

function warnOffscreenDelivery(label, e) {
  const now = Date.now();
  if (now - lccLastOffscreenWarnAt < LCC_OFFSCREEN_WARN_INTERVAL_MS) return;
  lccLastOffscreenWarnAt = now;
  console.warn("[lcc] offscreen delivery failed:", label, lccErrorText(e));
}

function sendOffscreenBestEffort(msg, label) {
  try {
    const p = chrome.runtime.sendMessage(msg);
    if (p && typeof p.then === "function") {
      p
        .then((res) => { if (res && res.ok === false) warnOffscreenDelivery(label, res.error || res.msg || "not ok"); })
        .catch((e) => warnOffscreenDelivery(label, e));
    }
  } catch (e) {
    warnOffscreenDelivery(label, e);
  }
}

function sendTab(tabId, msg) {
  if (tabId == null) return;
  const label = msg && msg.type || "message";
  try {
    const p = chrome.tabs.sendMessage(tabId, msg);
    if (p && typeof p.then === "function") {
      p
        .then((res) => { if (res && res.ok === false) warnTabDelivery(tabId, label, res.error || res.msg || "not ok"); })
        .catch((e) => warnTabDelivery(tabId, label, e));
    }
  } catch (e) {
    warnTabDelivery(tabId, label, e);
  }
}

function warnTabDelivery(tabId, label, e) {
  const now = Date.now();
  if (now - lccLastTabWarnAt < LCC_TAB_WARN_INTERVAL_MS) return;
  lccLastTabWarnAt = now;
  console.warn("[lcc] tab delivery failed:", tabId, label, lccErrorText(e));
}

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Capture tab audio and stream it to the local caption bridge."
  });
}

async function closeOffscreenIfPresent() {
  if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument();
}

async function cleanup() {
  const { mode, capturedTabId, pageTabId } = await chrome.storage.session.get(["mode", "capturedTabId", "pageTabId"]);
  await closeOffscreenIfPresent();
  if (capturedTabId != null) {
    if (mode === "video") sendTab(capturedTabId, { type: "vdelay-stop" });   // delay.js: tear down A/V tap+render
    sendTab(capturedTabId, { type: "status", on: false });                   // content.js: hide the caption overlay (both modes)
  }
  if (pageTabId != null) sendTab(pageTabId, { type: "page-translate-stop" });
  await chrome.storage.session.set({
    capturing: false,
    captioning: false,
    pageTranslating: false,
    mode: null,
    capturedTabId: null,
    pageTabId: null,
    pendingStreamId: null,
    pageContext: null,
    pageUrl: null,
    captureUrl: null,
    delaySec: null,
    wsOpen: false,
    lccStreamClock: null,
  });
  chrome.action.setBadgeText({ text: "" });
}

// offscreen has no working chrome.storage, so the SW resolves the bridge settings and pushes them
// in the start message (and re-pushes on the offscreen-ready handshake / reconnect needs).
// Callers must set captureUrl/pageUrl in session BEFORE building the config — the domain term seeds
// (tab memory) are resolved from them here.
async function bridgeConfig() {
  const s = (await chrome.storage.local.get("lcc-settings"))["lcc-settings"] || {};
  const config = globalThis.lccNormalizeSettings(s);
  if (config.termMemory === true) {
    try {   // seeds are an enhancement — never let them block a config push
      const { captureUrl, pageUrl } = await chrome.storage.session.get(["captureUrl", "pageUrl"]);
      config.autoGlossary = await globalThis.lccTermMemorySeeds([captureUrl, pageUrl]);
    } catch (e) {
      console.warn("[lcc] term-memory seed lookup failed:", lccErrorText(e));
    }
  }
  return config;
}

async function tabUrl(tabId) {
  try {
    return String((await chrome.tabs.get(tabId)).url || "");
  } catch (e) {
    console.warn("[lcc] tab url lookup failed:", tabId, lccErrorText(e));
    return "";
  }
}

// Re-deliver the start params when offscreen announces it's ready (covers the createDocument race
// where the warm start message is sent before the offscreen listener exists). Idempotent: start()/
// startRelay dedupe on streamId / relayMode.
async function resendStart() {
  const { captioning, pageTranslating, mode, pendingStreamId, pageContext, delaySec, pageTabId } = await chrome.storage.session.get(["captioning", "pageTranslating", "mode", "pendingStreamId", "pageContext", "delaySec", "pageTabId"]);
  if (!captioning && !pageTranslating) return;
  const config = await bridgeConfig();
  if (captioning && mode === "video") {
    sendOffscreenBestEffort({ target: "offscreen", cmd: "start-relay", delaySec, pageContext: pageContext || "", config }, "start-relay");
  } else if (captioning && mode === "audio" && pendingStreamId != null) {
    sendOffscreenBestEffort({ target: "offscreen", cmd: "start", streamId: pendingStreamId, delaySec, pageContext: pageContext || "", config }, "start");
  }
  if (pageTranslating && pageTabId != null) {
    sendOffscreenBestEffort({ target: "offscreen", cmd: "start-page", pageContext: pageContext || "", config }, "start-page");
    sendTab(pageTabId, { type: "page-translate-start", settings: config });
  }
}

const LCC_CONTENT_FILES = ["protocol.js", "pcm.js", "page-seed.js", "content.js", "delay.js"];
function unsupportedTabReason(tab) {
  const url = String(tab && tab.url || "");
  const scheme = (url.match(/^([a-z][a-z0-9+.-]*):/i) || [])[1];
  if (!scheme) return "";
  if (scheme === "http" || scheme === "https" || scheme === "file") return "";
  return `이 탭(${scheme}://)에는 확장 스크립트를 주입할 수 없어요. 일반 웹 페이지에서 다시 시작하세요.`;
}
async function injectContentFiles(target) {
  await chrome.scripting.executeScript({ target, files: LCC_CONTENT_FILES });
  await chrome.scripting.insertCSS({ target, files: ["content.css"] });
}
function contentScriptFailure(tabId, tab, e) {
  const where = tab && tab.url ? ` (${tab.url})` : "";
  console.warn("[lcc] content inject failed for tab", tabId, where, e);
  const error = `이 탭에는 확장 스크립트를 주입할 수 없어요. 일반 웹 페이지에서 다시 시작하세요.${where ? ` URL: ${tab.url}` : ""}`;
  return { ok: false, error };
}
// Manifest declares content scripts for page-load only, so a tab that was already open when the extension
// (re)loaded has none — captions wouldn't show without a refresh. Inject on demand: ping the tab; if nothing
// answers, executeScript the bundle. Already-injected tabs answer the ping and are skipped (no double-run).
async function ensureContentScript(tabId) {
  if (tabId == null) return { ok: false };
  const tab = await chrome.tabs.get(tabId).catch((e) => {
    console.warn("[lcc] content tab lookup failed:", tabId, lccErrorText(e));
    return null;
  });
  const unsupported = unsupportedTabReason(tab);
  if (unsupported) {
    console.warn("[lcc] unsupported content tab:", tabId, tab && tab.url);
    return { ok: false, error: unsupported };
  }
  const present = await chrome.tabs.sendMessage(tabId, { type: "lcc-ping" }).then((r) => !!(r && r.ok)).catch(() => false);
  if (present) return { ok: true };
  try {
    await injectContentFiles({ tabId, allFrames: true });
    console.log("[lcc] injected content scripts into tab", tabId);
    return { ok: true };
  } catch (_) {
    try {   // cross-origin subframes may be off-limits to activeTab; the top frame still carries the overlay
      await injectContentFiles({ tabId });
      console.log("[lcc] injected content scripts into tab", tabId);
      return { ok: true };
    } catch (e) {
      return contentScriptFailure(tabId, tab, e);
    }
  }
}
function requireContentScript(result) {
  if (!result || result === false || result.ok === false) {
    throw new Error(result && result.error || "이 탭에는 확장 스크립트를 주입할 수 없어요. 일반 웹 페이지에서 다시 시작하세요.");
  }
}
async function startAudio(streamId, tabId, delaySec, pageContext) {
  await cleanup();                                  // tear down any prior capture (either mode)
  requireContentScript(await ensureContentScript(tabId));  // inject into already-open tabs so captions show without a reload
  const dsec = Math.min(12, Math.max(0, Number(delaySec) || 0));
  const captureUrl = await tabUrl(tabId);           // set BEFORE bridgeConfig so domain term seeds resolve
  await chrome.storage.session.set({ capturing: true, captioning: true, mode: "audio", capturedTabId: tabId, pendingStreamId: streamId, pageContext: pageContext || "", delaySec: dsec, captureUrl });
  const config = await bridgeConfig();
  await ensureOffscreen();
  sendOffscreenBestEffort({ target: "offscreen", cmd: "start", streamId, delaySec: dsec, pageContext: pageContext || "", config }, "start");   // warm path; offscreen-ready handshake re-delivers if missed
  chrome.action.setBadgeText({ text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  sendTab(tabId, { type: "status", on: true, mode: "audio", playbackDelayMs: Math.round(dsec * 1000) });
  console.log("[lcc] audio capture started for tab", tabId);
}

async function startVideo(tabId, delaySec, pageContext) {
  await cleanup();                                  // tear down any prior capture (esp. stale offscreen)
  requireContentScript(await ensureContentScript(tabId));  // inject into already-open tabs so the delayed-render path is present
  const dsec = Math.min(12, Math.max(0.5, Number(delaySec) || 3.5));
  const captureUrl = await tabUrl(tabId);           // set BEFORE bridgeConfig so domain term seeds resolve
  // Set session state BEFORE creating the offscreen doc so resendStart() can recover params.
  await chrome.storage.session.set({ capturing: true, captioning: true, mode: "video", capturedTabId: tabId, pendingStreamId: null, pageContext: pageContext || "", delaySec: dsec, captureUrl });
  const config = await bridgeConfig();
  await ensureOffscreen();                           // offscreen owns the bridge WS (page CSP/origin can't open one)
  sendOffscreenBestEffort({ target: "offscreen", cmd: "start-relay", delaySec: dsec, pageContext: pageContext || "", config }, "start-relay");   // warm path; offscreen-ready handshake re-delivers if missed
  chrome.action.setBadgeText({ text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  sendTab(tabId, { type: "status", on: true, mode: "video", playbackDelayMs: Math.round(dsec * 1000) });
  sendTab(tabId, { type: "vdelay-start", delaySec: dsec, pageContext: pageContext || "" });   // delay.js: delayed A/V re-render + PCM tap -> offscreen
  console.log("[lcc] video-delay started for tab", tabId, "(offscreen relay)");
}

async function startPage(tabId, pageContext) {
  requireContentScript(await ensureContentScript(tabId));  // page translation needs the content script present
  const pageUrl = await tabUrl(tabId);
  await chrome.storage.session.set({ capturing: true, pageTranslating: true, pageTabId: tabId, pageContext: pageContext || "", pageUrl });
  const config = await bridgeConfig();              // AFTER session.set so domain term seeds resolve
  await ensureOffscreen();
  sendOffscreenBestEffort({ target: "offscreen", cmd: "start-page", pageContext: pageContext || "", config }, "start-page");
  sendTab(tabId, { type: "page-translate-start", settings: config });
  chrome.action.setBadgeText({ text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  console.log("[lcc] page translation started for tab", tabId);
}

async function clearTranscript(tabId) {
  await chrome.storage.local.remove(["lcc-transcript", "lcc-session"]);
  await chrome.storage.session.remove("lcc-answer");
  const { capturedTabId } = await chrome.storage.session.get("capturedTabId");
  const targets = [...new Set([capturedTabId, tabId].filter((id) => id != null))];
  for (const id of targets) {
    sendTab(id, { type: "transcript-clear" });
  }
}

async function resetTranslationContext() {
  const { capturedTabId } = await chrome.storage.session.get("capturedTabId");
  if (capturedTabId != null) sendTab(capturedTabId, { type: "translation-context-reset" });
}

async function forward(msg) {
  if (msg.type === "capture-failed") { await cleanup(); return; }   // offscreen couldn't set up capture -> roll back the optimistic capturing/badge state
  if (msg.type === "wsstate") {                     // connection state isn't tab-specific -> set before the gate
    await chrome.storage.session.set({ wsOpen: !!msg.open });   // (don't let the capturedTabId early-return drop it)
    chrome.action.setBadgeText({ text: msg.open ? "ON" : "…" });
  }
  const { capturedTabId, pageTabId, mode, delaySec, captureUrl, pageUrl } = await chrome.storage.session.get(["capturedTabId", "pageTabId", "mode", "delaySec", "captureUrl", "pageUrl"]);
  const { route: _route, ...payloadWithMaybeRouteTarget } = msg;
  const payload = payloadWithMaybeRouteTarget.target === "background"
    ? (({ target: _target, ...rest }) => rest)(payloadWithMaybeRouteTarget)
    : payloadWithMaybeRouteTarget;
  if (msg.type === "caption" && capturedTabId != null) sendTab(capturedTabId, payload);
  else if (msg.type === "caption_partial" && capturedTabId != null) sendTab(capturedTabId, payload);
  else if (msg.type === "source" && capturedTabId != null) sendTab(capturedTabId, payload);
  else if (msg.type === "stream-clock-start") {
    const clock = {
      mode: msg.mode || mode || "audio",
      playbackDelayMs: msg.playbackDelayMs ?? Math.round((Number(delaySec) || 0) * 1000),
      streamStartWall: msg.streamStartWall,
      streamStartPerf: msg.streamStartPerf,
    };
    await chrome.storage.session.set({ lccStreamClock: clock });   // cache so a navigated audio tab can re-anchor to the still-running stream
    if (capturedTabId != null) sendTab(capturedTabId, { type: "stream-clock-start", ...clock });
  }
  else if (msg.type === "wsstate") {
    if (capturedTabId != null) sendTab(capturedTabId, { type: "wsstate", open: msg.open });
    if (pageTabId != null) sendTab(pageTabId, { type: "page-translate-wsstate", open: msg.open });
  }
  else if (msg.type === "notice" && capturedTabId != null) sendTab(capturedTabId, { type: "notice", text: msg.text });
  else if (msg.type === "term_memory") await globalThis.lccTermMemorySave(msg.terms, captureUrl || pageUrl);   // tab memory: persist mined terms per domain
  else if (msg.type === "answer_partial") await chrome.storage.session.set({ "lcc-answer": { text: msg.text, done: false } });   // popup reads via onChanged
  else if (msg.type === "answer") await chrome.storage.session.set({ "lcc-answer": { text: msg.text, done: true } });
  else if (msg.type === "err" && capturedTabId != null) sendTab(capturedTabId, { type: "err", text: msg.text });
  else if ((msg.type === "dom_translate_result" || msg.type === "dom_translate_partial" || msg.type === "dom_translate_done" || msg.type === "dom_translate_busy" || msg.type === "dom_translate_err" || msg.type === "input_translate_result" || msg.type === "input_translate_err") && pageTabId != null) {
    sendTab(pageTabId, payload);
  }
  if (msg.type === "caption" || msg.type === "source" || msg.type === "dom_translate_result" || msg.type === "dom_translate_partial") await chrome.storage.session.set({ wsOpen: true });  // data flowing => connected (self-heal)
}

// Message types that legitimately originate from a tab (content scripts: delay.js / content.js). Every other
// type must come from the popup or the offscreen document, neither of which has a sender.tab. The content
// bundle runs in EVERY page, so without this gate a compromised content script could drive the privileged
// popup/ask/forward command surface (start/stop captures, ask the bridge, spoof captions) for arbitrary tabs.
const LCC_TAB_SENDER_TYPES = new Set(["vd-pcm", "content-ready", "page-translate-batch", "input-translate"]);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return;
  if (sender && sender.id && sender.id !== chrome.runtime.id) return;             // not our extension -> ignore
  if (sender && sender.tab && !LCC_TAB_SENDER_TYPES.has(msg.type)) return;        // a tab/content script may not issue popup/forward commands
  if (msg.type === "vd-pcm") { sendOffscreenBestEffort({ target: "offscreen", cmd: "pcm", pcm: msg.pcm }, "vd-pcm"); return; }   // delay.js -> offscreen relay (video mode)
  if (msg.type === "offscreen-ready") {
    resendStart().catch((e) => console.warn("[lcc] offscreen-ready resend failed:", lccErrorText(e)));
    return;
  }   // offscreen loaded -> (re)deliver start params
  if (msg.type === "popup-status") {
    chrome.storage.session.get(["captioning", "pageTranslating", "capturing", "wsOpen"]).then(({ captioning, pageTranslating, capturing, wsOpen }) => {
      const active = !!captioning || !!pageTranslating || !!capturing;
      sendResponse({ capturing: active, captioning: !!captioning, pageTranslating: !!pageTranslating, wsOpen: !!wsOpen });
    });
    return true;
  }
  if (msg.type === "content-ready") {
    const tabId = sender && sender.tab && sender.tab.id;
    chrome.storage.session.get(["pageTranslating", "pageTabId", "pageContext"]).then(async ({ pageTranslating, pageTabId, pageContext }) => {
      const shouldResumePage = !!pageTranslating && tabId != null && pageTabId === tabId;
      if (!shouldResumePage) {
        sendResponse({ ok: true, pageTranslating: false, settings: null });
        return;
      }
      const nextContext = String(msg.pageContext || pageContext || "").trim().slice(0, 200);
      const nextUrl = String(msg.pageUrl || (sender && sender.tab && sender.tab.url) || "").slice(0, 500);
      await chrome.storage.session.set({ pageContext: nextContext, pageUrl: nextUrl });
      const config = await bridgeConfig();          // AFTER session.set so domain term seeds resolve
      await ensureOffscreen();
      sendOffscreenBestEffort({ target: "offscreen", cmd: "start-page", pageContext: nextContext, config }, "start-page");
      sendResponse({ ok: true, pageTranslating: true, settings: config });
    }).catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.type === "popup-cleanup") {
    cleanup()
      .then(() => sendResponse({ ok: true }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.type === "popup-config-update") {
    chrome.storage.session.get(["captioning", "pageTranslating", "pageTabId"]).then(async ({ captioning, pageTranslating, pageTabId }) => {
      if (!captioning && !pageTranslating) return { ok: true, applied: false };
      const config = await bridgeConfig();
      await ensureOffscreen();
      const pushed = await chrome.runtime.sendMessage({ target: "offscreen", cmd: "config", config });
      if (pushed && pushed.ok === false) return pushed;
      if (pageTranslating && pageTabId != null) sendTab(pageTabId, { type: "page-translate-config", settings: config });
      if (msg.resetTranslationContext) await resetTranslationContext();
      return { ok: true, applied: true };
    })
      .then((res) => sendResponse(res || { ok: true }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.type === "popup-clear-transcript") {
    clearTranscript(msg.tabId)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.type === "popup-start") {
    startAudio(msg.streamId, msg.tabId, msg.delaySec, msg.pageContext)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => { console.error("[lcc] startAudio", e); respondError(sendResponse, e); });
    return true;
  }
  if (msg.type === "popup-start-video") {
    startVideo(msg.tabId, msg.delaySec, msg.pageContext)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => { console.error("[lcc] startVideo", e); respondError(sendResponse, e); });
    return true;
  }
  if (msg.type === "popup-start-page") {
    startPage(msg.tabId, msg.pageContext)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => { console.error("[lcc] startPage", e); respondError(sendResponse, e); });
    return true;
  }
  if (msg.type === "popup-stop") {
    cleanup()
      .then(() => sendResponse({ ok: true }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.type === "lcc-ask") {
    chrome.runtime.sendMessage({ target: "offscreen", cmd: "ask", mode: msg.mode, transcript: msg.transcript, question: msg.question })
      .then((res) => sendResponse(res && res.ok === false ? res : { ok: true }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.type === "input-translate") {
    const tabId = sender && sender.tab && sender.tab.id;
    chrome.storage.session.get(["pageTranslating", "pageTabId"])
      .then(({ pageTranslating, pageTabId }) => {
        if (!pageTranslating || tabId == null || pageTabId !== tabId) return { ok: false, error: "페이지 번역이 켜진 탭에서만 됩니다" };
        return ensureOffscreen()
          .then(() => chrome.runtime.sendMessage({ target: "offscreen", cmd: "input-translate", requestId: msg.requestId, text: msg.text || "", targetLang: msg.targetLang || "" }))
          .then((res) => (res && res.ok === false) ? res : { ok: true });
      })
      .then((res) => sendResponse(res || { ok: false }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.type === "page-translate-batch") {
    const tabId = sender && sender.tab && sender.tab.id;
    chrome.storage.session.get(["pageTranslating", "pageTabId"])
      .then(({ pageTranslating, pageTabId }) => {
        if (!pageTranslating || tabId == null || pageTabId !== tabId) return { ok: true, routed: false };
        return ensureOffscreen()
          .then(() => chrome.runtime.sendMessage({ target: "offscreen", cmd: "dom-translate-batch", tabId, requestId: msg.requestId, items: msg.items || [], verify: msg.verify === true }))
          .then((res) => (res && res.ok === false) ? res : { ok: true, routed: true });
      })
      .then((res) => sendResponse(res || { ok: true, routed: false }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
  if (msg.route === "background" || msg.target === "background") {
    forward(msg)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => respondError(sendResponse, e));
    return true;
  }
});

// A captured / page-translated tab closing (or being replaced by, e.g., a discard) must tear capture
// down. content.js's pagehide only cleans the page side and never notifies the SW, so without this the
// offscreen doc + bridge WS leak and the badge stays "ON" until the next start.
async function onTabGone(tabId) {
  if (tabId == null) return;
  const { capturedTabId, pageTabId, captioning } = await chrome.storage.session.get(["capturedTabId", "pageTabId", "captioning"]);
  if (capturedTabId === tabId) { await cleanup(); return; }   // capture tab gone -> full teardown (both features share the offscreen WS via capturedTabId)
  if (pageTabId === tabId) {
    // page-translate tab gone; a capture may still be live on another tab -> clear only the page side
    await chrome.storage.session.set({ pageTranslating: false, pageTabId: null, pageContext: null, pageUrl: null });
    if (!captioning) {                                         // nothing else holds the offscreen WS -> close it too
      await closeOffscreenIfPresent();
      await chrome.storage.session.set({ capturing: false, wsOpen: false });
      chrome.action.setBadgeText({ text: "" });
    }
  }
}
function handleTabGone(tabId) {
  onTabGone(tabId).catch((e) => console.warn("[lcc] tab-gone cleanup failed:", tabId, lccErrorText(e)));
}
chrome.tabs.onRemoved.addListener((tabId) => { handleTabGone(tabId); });
chrome.tabs.onReplaced.addListener((addedTabId, removedTabId) => { handleTabGone(removedTabId); });

// When the captured tab loads a NEW document (navigation or reload), the page-side state set up at start
// is gone and nothing re-arms it — captions silently stop. Re-arm on the new page. (A bridge restart does
// NOT need this: the offscreen WS auto-reconnects and, in video mode, delay.js runs page-side — so this
// only handles real navigation, which onUpdated reports and the initial start, on an already-loaded page,
// does not.)
async function reArmCapturedTab(tabId) {
  if (tabId == null) return;
  const { captioning, mode, capturedTabId, pageContext, delaySec, lccStreamClock } =
    await chrome.storage.session.get(["captioning", "mode", "capturedTabId", "pageContext", "delaySec", "lccStreamClock"]);
  if (!captioning || capturedTabId !== tabId) return;
  const ready = await ensureContentScript(tabId);          // the new page may need the bundle injected first
  if (!ready || ready.ok === false) return;                // not an injectable page (e.g. chrome://) -> nothing to re-arm
  const dsec = Math.min(12, Math.max(0, Number(delaySec) || 0));
  if (mode === "video") {
    // Video delay lives in the page's delay.js (audio DelayNode + canvas), gone when the old page unloads.
    // Reset the relay so a fresh start-relay isn't deduped and the bridge's audio_ms restarts at 0, which
    // is what the re-armed delay.js anchors its subtitle clock to (first PCM tap == audio_ms 0).
    const config = await bridgeConfig();
    await ensureOffscreen();
    sendOffscreenBestEffort({ target: "offscreen", cmd: "stop" }, "stop");
    sendOffscreenBestEffort({ target: "offscreen", cmd: "start-relay", delaySec: dsec, pageContext: pageContext || "", config }, "start-relay");
    sendTab(tabId, { type: "status", on: true, mode: "video", playbackDelayMs: Math.round(dsec * 1000) });
    sendTab(tabId, { type: "vdelay-start", delaySec: dsec, pageContext: pageContext || "" });
    console.log("[lcc] re-armed video delay after navigation for tab", tabId);
    return;
  }
  // Audio mode: the tab-capture stream lives in the offscreen doc and survives same-tab navigation, so
  // PCM (and captions) keep flowing — only the fresh page lost the overlay + caption clock. Re-show the
  // overlay and re-send the cached clock so captions re-anchor to the still-running stream. Do NOT reset
  // the relay: that would drop the capture, which can't be re-acquired without a user gesture. (If the
  // stream actually ended on navigation, offscreen's track-ended -> capture-failed tears it down instead.)
  sendTab(tabId, { type: "status", on: true, mode: "audio", playbackDelayMs: Math.round(dsec * 1000) });
  if (lccStreamClock && lccStreamClock.streamStartWall != null) {
    sendTab(tabId, { type: "stream-clock-start", ...lccStreamClock });
  }
  console.log("[lcc] re-armed audio overlay after navigation for tab", tabId);
}
// Debounce per tab — a single load emits several onUpdated events. status:complete needs no "tabs"
// permission. In-page SPA swaps that don't reload the document (e.g. clicking the next YouTube video
// when the page reuses its <video>) aren't covered here.
const lccReArmTimers = new Map();
function scheduleReArm(tabId) {
  if (lccReArmTimers.has(tabId)) clearTimeout(lccReArmTimers.get(tabId));
  lccReArmTimers.set(tabId, setTimeout(() => {
    lccReArmTimers.delete(tabId);
    reArmCapturedTab(tabId).catch((e) => console.warn("[lcc] tab re-arm failed:", tabId, lccErrorText(e)));
  }, 500));
}
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "complete") scheduleReArm(tabId);
});
