// Service worker: single source of truth for capture state (storage.session). Routes both modes:
//  - "audio": offscreen tabCapture -> WS
//  - "video": delay.js in the captured tab (B-2)
// State {capturing, mode, capturedTabId, pendingStreamId} lives in session so it survives SW sleep.
importScripts("protocol.js");
console.log("[lcc] background service worker loaded");

function sendTab(tabId, msg) {
  if (tabId == null) return;
  try {
    const p = chrome.tabs.sendMessage(tabId, msg);
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (_) {}
}

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Capture tab audio and stream it to the local caption bridge."
  });
}

async function cleanup() {
  const { mode, capturedTabId, pageTabId } = await chrome.storage.session.get(["mode", "capturedTabId", "pageTabId"]);
  try { if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument(); } catch (_) {}
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
    delaySec: null,
    wsOpen: false,
  });
  chrome.action.setBadgeText({ text: "" });
}

// offscreen has no working chrome.storage, so the SW resolves the bridge settings and pushes them
// in the start message (and re-pushes on the offscreen-ready handshake / reconnect needs).
async function bridgeConfig() {
  const s = (await chrome.storage.local.get("lcc-settings"))["lcc-settings"] || {};
  return globalThis.lccNormalizeSettings(s);
}

// Re-deliver the start params when offscreen announces it's ready (covers the createDocument race
// where the warm start message is sent before the offscreen listener exists). Idempotent: start()/
// startRelay dedupe on streamId / relayMode.
async function resendStart() {
  const { captioning, pageTranslating, mode, pendingStreamId, pageContext, delaySec, pageTabId } = await chrome.storage.session.get(["captioning", "pageTranslating", "mode", "pendingStreamId", "pageContext", "delaySec", "pageTabId"]);
  if (!captioning && !pageTranslating) return;
  const config = await bridgeConfig();
  if (captioning && mode === "video") {
    chrome.runtime.sendMessage({ target: "offscreen", cmd: "start-relay", delaySec, pageContext: pageContext || "", config });
  } else if (captioning && mode === "audio" && pendingStreamId != null) {
    chrome.runtime.sendMessage({ target: "offscreen", cmd: "start", streamId: pendingStreamId, delaySec, pageContext: pageContext || "", config });
  }
  if (pageTranslating && pageTabId != null) {
    chrome.runtime.sendMessage({ target: "offscreen", cmd: "start-page", pageContext: pageContext || "", config });
    sendTab(pageTabId, { type: "page-translate-start", settings: config });
  }
}

const LCC_CONTENT_FILES = ["protocol.js", "pcm.js", "page-seed.js", "content.js", "delay.js"];
// Manifest declares content scripts for page-load only, so a tab that was already open when the extension
// (re)loaded has none — captions wouldn't show without a refresh. Inject on demand: ping the tab; if nothing
// answers, executeScript the bundle. Already-injected tabs answer the ping and are skipped (no double-run).
async function ensureContentScript(tabId) {
  if (tabId == null) return;
  const present = await chrome.tabs.sendMessage(tabId, { type: "lcc-ping" }).then((r) => !!(r && r.ok)).catch(() => false);
  if (present) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: LCC_CONTENT_FILES });
    await chrome.scripting.insertCSS({ target: { tabId, allFrames: true }, files: ["content.css"] });
  } catch (_) {
    try {   // cross-origin subframes may be off-limits to activeTab; the top frame still carries the overlay
      await chrome.scripting.executeScript({ target: { tabId }, files: LCC_CONTENT_FILES });
      await chrome.scripting.insertCSS({ target: { tabId }, files: ["content.css"] });
    } catch (e) { console.warn("[lcc] content inject failed for tab", tabId, e); return; }
  }
  console.log("[lcc] injected content scripts into tab", tabId);
}
async function startAudio(streamId, tabId, delaySec, pageContext) {
  await cleanup();                                  // tear down any prior capture (either mode)
  await ensureContentScript(tabId);                 // inject into already-open tabs so captions show without a reload
  const dsec = Math.min(12, Math.max(0, Number(delaySec) || 0));
  const config = await bridgeConfig();
  await chrome.storage.session.set({ capturing: true, captioning: true, mode: "audio", capturedTabId: tabId, pendingStreamId: streamId, pageContext: pageContext || "", delaySec: dsec });
  await ensureOffscreen();
  chrome.runtime.sendMessage({ target: "offscreen", cmd: "start", streamId, delaySec: dsec, pageContext: pageContext || "", config });   // warm path; offscreen-ready handshake re-delivers if missed
  chrome.action.setBadgeText({ text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  sendTab(tabId, { type: "status", on: true, mode: "audio", playbackDelayMs: Math.round(dsec * 1000) });
  console.log("[lcc] audio capture started for tab", tabId);
}

async function startVideo(tabId, delaySec, pageContext) {
  await cleanup();                                  // tear down any prior capture (esp. stale offscreen)
  await ensureContentScript(tabId);                 // inject into already-open tabs so the delayed-render path is present
  const dsec = Math.min(12, Math.max(0.5, Number(delaySec) || 3.5));
  const config = await bridgeConfig();
  // Set session state BEFORE creating the offscreen doc so resendStart() can recover params.
  await chrome.storage.session.set({ capturing: true, captioning: true, mode: "video", capturedTabId: tabId, pendingStreamId: null, pageContext: pageContext || "", delaySec: dsec });
  await ensureOffscreen();                           // offscreen owns the bridge WS (page CSP/origin can't open one)
  chrome.runtime.sendMessage({ target: "offscreen", cmd: "start-relay", delaySec: dsec, pageContext: pageContext || "", config });   // warm path; offscreen-ready handshake re-delivers if missed
  chrome.action.setBadgeText({ text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  sendTab(tabId, { type: "status", on: true, mode: "video", playbackDelayMs: Math.round(dsec * 1000) });
  sendTab(tabId, { type: "vdelay-start", delaySec: dsec, pageContext: pageContext || "" });   // delay.js: delayed A/V re-render + PCM tap -> offscreen
  console.log("[lcc] video-delay started for tab", tabId, "(offscreen relay)");
}

async function startPage(tabId, pageContext) {
  await ensureContentScript(tabId);                 // page translation needs the content script present
  const config = await bridgeConfig();
  let pageUrl = "";
  try {
    const tab = await chrome.tabs.get(tabId);
    pageUrl = (tab && tab.url) || "";
  } catch (_) {}
  await chrome.storage.session.set({ capturing: true, pageTranslating: true, pageTabId: tabId, pageContext: pageContext || "", pageUrl });
  await ensureOffscreen();
  chrome.runtime.sendMessage({ target: "offscreen", cmd: "start-page", pageContext: pageContext || "", config });
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
  if (msg.type === "wsstate") {                     // connection state isn't tab-specific -> set before the gate
    chrome.storage.session.set({ wsOpen: !!msg.open });   // (don't let the capturedTabId early-return drop it)
    chrome.action.setBadgeText({ text: msg.open ? "ON" : "…" });
  }
  const { capturedTabId, pageTabId, mode, delaySec } = await chrome.storage.session.get(["capturedTabId", "pageTabId", "mode", "delaySec"]);
  const { route: _route, ...payloadWithMaybeRouteTarget } = msg;
  const payload = payloadWithMaybeRouteTarget.target === "background"
    ? (({ target: _target, ...rest }) => rest)(payloadWithMaybeRouteTarget)
    : payloadWithMaybeRouteTarget;
  if (msg.type === "caption" && capturedTabId != null) sendTab(capturedTabId, payload);
  else if (msg.type === "caption_partial" && capturedTabId != null) sendTab(capturedTabId, payload);
  else if (msg.type === "source" && capturedTabId != null) sendTab(capturedTabId, payload);
  else if (msg.type === "stream-clock-start") {
    if (capturedTabId != null) sendTab(capturedTabId, {
      type: "stream-clock-start",
      mode: msg.mode || mode || "audio",
      playbackDelayMs: msg.playbackDelayMs ?? Math.round((Number(delaySec) || 0) * 1000),
      streamStartWall: msg.streamStartWall,
      streamStartPerf: msg.streamStartPerf,
    });
  }
  else if (msg.type === "wsstate") {
    if (capturedTabId != null) sendTab(capturedTabId, { type: "wsstate", open: msg.open });
    if (pageTabId != null) sendTab(pageTabId, { type: "page-translate-wsstate", open: msg.open });
  }
  else if (msg.type === "notice" && capturedTabId != null) sendTab(capturedTabId, { type: "notice", text: msg.text });
  else if (msg.type === "answer_partial") chrome.storage.session.set({ "lcc-answer": { text: msg.text, done: false } });   // popup reads via onChanged
  else if (msg.type === "answer") chrome.storage.session.set({ "lcc-answer": { text: msg.text, done: true } });
  else if (msg.type === "err" && capturedTabId != null) sendTab(capturedTabId, { type: "err", text: msg.text });
  else if ((msg.type === "dom_translate_result" || msg.type === "dom_translate_partial" || msg.type === "dom_translate_done" || msg.type === "dom_translate_busy" || msg.type === "dom_translate_err") && pageTabId != null) {
    sendTab(pageTabId, payload);
  }
  if (msg.type === "caption" || msg.type === "source" || msg.type === "dom_translate_result" || msg.type === "dom_translate_partial") chrome.storage.session.set({ wsOpen: true });  // data flowing => connected (self-heal)
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "vd-pcm") { chrome.runtime.sendMessage({ target: "offscreen", cmd: "pcm", pcm: msg.pcm }); return; }   // delay.js -> offscreen relay (video mode)
  if (msg.type === "offscreen-ready") { resendStart(); return; }   // offscreen loaded -> (re)deliver start params
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
      const config = await bridgeConfig();
      await chrome.storage.session.set({ pageContext: nextContext, pageUrl: nextUrl });
      await ensureOffscreen();
      chrome.runtime.sendMessage({ target: "offscreen", cmd: "start-page", pageContext: nextContext, config });
      sendResponse({ ok: true, pageTranslating: true, settings: config });
    }).catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;
  }
  if (msg.type === "popup-cleanup") { cleanup().then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === "popup-config-update") {
    chrome.storage.session.get(["captioning", "pageTranslating", "pageTabId"]).then(({ captioning, pageTranslating, pageTabId }) => {
      if (!captioning && !pageTranslating) return;
      bridgeConfig().then((config) => {
        chrome.runtime.sendMessage({ target: "offscreen", cmd: "config", config });
        if (pageTranslating && pageTabId != null) sendTab(pageTabId, { type: "page-translate-config", settings: config });
        if (msg.resetTranslationContext) resetTranslationContext();
      });
    });
    return;
  }
  if (msg.type === "popup-clear-transcript") { clearTranscript(msg.tabId).then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === "popup-start") {
    startAudio(msg.streamId, msg.tabId, msg.delaySec, msg.pageContext)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => { console.error("[lcc] startAudio", e); sendResponse({ ok: false, error: String(e && e.message || e) }); });
    return true;
  }
  if (msg.type === "popup-start-video") {
    startVideo(msg.tabId, msg.delaySec, msg.pageContext)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => { console.error("[lcc] startVideo", e); sendResponse({ ok: false, error: String(e && e.message || e) }); });
    return true;
  }
  if (msg.type === "popup-start-page") {
    startPage(msg.tabId, msg.pageContext)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => { console.error("[lcc] startPage", e); sendResponse({ ok: false, error: String(e && e.message || e) }); });
    return true;
  }
  if (msg.type === "popup-stop") { cleanup(); return; }
  if (msg.type === "lcc-ask") { chrome.runtime.sendMessage({ target: "offscreen", cmd: "ask", mode: msg.mode, transcript: msg.transcript, question: msg.question }); return; }
  if (msg.type === "page-translate-batch") {
    const tabId = sender && sender.tab && sender.tab.id;
    ensureOffscreen()
      .then(() => chrome.runtime.sendMessage({ target: "offscreen", cmd: "dom-translate-batch", tabId, requestId: msg.requestId, items: msg.items || [] }))
      .catch((e) => console.error("[lcc] page batch route", e));
    return;
  }
  if (msg.route === "background" || msg.target === "background") {
    forward(msg)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;
  }
});
