// Offscreen doc: capture tab audio -> 16k PCM16 -> bridge; relay captions. (normal/audio mode)
let ws = null, audioCtx = null, node = null, stream = null, currentId = null;
let active = false, reconnectTimer = null, backoff = 0;   // auto-reconnect state (bridge restart/drop)
let wsConfigured = false;
let relayMode = false;                                    // video mode: page taps PCM, we just relay it
let relayReconnect = false;                               // video mode: a drop happened -> offscreen re-anchors the clock (delay.js owns the initial anchor)
let pageActive = false;                                    // page mode: DOM text batches -> bridge -> direct page replacements
let currentConfig = {};                                   // bridge settings, pushed from background (offscreen has no chrome.storage)
let currentPageContext = "";
let bufferedPcm = [], bufferedBytes = 0, droppedPcmMs = 0;
let domBatchQueue = [], domBatchBytes = 0;
let currentDelaySec = 0, streamClockWall = 0, streamClockSent = false;
const PCM_RATE = 16000;
const PCM_BUFFER_BYTES = PCM_RATE * 2 * 6;                // keep up to 6s while the bridge restarts
const WS_BACKPRESSURE_BYTES = PCM_BUFFER_BYTES * 2;         // browser WebSocket send buffer cap
const DOM_BATCH_QUEUE_BYTES = 128 * 1024;                  // bound page translation backlog if the bridge restarts

function report(text) {
  console.log("[lcc-offscreen]", text);
  chrome.runtime.sendMessage({ route: "background", type: "err", text });
}
function notice(text) {
  console.log("[lcc-offscreen]", text);
  chrome.runtime.sendMessage({ route: "background", type: "notice", text });
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.target !== "offscreen") return;
  if (msg.cmd === "start") start(msg.streamId, msg.pageContext || "", msg.delaySec, msg.config).catch((e) => report("start 실패: " + (e && e.message || e)));
  else if (msg.cmd === "start-relay") startRelay(msg.pageContext || "", msg.delaySec, msg.config).catch((e) => report("relay 시작 실패: " + (e && e.message || e)));
  else if (msg.cmd === "start-page") startPage(msg.pageContext || "", msg.config).catch((e) => report("페이지 번역 시작 실패: " + (e && e.message || e)));
  else if (msg.cmd === "config") {
    currentConfig = msg.config || {};
    sendBridgeConfig();
  }
  else if (msg.cmd === "pcm") { if (relayMode && msg.pcm && msg.pcm.length) queueOrSendPcm(Int16Array.from(msg.pcm)); }   // video-mode PCM from delay.js
  else if (msg.cmd === "dom-translate-batch") queueOrSendDomBatch(msg);
  else if (msg.cmd === "stop") stop();
  else if (msg.cmd === "ask") {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "ask", mode: msg.mode, transcript: msg.transcript || "", question: msg.question || "" }));
    } else {
      chrome.runtime.sendMessage({ route: "background", type: "answer", text: "자막을 시작한 상태에서만 요약/질문이 됩니다." });
    }
  }
});

// cold-start race fix: the cmd:"start"/"start-relay" message can be sent before this doc's
// listener is registered (createDocument resolves before scripts run). offscreen has no working
// chrome.storage here, so we can't self-resume from session — instead announce readiness and let
// the service worker (re)deliver the start params + settings. Dedupe in start()/startRelay makes
// the warm message + this handshake idempotent.
chrome.runtime.sendMessage({ route: "background", type: "offscreen-ready" });

function connectWS() {
  if (ws && ws.readyState !== WebSocket.CLOSED) {
    try {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      ws.close();
    } catch (_) {}
  }
  wsConfigured = false;
  ws = new WebSocket(globalThis.LCC_BRIDGE_URL);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    backoff = 0;                                    // good connect -> reset backoff
    chrome.runtime.sendMessage({ route: "background", type: "wsstate", open: true });
    globalThis.lccBridgeHello(ws);
    sendBridgeConfig();
  };
  ws.onclose = (ev) => {
    wsConfigured = false;
    resetStreamClock();                              // new WS means server audio_ms starts at 0 again
    if (relayMode) relayReconnect = true;            // bridge audio_ms resets on reconnect -> offscreen must re-anchor (wall-based)
    chrome.runtime.sendMessage({ route: "background", type: "wsstate", open: false });
    const why = ev && (ev.code || ev.reason) ? "브릿지 연결 끊김 (" + ev.code + (ev.reason ? " · " + ev.reason : "") + ")" : "브릿지 연결 끊김";
    report(why);
    if (active) scheduleReconnect();                // dropped/restarted while capturing -> retry (audio stays up)
  };
  ws.onerror = () => report("브릿지(ws://127.0.0.1:8765) 연결 실패 — server.py 실행 확인");
  ws.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === "caption" || d.type === "caption_partial" || d.type === "source" ||
          d.type === "dom_translate_result" || d.type === "dom_translate_done" ||
          d.type === "dom_translate_busy" || d.type === "dom_translate_err" ||
          d.type === "answer_partial" || d.type === "answer" ||
          d.type === "err" || d.type === "notice") {   // surface bridge diagnostics (e.g. ASR switch failure) — content.js renders them
        chrome.runtime.sendMessage({ route: "background", ...d });
      }
    } catch (_) {}
  };
}
function sendBridgeConfig() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify(globalThis.lccBuildBridgeConfig(currentConfig, currentPageContext)));
    wsConfigured = true;
    flushBufferedPcm();
    flushDomBatches();
  } catch (e) { report("config 전송 실패: " + (e && e.message || e)); }
}
function scheduleReconnect() {
  if (reconnectTimer || !active) return;
  backoff = Math.min(8000, (backoff || 500) * 1.6);   // 0.8s -> 1.3 -> 2 -> ... cap 8s
  console.log("[lcc-offscreen] bridge reconnect in", backoff, "ms");
  reconnectTimer = setTimeout(() => { reconnectTimer = null; if (active) connectWS(); }, backoff);
}
function resetBufferedPcm() {
  bufferedPcm = [];
  bufferedBytes = 0;
  droppedPcmMs = 0;
}
function resetDomBatches() {
  domBatchQueue = [];
  domBatchBytes = 0;
}
function resetStreamClock() {
  streamClockWall = 0;
  streamClockSent = false;
}
function rememberStreamClock() {
  if (!streamClockWall) streamClockWall = Date.now();
}
function announceStreamClock() {
  if (streamClockSent || !streamClockWall) return;
  if (relayMode && !relayReconnect) return;   // initial video anchor is stamped precisely by delay.js (page perf); offscreen re-anchors only after a reconnect
  streamClockSent = true;
  chrome.runtime.sendMessage({
    route: "background",
    type: "stream-clock-start",
    mode: relayMode ? "video" : "audio",
    playbackDelayMs: Math.round(currentDelaySec * 1000),
    streamStartWall: streamClockWall,
  });
}
function lccWsCanSendPcm() {
  return ws && ws.readyState === WebSocket.OPEN && wsConfigured && ws.bufferedAmount < WS_BACKPRESSURE_BYTES;
}
function lccWsCanSendControl() {
  return ws && ws.readyState === WebSocket.OPEN && wsConfigured && ws.bufferedAmount < WS_BACKPRESSURE_BYTES;
}
function bufferPcmBytes(bytes) {
  if (!active) return;
  bufferedPcm.push(bytes);
  bufferedBytes += bytes.byteLength;
  let dropped = false;
  while (bufferedBytes > PCM_BUFFER_BYTES && bufferedPcm.length) {
    const old = bufferedPcm.shift();
    bufferedBytes -= old.byteLength;
    const droppedMs = (old.byteLength / 2 / PCM_RATE) * 1000;
    droppedPcmMs += droppedMs;
    if (streamClockWall) streamClockWall += droppedMs;  // server audio_ms starts at the first retained PCM
    dropped = true;
  }
  // The anchor just moved forward, but a clock was likely already announced — re-arm so the corrected
  // stream-clock-start is re-sent on the next flush, otherwise captions drift by the dropped duration.
  if (dropped && streamClockWall) streamClockSent = false;
}
function queueOrSendPcm(pcm) {
  if (!pcm) return;
  rememberStreamClock();
  const bytes = pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength);
  if (lccWsCanSendPcm() && bufferedPcm.length === 0) {
    announceStreamClock();
    ws.send(bytes);
    return;
  }
  bufferPcmBytes(bytes);
  flushBufferedPcm();
}
function flushBufferedPcm() {
  if (!ws || ws.readyState !== WebSocket.OPEN || !wsConfigured || !bufferedPcm.length) return;
  const hadDropped = droppedPcmMs > 0;
  let sent = 0;
  announceStreamClock();
  while (bufferedPcm.length && lccWsCanSendPcm()) {
    const bytes = bufferedPcm.shift();
    bufferedBytes -= bytes.byteLength;
    ws.send(bytes);
    sent += 1;
  }
  if (bufferedPcm.length) return;
  if (hadDropped) {
    notice("브릿지 재연결됨 — 끊긴 동안 약 " + Math.round(droppedPcmMs / 100) / 10 + "초 오디오는 유실됨");
  } else if (sent) {
    notice("브릿지 재연결됨 — 최근 오디오 이어서 전송");
  }
  droppedPcmMs = 0;
}

async function startPage(pageContext, config) {
  pageActive = true;
  active = true;
  currentPageContext = pageContext || currentPageContext || "";
  currentConfig = config || currentConfig || {};
  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
    connectWS();
  } else {
    sendBridgeConfig();
  }
  flushDomBatches();
}

function queueOrSendDomBatch(msg) {
  if (!pageActive || !msg.requestId || !Array.isArray(msg.items) || !msg.items.length) return;
  const payload = {
    type: "dom_translate_batch",
    request_id: String(msg.requestId),
    items: msg.items.slice(0, 12).map((it) => ({
      id: String(it.id || ""),
      text: String(it.text || ""),
    })).filter((it) => it.id && it.text.trim()),
  };
  if (!payload.items.length) return;
  const raw = JSON.stringify(payload);
  const bytes = raw.length * 2;
  if (lccWsCanSendControl() && domBatchQueue.length === 0) {
    ws.send(raw);
    return;
  }
  domBatchQueue.push(raw);
  domBatchBytes += bytes;
  while (domBatchBytes > DOM_BATCH_QUEUE_BYTES && domBatchQueue.length) {
    const old = domBatchQueue.shift();
    domBatchBytes -= old.length * 2;
  }
  flushDomBatches();
}

function flushDomBatches() {
  if (!lccWsCanSendControl() || !domBatchQueue.length) return;
  while (domBatchQueue.length && lccWsCanSendControl()) {
    const raw = domBatchQueue.shift();
    domBatchBytes -= raw.length * 2;
    ws.send(raw);
  }
}

// Video mode: delay.js (page) owns tabCapture-free A/V re-render + the undelayed PCM tap and
// forwards PCM here. We just hold the bridge WS and relay — no getUserMedia, no AudioContext.
// Everything downstream (config, stream clock, buffering/reconnect, captions, ask) is shared
// with audio mode, so video mode inherits the same robustness.
async function startRelay(pageContext, requestedDelaySec, config) {
  if (relayMode && active) return;   // dedupe: warm message + ready-handshake can both fire
  relayMode = true;
  relayReconnect = false;            // initial connect: let delay.js stamp the precise page-perf anchor
  active = true;
  currentPageContext = pageContext || "";
  currentConfig = config || {};
  currentDelaySec = Math.min(12, Math.max(0, Number(requestedDelaySec) || 0));
  stop(true);                        // drop any prior socket; keep active/relay flags (keepId)
  resetBufferedPcm();
  resetStreamClock();
  connectWS();                       // hello + config + flush-on-open; reconnects on drop (active)
  report("relay 모드 — 페이지 PCM 중계 시작 (delay=" + currentDelaySec + "s)");
}

async function start(streamId, pageContext, requestedDelaySec, config) {
  currentPageContext = pageContext || "";
  if (currentId === streamId) return;   // dedupe (warm message + ready-handshake can both fire)
  currentId = streamId;
  currentConfig = config || {};
  relayMode = false;
  relayReconnect = false;
  stop(true);
  resetBufferedPcm();
  resetStreamClock();
  active = true;
  connectWS();

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } }
    });

    audioCtx = new AudioContext();
    if (audioCtx.state === "suspended") await audioCtx.resume();
    const source = audioCtx.createMediaStreamSource(stream);

    const delaySec = Math.min(12, Math.max(0, Number(requestedDelaySec) || 0));
    currentDelaySec = delaySec;
    if (delaySec > 0) {
      const d = audioCtx.createDelay(delaySec + 1); d.delayTime.value = delaySec;
      source.connect(d); d.connect(audioCtx.destination);
    } else {
      source.connect(audioCtx.destination);
    }

    await audioCtx.audioWorklet.addModule(chrome.runtime.getURL("pcm-worklet.js"));
    node = new AudioWorkletNode(audioCtx, "pcm-worklet");
    source.connect(node);
    const resample = lccMakeResampler(audioCtx.sampleRate, 16000);
    node.port.onmessage = (ev) => {
      const pcm = resample(ev.data);
      queueOrSendPcm(pcm);
    };
    report("캡처 시작 OK (rate=" + audioCtx.sampleRate + ", audioDelay=" + delaySec + "s)");
  } catch (e) {
    // Setup failed after the WS was opened optimistically. Tear everything down (socket, reconnect
    // loop, currentId) so we don't leave a zombie connection + retry loop, and so a same-streamId
    // restart isn't deduped away. stop(false) detaches onclose, so announce the closed state here.
    report("탭 오디오 캡처/설정 실패: " + (e && e.message || e));
    stop(false);
    chrome.runtime.sendMessage({ route: "background", type: "wsstate", open: false });
    throw e;
  }
}

function stop(keepId) {
  if (!keepId) { active = false; currentId = null; relayMode = false; relayReconnect = false; pageActive = false; }   // real stop -> don't auto-reconnect
  resetBufferedPcm();
  resetDomBatches();
  resetStreamClock();
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  try { node && (node.port.onmessage = null, node.disconnect()); } catch (_) {}
  try { stream && stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
  try { audioCtx && audioCtx.close(); } catch (_) {}
  try { if (ws) {
    const sock = ws;
    ws = null;
    if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify({ type: "eos" }));
    sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;   // detach so this close doesn't trigger reconnect
    sock.close();
  } } catch (_) {}
  ws = audioCtx = node = stream = null;
  wsConfigured = false;
}
