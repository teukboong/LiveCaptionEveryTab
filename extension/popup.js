const btn = document.getElementById("btn");
const status = document.getElementById("status");
let capturing = false;

// Shared defaults live in protocol.js so popup/background/offscreen send the same bridge config.
const DEFAULTS = globalThis.LCC_DEFAULT_SETTINGS;
const BRIDGE_SETTING_KEYS = new Set(["targetLang", "asrEngine"]);
// 영상 종류 프리셋: 한 번 고르면 말투(register)+지연(latencyMode)을 콘텐츠에 맞춰 묶어 세팅 (개별 노출 X).
const LCC_PRESETS = globalThis.LCC_CONTENT_PRESETS;
const RANGES = { fontSize: "fontSize", bottomPct: "bottomPct", leftPct: "leftPct", delaySec: "delaySec",
                 sentSilenceMs: "sentSilenceMs", vadLevel: "vadLevel", syncOffsetMs: "syncOffsetMs" };

function formatRangeValue(key, value) {
  if (key === "syncOffsetMs") {
    const n = Number(value) || 0;
    return (n > 0 ? "+" : "") + n + "ms";
  }
  return value;
}

function setState(on) {
  capturing = on;
  btn.textContent = on ? "■ 자막 중지" : "▶ 자막 시작";
  btn.className = on ? "stop" : "start";
}

// ---- settings ----
let settings = { ...DEFAULTS };
async function loadSettings() {
  const r = await chrome.storage.local.get("lcc-settings");
  settings = { ...DEFAULTS, ...(r["lcc-settings"] || {}) };
  for (const [key, id] of Object.entries(RANGES)) {
    const el = document.getElementById(id);
    el.value = settings[key];
    document.getElementById(id + "V").textContent = formatRangeValue(key, settings[key]);
  }
  document.getElementById("showSource").checked = settings.showSource;
  document.getElementById("videoDelay").checked = settings.videoDelay;
  document.getElementById("targetLang").value = settings.targetLang;
  document.getElementById("asrEngine").value = settings.asrEngine;
  document.getElementById("contentType").value = settings.contentType;
  document.getElementById("latencyMode").value = settings.latencyMode;
  document.getElementById("register").value = settings.register;
  document.getElementById("accuracyMode").checked = settings.accuracyMode;
  document.getElementById("autoPrime").checked = settings.autoPrime;
  document.getElementById("debugSync").checked = settings.debugSync;
  document.getElementById("contextHint").value = settings.contextHint;
  document.getElementById("glossary").value = settings.glossary;
  setMode(settings.uiMode || "simple");
}
async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab && tab.id != null ? tab : null;
}
async function getPageContext(tabId) {
  if (tabId == null || !settings.autoPrime) return "";
  try {
    const res = await chrome.tabs.sendMessage(tabId, { type: "page-context-get" });
    return (res && res.context || "").trim();
  } catch (_) {
    return "";
  }
}
async function saveSettings(pushConfig = false) {
  await chrome.storage.local.set({ "lcc-settings": settings });
  if (pushConfig) chrome.runtime.sendMessage({ type: "popup-config-update" });
}
let _pushCfgTimer = null;
function pushBridgeConfigDebounced(ms = 400) {   // free-text inputs fire per keystroke; coalesce the live bridge push
  if (_pushCfgTimer) clearTimeout(_pushCfgTimer);
  _pushCfgTimer = setTimeout(() => { _pushCfgTimer = null; chrome.runtime.sendMessage({ type: "popup-config-update" }); }, ms);
}
for (const [key, id] of Object.entries(RANGES)) {
  document.getElementById(id).addEventListener("input", (e) => {
    settings[key] = Number(e.target.value);
    document.getElementById(id + "V").textContent = formatRangeValue(key, settings[key]);
    saveSettings(BRIDGE_SETTING_KEYS.has(key));
  });
}
document.getElementById("showSource").addEventListener("change", (e) => {
  settings.showSource = e.target.checked;
  saveSettings();
});
document.getElementById("videoDelay").addEventListener("change", (e) => {
  settings.videoDelay = e.target.checked;
  saveSettings();
});
document.getElementById("targetLang").addEventListener("change", (e) => {
  settings.targetLang = e.target.value;
  saveSettings(true);
});
document.getElementById("asrEngine").addEventListener("change", (e) => {
  settings.asrEngine = e.target.value;
  saveSettings(true);
});
document.getElementById("contentType").addEventListener("change", (e) => {
  settings.contentType = e.target.value;
  const p = LCC_PRESETS[settings.contentType] || LCC_PRESETS.general;   // bundle tone + latency for the content type
  settings.register = p.register;
  settings.latencyMode = p.latencyMode;
  saveSettings(true);                                                   // shared bridge config pushes register+latencyMode live
});

// ---- capture + connection state ----
function setConn(capturing, wsOpen) {
  const el = document.getElementById("conn");
  if (!capturing) { el.textContent = ""; return; }
  el.textContent = wsOpen ? "🟢 브릿지 연결됨" : "🔴 브릿지 재연결 중…";
  el.style.color = wsOpen ? "#16a34a" : "#dc2626";
}
chrome.runtime.sendMessage({ type: "popup-status" }, (res) => {
  if (chrome.runtime.lastError) return;
  if (res) { setState(res.capturing); setConn(res.capturing, res.wsOpen); }
});
loadSettings();

btn.onclick = async () => {
  if (capturing) {
    chrome.runtime.sendMessage({ type: "popup-stop" });   // background tears down the right mode+tab
    setState(false);
    status.textContent = "중지됨";
    return;
  }
  const tab = await getActiveTab();
  if (!tab || tab.id == null) { status.textContent = "활성 탭을 찾지 못함"; return; }
  try {
    const pageContext = await getPageContext(tab.id);
    if (settings.videoDelay) {
      // B-2: delay.js captures the page <video> directly; routed via background so state+stop are tracked
      chrome.runtime.sendMessage({ type: "popup-start-video", tabId: tab.id, delaySec: settings.delaySec, pageContext });
      setState(true);
      status.textContent = "🎬 영상 지연 모드 — 영상이 재생 중이어야 함";
    } else {
      await chrome.runtime.sendMessage({ type: "popup-cleanup" });   // release stale stream before getMediaStreamId
      const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
      chrome.runtime.sendMessage({ type: "popup-start", streamId, tabId: tab.id, delaySec: settings.delaySec, pageContext });
      setState(true);
      status.textContent = "✓ 캡처 시작됨 — 영상에서 발화 대기";
    }
  } catch (e) {
    status.textContent = "실패: " + (e && e.message || e);
  }
};

// ---- 자막 기록 · AI (transcript mirrored to storage.local by content.js; answers via storage.session) ----
function lccFmtClock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}
async function renderHist() {
  const r = await chrome.storage.local.get(["lcc-transcript", "lcc-session"]);
  const tr = r["lcc-transcript"] || [];
  const start = (r["lcc-session"] && r["lcc-session"].start) || (tr[0] && tr[0].t) || 0;
  const el = document.getElementById("hist");
  el.innerHTML = "";
  for (const e of tr) {
    const row = document.createElement("div"); row.className = "h-row";
    row.innerHTML = '<div class="h-t"><span class="h-tt"></span></div><div class="h-src"></div><div class="h-ko"></div>';
    row.querySelector(".h-tt").textContent = lccFmtClock(e.t - start);
    row.querySelector(".h-src").textContent = e.source || "";
    row.querySelector(".h-ko").textContent = e.ko || "";
    el.appendChild(row);
  }
  el.scrollTop = el.scrollHeight;
}
function sendAsk(mode, question) {
  const res = document.getElementById("aiResult");
  if (!capturing) { res.textContent = "자막을 시작한 상태에서만 요약/질문이 됩니다."; return; }
  res.textContent = (mode === "qa" ? "⏳ 답하는 중…" : "⏳ 요약 중…");
  chrome.storage.local.get("lcc-transcript").then((r) => {
    const transcript = (r["lcc-transcript"] || []).map((e) => e.source || e.ko).join(" ").slice(-8000);   // match server window; keep the control msg small
    if (!transcript.trim()) { res.textContent = "(아직 자막 기록이 없어요)"; return; }
    chrome.storage.session.remove("lcc-answer");
    chrome.runtime.sendMessage({ type: "lcc-ask", mode: mode, question: question || "", transcript: transcript });
  });
}
document.getElementById("aiSum").onclick = () => sendAsk("summary", "");
const aiQ = document.getElementById("aiQ");
aiQ.addEventListener("keydown", (e) => { if (e.key === "Enter" && aiQ.value.trim()) { sendAsk("qa", aiQ.value.trim()); aiQ.value = ""; } });
document.getElementById("clearTr").onclick = async () => {
  const tab = await getActiveTab();
  await chrome.runtime.sendMessage({ type: "popup-clear-transcript", tabId: tab && tab.id });
  await chrome.storage.session.remove("lcc-answer");
  document.getElementById("hist").innerHTML = "";
  document.getElementById("aiResult").textContent = "";
};
document.getElementById("exportMd").onclick = async () => {
  const res = document.getElementById("aiResult");
  const r = await chrome.storage.local.get(["lcc-transcript", "lcc-session"]);
  const tr = r["lcc-transcript"] || [];
  if (!tr.length) { res.textContent = "(기록 없음)"; return; }
  const sess = r["lcc-session"] || {};
  const start = sess.start || (tr[0] && tr[0].t) || 0;
  const out = ["# " + (sess.title || "Live Caption") + " — 자막 기록", "", new Date().toLocaleString(), ""];
  for (const e of tr) out.push("**[" + lccFmtClock(e.t - start) + "]** " + e.source, "", "> " + e.ko, "");
  const blob = new Blob([out.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "livecaption-" + new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-") + ".md";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
};
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === "local" && ch["lcc-transcript"]) renderHist();
  if (area === "session" && ch["lcc-answer"] && ch["lcc-answer"].newValue) {
    document.getElementById("aiResult").textContent = ch["lcc-answer"].newValue.text || "";
  }
});
renderHist();
chrome.storage.session.get("lcc-answer").then((r) => {
  if (r["lcc-answer"]) document.getElementById("aiResult").textContent = r["lcc-answer"].text || "";
});

// ---- bridge control (native-messaging host launches/stops the local server.py) ----
// Requires the one-time host install: extension/native-host/install-host.sh
const LCC_NM_HOST = "io.github.teukboong.livecaption";
function nmSend(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(LCC_NM_HOST, msg, (resp) => {
        if (chrome.runtime.lastError) resolve({ ok: false, noHost: true, error: chrome.runtime.lastError.message });
        else resolve(resp || { ok: false, error: "빈 응답" });
      });
    } catch (e) { resolve({ ok: false, noHost: true, error: String(e) }); }
  });
}
const bridgeBtn = document.getElementById("bridgeBtn");
const bridgeStopBtn = document.getElementById("bridgeStopBtn");
const bridgeStatusEl = document.getElementById("bridgeStatus");
let bridgePoll = null;
function setBridgeUI(state, text) {           // state: on | off | starting | nohost
  bridgeStatusEl.style.color = state === "on" ? "#16a34a" : (state === "nohost" ? "#dc2626" : "#666");
  if (text != null) bridgeStatusEl.textContent = text;
  bridgeBtn.disabled = (state === "starting");
  bridgeStopBtn.style.display = (state === "on" || state === "starting") ? "" : "none";
  bridgeBtn.textContent = state === "on" ? "✅ 브릿지 켜짐" : "🚀 브릿지 켜기";
}
async function refreshBridge() {
  const r = await nmSend({ cmd: "status" });
  if (r.noHost) { setBridgeUI("nohost", "❌ 호스트 미설치"); return; }
  setBridgeUI(r.running ? "on" : "off", r.running ? ("🟢 켜짐" + (r.pid ? " (pid " + r.pid + ")" : "")) : "꺼짐");
}
function pollBridgeUntilUp(maxSec) {
  if (bridgePoll) clearInterval(bridgePoll);
  let t = 0;
  bridgePoll = setInterval(async () => {
    t += 2;
    const r = await nmSend({ cmd: "status" });
    if (r.running) { clearInterval(bridgePoll); bridgePoll = null; setBridgeUI("on", "🟢 켜짐"); }
    else if (t >= maxSec) { clearInterval(bridgePoll); bridgePoll = null; setBridgeUI("off", "⌛ 응답 없음 — ~/.lcc-bridge.log 확인"); }
    else setBridgeUI("starting", "⏳ 기동 중… (" + t + "s · 모델 로드 ~40s)");
  }, 2000);
}
bridgeBtn.onclick = async () => {
  setBridgeUI("starting", "⏳ 시작 요청…");
  const r = await nmSend({ cmd: "start", asrEngine: settings.asrEngine || "granite" });
  if (r.noHost) { setBridgeUI("nohost", "❌ 호스트 미설치 — install-host.sh 실행"); return; }
  if (!r.ok) { setBridgeUI("off", "❌ " + (r.error || "실패")); return; }
  if (r.already || r.running) { setBridgeUI("on", "🟢 이미 켜짐"); return; }
  pollBridgeUntilUp(70);
};
bridgeStopBtn.onclick = async () => {
  if (bridgePoll) { clearInterval(bridgePoll); bridgePoll = null; }
  setBridgeUI("starting", "⏳ 종료 중…");
  const r = await nmSend({ cmd: "stop" });
  if (r.noHost) { setBridgeUI("nohost", "❌ 호스트 미설치"); return; }
  setBridgeUI(r.running ? "on" : "off", r.running ? "❌ 종료 실패" : "꺼짐");
};
refreshBridge();

// ---- Simple / Advanced mode ----
function setMode(mode) {
  const adv = mode === "advanced";
  document.getElementById("adv").hidden = !adv;
  document.getElementById("modeAdv").classList.toggle("active", adv);
  document.getElementById("modeSimple").classList.toggle("active", !adv);
}
document.getElementById("modeSimple").onclick = () => { settings.uiMode = "simple"; setMode("simple"); saveSettings(); };
document.getElementById("modeAdv").onclick = () => { settings.uiMode = "advanced"; setMode("advanced"); saveSettings(); };

// ---- advanced parameter controls (the raw knobs; ranges handled by the generic RANGES loop) ----
document.getElementById("latencyMode").addEventListener("change", (e) => { settings.latencyMode = e.target.value; saveSettings(true); });
document.getElementById("register").addEventListener("change", (e) => { settings.register = e.target.value; saveSettings(true); });
document.getElementById("accuracyMode").addEventListener("change", (e) => { settings.accuracyMode = e.target.checked; saveSettings(); });
document.getElementById("autoPrime").addEventListener("change", (e) => { settings.autoPrime = e.target.checked; saveSettings(); });
document.getElementById("debugSync").addEventListener("change", (e) => { settings.debugSync = e.target.checked; saveSettings(); });
document.getElementById("contextHint").addEventListener("input", (e) => { settings.contextHint = e.target.value; saveSettings(); pushBridgeConfigDebounced(); });
document.getElementById("glossary").addEventListener("input", (e) => { settings.glossary = e.target.value; saveSettings(); pushBridgeConfigDebounced(); });

// ---- model install (full/mid/lite): native host spawns the downloader; poll progress ----
const TIER_LABEL = { full: "Full", mid: "Mid", lite: "Lite" };
let instPoll = null;
function setInstStatus(text, color) {
  const el = document.getElementById("instStatus");
  el.textContent = text;
  el.style.color = color || "#999";
}
function setInstBusy(busy) {
  for (const id of ["instFull", "instMid", "instLite"]) document.getElementById(id).disabled = busy;
}
function pollInstall() {
  if (instPoll) clearInterval(instPoll);
  setInstBusy(true);
  instPoll = setInterval(async () => {
    const r = await nmSend({ cmd: "install_status" });
    if (r.noHost) { clearInterval(instPoll); instPoll = null; setInstBusy(false); setInstStatus("❌ 호스트 미설치", "#dc2626"); return; }
    if (r.idle) return;
    if (r.done) {
      clearInterval(instPoll); instPoll = null; setInstBusy(false);
      if (r.ok) setInstStatus("✅ " + (TIER_LABEL[r.tier] || r.tier || "") + " 설치 완료 — 브릿지 (재)시작 시 적용", "#16a34a");
      else setInstStatus("❌ 실패: " + (r.error || "") + " (~/.lcc-install.log 확인)", "#dc2626");
      return;
    }
    const n = (r.index || 0) + 1, t = r.total || "?";
    setInstStatus("⏳ " + (r.current || "다운로드 중") + "  (" + n + "/" + t + ")", "#666");
  }, 2000);
}
async function startInstall(tier) {
  setInstBusy(true);
  setInstStatus("⏳ " + TIER_LABEL[tier] + " 설치 요청…", "#666");
  const r = await nmSend({ cmd: "install", tier });
  if (r.noHost) { setInstBusy(false); setInstStatus("❌ 호스트 미설치 — install-host.sh 실행", "#dc2626"); return; }
  if (!r.ok) { setInstBusy(false); setInstStatus("❌ " + (r.error || "실패"), "#dc2626"); return; }
  pollInstall();
}
document.getElementById("instFull").onclick = () => startInstall("full");
document.getElementById("instMid").onclick = () => startInstall("mid");
document.getElementById("instLite").onclick = () => startInstall("lite");
// reflect any in-progress / last install when the popup opens
nmSend({ cmd: "install_status" }).then((r) => {
  if (!r || r.idle || r.noHost) return;
  if (!r.done) pollInstall();
  else if (r.ok && r.current === "완료") setInstStatus("✅ " + (TIER_LABEL[r.tier] || r.tier || "") + " 설치됨", "#16a34a");
});
