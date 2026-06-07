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
  const { mode, capturedTabId } = await chrome.storage.session.get(["mode", "capturedTabId"]);
  try { if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument(); } catch (_) {}
  if (capturedTabId != null) {
    if (mode === "video") sendTab(capturedTabId, { type: "vdelay-stop" });   // delay.js: tear down A/V tap+render
    sendTab(capturedTabId, { type: "status", on: false });                   // content.js: hide the caption overlay (both modes)
  }
  await chrome.storage.session.set({ capturing: false, mode: null, capturedTabId: null, pendingStreamId: null, pageContext: null, delaySec: null, wsOpen: false });
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
  const { capturing, mode, pendingStreamId, pageContext, delaySec } = await chrome.storage.session.get(["capturing", "mode", "pendingStreamId", "pageContext", "delaySec"]);
  if (!capturing) return;
  const config = await bridgeConfig();
  if (mode === "video") {
    chrome.runtime.sendMessage({ target: "offscreen", cmd: "start-relay", delaySec, pageContext: pageContext || "", config });
  } else if (mode === "audio" && pendingStreamId != null) {
    chrome.runtime.sendMessage({ target: "offscreen", cmd: "start", streamId: pendingStreamId, delaySec, pageContext: pageContext || "", config });
  }
}

async function startAudio(streamId, tabId, delaySec, pageContext) {
  await cleanup();                                  // tear down any prior capture (either mode)
  const dsec = Math.min(12, Math.max(0, Number(delaySec) || 0));
  const config = await bridgeConfig();
  await chrome.storage.session.set({ capturing: true, mode: "audio", capturedTabId: tabId, pendingStreamId: streamId, pageContext: pageContext || "", delaySec: dsec });
  await ensureOffscreen();
  chrome.runtime.sendMessage({ target: "offscreen", cmd: "start", streamId, delaySec: dsec, pageContext: pageContext || "", config });   // warm path; offscreen-ready handshake re-delivers if missed
  chrome.action.setBadgeText({ text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  sendTab(tabId, { type: "status", on: true, mode: "audio", playbackDelayMs: Math.round(dsec * 1000) });
  console.log("[lcc] audio capture started for tab", tabId);
}

async function startVideo(tabId, delaySec, pageContext) {
  await cleanup();                                  // tear down any prior capture (esp. stale offscreen)
  const dsec = Math.min(12, Math.max(0.5, Number(delaySec) || 3.5));
  const config = await bridgeConfig();
  // Set session state BEFORE creating the offscreen doc so resendStart() can recover params.
  await chrome.storage.session.set({ capturing: true, mode: "video", capturedTabId: tabId, pendingStreamId: null, pageContext: pageContext || "", delaySec: dsec });
  await ensureOffscreen();                           // offscreen owns the bridge WS (page CSP/origin can't open one)
  chrome.runtime.sendMessage({ target: "offscreen", cmd: "start-relay", delaySec: dsec, pageContext: pageContext || "", config });   // warm path; offscreen-ready handshake re-delivers if missed
  chrome.action.setBadgeText({ text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
  sendTab(tabId, { type: "status", on: true, mode: "video", playbackDelayMs: Math.round(dsec * 1000) });
  sendTab(tabId, { type: "vdelay-start", delaySec: dsec, pageContext: pageContext || "" });   // delay.js: delayed A/V re-render + PCM tap -> offscreen
  console.log("[lcc] video-delay started for tab", tabId, "(offscreen relay)");
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
  const { capturedTabId, mode, delaySec } = await chrome.storage.session.get(["capturedTabId", "mode", "delaySec"]);
  if (capturedTabId == null) return;
  const { target: _target, ...payload } = msg;
  if (msg.type === "caption") sendTab(capturedTabId, payload);
  else if (msg.type === "caption_partial") sendTab(capturedTabId, payload);
  else if (msg.type === "source") sendTab(capturedTabId, payload);
  else if (msg.type === "stream-clock-start") {
    sendTab(capturedTabId, {
      type: "stream-clock-start",
      mode: msg.mode || mode || "audio",
      playbackDelayMs: msg.playbackDelayMs ?? Math.round((Number(delaySec) || 0) * 1000),
      streamStartWall: msg.streamStartWall,
      streamStartPerf: msg.streamStartPerf,
    });
  }
  else if (msg.type === "wsstate") sendTab(capturedTabId, { type: "wsstate", open: msg.open });
  else if (msg.type === "notice") sendTab(capturedTabId, { type: "notice", text: msg.text });
  else if (msg.type === "answer_partial") chrome.storage.session.set({ "lcc-answer": { text: msg.text, done: false } });   // popup reads via onChanged
  else if (msg.type === "answer") chrome.storage.session.set({ "lcc-answer": { text: msg.text, done: true } });
  else if (msg.type === "err") sendTab(capturedTabId, { type: "err", text: msg.text });
  if (msg.type === "caption" || msg.type === "source") chrome.storage.session.set({ wsOpen: true });  // data flowing => connected (self-heal)
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "vd-pcm") { chrome.runtime.sendMessage({ target: "offscreen", cmd: "pcm", pcm: msg.pcm }); return; }   // delay.js -> offscreen relay (video mode)
  if (msg.type === "offscreen-ready") { resendStart(); return; }   // offscreen loaded -> (re)deliver start params
  if (msg.type === "popup-status") {
    chrome.storage.session.get(["capturing", "wsOpen"]).then(({ capturing, wsOpen }) => sendResponse({ capturing: !!capturing, wsOpen: !!wsOpen }));
    return true;
  }
  if (msg.type === "popup-cleanup") { cleanup().then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === "popup-config-update") {
    chrome.storage.session.get("capturing").then(({ capturing }) => {
      if (!capturing) return;
      bridgeConfig().then((config) => {
        chrome.runtime.sendMessage({ target: "offscreen", cmd: "config", config });
        if (msg.resetTranslationContext) resetTranslationContext();
      });
    });
    return;
  }
  if (msg.type === "popup-clear-transcript") { clearTranscript(msg.tabId).then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === "popup-start") { startAudio(msg.streamId, msg.tabId, msg.delaySec, msg.pageContext).catch((e) => console.error("[lcc] startAudio", e)); return; }
  if (msg.type === "popup-start-video") { startVideo(msg.tabId, msg.delaySec, msg.pageContext).catch((e) => console.error("[lcc] startVideo", e)); return; }
  if (msg.type === "popup-stop") { cleanup(); return; }
  if (msg.type === "lcc-ask") { chrome.runtime.sendMessage({ target: "offscreen", cmd: "ask", mode: msg.mode, transcript: msg.transcript, question: msg.question }); return; }
  if (msg.target === "background") { forward(msg); return; }
});
