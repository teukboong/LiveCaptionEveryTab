const btn = document.getElementById("btn");
const status = document.getElementById("status");
let capturing = false;

// Shared defaults live in protocol.js so popup/background/offscreen send the same bridge config.
const DEFAULTS = globalThis.LCC_DEFAULT_SETTINGS;
const BRIDGE_SETTING_KEYS = new Set(["targetLang", "asrEngine"]);
// Range sliders the bridge tunes live (server re-applies on a `config` message without dropping
// the in-flight utterance). Display-only ranges (font/position/delay/sync) stay local.
const BRIDGE_RANGE_KEYS = new Set(["vadLevel", "sentSilenceMs"]);
// 영상 종류 프리셋: 한 번 고르면 말투(register)+지연(latencyMode)을 콘텐츠에 맞춰 묶어 세팅 (개별 노출 X).
const LCC_PRESETS = globalThis.LCC_CONTENT_PRESETS;
const RANGES = { fontSize: "fontSize", bottomPct: "bottomPct", leftPct: "leftPct", delaySec: "delaySec",
                 sentSilenceMs: "sentSilenceMs", vadLevel: "vadLevel", syncOffsetMs: "syncOffsetMs" };
const UI_TEXT = Object.freeze({
  ko: Object.freeze({
    modeSimple: "Simple",
    modeAdvanced: "Advanced",
    captionStart: "자막 시작",
    captionStop: "자막 중지",
    bridgeRequired: "로컬 브릿지(server.py)가 실행 중이어야 합니다",
    bridgeStart: "브릿지 켜기",
    bridgeStarted: "브릿지 켜짐",
    bridgeStop: "끄기",
    bridgeStopTitle: "브릿지 끄기",
    sectionMode: "동작 모드",
    pageTranslate: "페이지 번역",
    captionTranslate: "동영상 번역",
    pageTranslateHint: "페이지 번역은 오버레이 없이 실제 DOM 텍스트를 순차적으로 교체합니다.",
    sectionUi: "UI",
    labelUiLang: "UI 언어",
    sectionInstall: "모델 설치 · 티어",
    installHint: "사양에 맞는 모델을 받아 번역 티어로 배선 (Apple Silicon). 안 눌러도 첫 실행 때 메모리에 맞춰 자동 선택됩니다.",
    sectionTranscript: "자막 기록 · AI",
    summary: "요약",
    askPlaceholder: "이 영상에 질문…",
    exportMd: ".md 저장",
    clearTranscript: "기록 지우기",
    sectionAsr: "전사 엔진",
    labelAsr: "ASR",
    asrGranite: "Granite (영어)",
    asrQwen: "Qwen3-ASR (일어·다국어)",
    asrHint: "Granite=영어 충실 · Qwen3=일어/한국어 등 다국어 구두점 · 실행 중이면 다음 발화부터",
    sectionTranslation: "번역",
    labelTargetLang: "대상 언어",
    sectionContent: "영상 종류",
    labelPreset: "프리셋",
    presetGeneral: "일반 · 잡담",
    presetConference: "컨퍼런스 · 강연",
    presetNews: "뉴스 · 인터뷰",
    presetStreaming: "개인 스트리밍",
    contentHint: "말투·지연을 콘텐츠에 맞게 한 번에 — 강연=격식·안정, 뉴스=균형, 스트리밍=구어·즉각 · 실행 중 적용",
    sectionDisplay: "자막 표시",
    labelFontSize: "글자 크기",
    labelBottom: "상하 위치",
    labelLeft: "좌우 위치",
    showSource: "원문 줄 표시 (끄면 Alt 누른 동안만 보기)",
    sectionSync: "싱크",
    labelDelay: "재생 지연",
    videoDelay: "영상도 지연 (DRM 불가)",
    syncHint: "기본은 소리만 지연돼서 자막이 영상보다 먼저 뜰 수 있음. 켜면 영상도 같이 늦춰 영상과 자막 싱크를 맞춥니다.",
    sectionAdvanced: "고급 · 직접 파라미터",
    labelLatency: "지연 모드",
    latencyStable: "안정 (확정만)",
    latencyBalanced: "균형",
    latencyAggressive: "공격 (즉각)",
    labelRegister: "말투",
    registerCasual: "캐주얼",
    registerLecture: "강연 · 격식",
    registerNews: "뉴스",
    registerChat: "잡담 · 구어",
    advancedPresetHint: "영상 종류 프리셋이 지연 모드·말투를 묶어 정함. 여기서 개별로 덮어쓸 수 있음(다음 프리셋 변경 시 재설정).",
    labelSentSilence: "문장 대기",
    labelVad: "음성 감지",
    advancedVadHint: "문장 대기↑ = 더 긴 문맥으로 번역(지연↑) · 음성 감지↑ = 잡음/음악 더 무시",
    accuracyMode: "정확도 모드 (문장 2패스 재전사)",
    autoPrime: "자동 용어 프라이밍 (제목을 힌트로)",
    labelPageStream: "페이지 출력",
    pageStreamPartial: "라이브 partial",
    pageStreamFinal: "확정만",
    pageBilingual: "원문 보기 (번역 위에 마우스)",
    pageVerify: "캐시 번역 idle 재확인",
    labelSyncOffset: "싱크 보정",
    debugSync: "싱크 디버그 표시",
    labelContextHint: "용어 힌트",
    contextPlaceholder: "자유 텍스트 바이어싱",
    labelGlossary: "용어집",
    glossaryPlaceholder: "이름=번역 (줄마다 하나)\n예: Blackwell=블랙웰",
    advancedApplyHint: "용어집·용어 힌트는 다음 발화부터 / 정확도·음성 감지는 자막 다시 시작 시 적용",
    sectionPageAdvanced: "페이지 번역",
    labelPageRegister: "페이지 말투",
    pageRegisterCasual: "짧은 UI · 일반",
    pageRegisterLecture: "문서 · 격식",
    pageRegisterNews: "뉴스 · 보도",
    pageRegisterChat: "댓글 · 대화",
    labelPageContextHint: "페이지 힌트",
    pageContextPlaceholder: "비우면 영상 용어 힌트 상속",
    labelPageGlossary: "페이지 용어집",
    pageGlossaryPlaceholder: "비우면 영상 용어집 상속\n예: subreddit=서브레딧",
    pageAdvancedApplyHint: "페이지 설정은 DOM 번역에만 적용됩니다. 페이지 용어집을 비우면 위 용어집을 상속합니다.",
    connConnected: "브릿지 연결됨",
    connReconnecting: "브릿지 재연결 중…",
    stopped: "중지됨",
    noActiveTab: "활성 탭을 찾지 못함",
    chooseRunMode: "페이지 번역이나 동영상 번역 중 하나는 켜야 합니다",
    pageCaptionRunning: "페이지 + 동영상 번역 중",
    pageRunning: "페이지 DOM 번역 중",
    captionRunning: "동영상 번역 중",
    videoMode: "영상 지연 모드 — 영상이 재생 중이어야 함",
    captureStarted: "캡처 시작됨 — 영상에서 발화 대기",
    failurePrefix: "실패: ",
    askNeedsCaption: "자막을 시작한 상태에서만 요약/질문이 됩니다.",
    answering: "답하는 중…",
    summarizing: "요약 중…",
    noTranscriptYet: "(아직 자막 기록이 없어요)",
    noRecord: "(기록 없음)",
    transcriptTitleSuffix: "자막 기록",
    emptyNativeResponse: "빈 응답",
    noHost: "호스트 미설치",
    off: "꺼짐",
    on: "켜짐",
    alreadyOn: "이미 켜짐",
    startRequested: "시작 요청…",
    starting: "기동 중… 모델 로드 ~40s",
    startingWithSeconds: "기동 중… ({seconds}s · 모델 로드 ~40s)",
    noResponseLog: "응답 없음 — ~/.lcc-bridge.log 확인",
    setupHost: "호스트 미설치 — 터미널에서 ./setup.sh 1회",
    stopping: "종료 중…",
    stopFailed: "종료 실패",
    downloadingDefault: "다운로드 중",
    installIdle: "설치 진행 없음 — 다시 선택하세요",
    installComplete: "{tier} 설치 완료 — 브릿지 (재)시작 시 적용",
    installFailed: "실패: {error} (~/.lcc-install.log 확인)",
    downloading: "{name}  ({index}/{total})",
    installRequest: "{tier} 설치 요청…",
    installed: "{tier} 설치됨",
  }),
  en: Object.freeze({
    modeSimple: "Simple",
    modeAdvanced: "Advanced",
    captionStart: "Start captions",
    captionStop: "Stop captions",
    bridgeRequired: "Local bridge (server.py) must be running",
    bridgeStart: "Start bridge",
    bridgeStarted: "Bridge on",
    bridgeStop: "Stop",
    bridgeStopTitle: "Stop bridge",
    sectionMode: "Mode",
    pageTranslate: "Page translation",
    captionTranslate: "Video translation",
    pageTranslateHint: "Page translation replaces real DOM text incrementally, without an overlay.",
    sectionUi: "UI",
    labelUiLang: "UI language",
    sectionInstall: "Model install · tier",
    installHint: "Download and wire the translation tier for this Mac. If untouched, the first run auto-selects by memory.",
    sectionTranscript: "Transcript · AI",
    summary: "Summary",
    askPlaceholder: "Ask about this video…",
    exportMd: "Save .md",
    clearTranscript: "Clear history",
    sectionAsr: "Speech engine",
    labelAsr: "ASR",
    asrGranite: "Granite (English)",
    asrQwen: "Qwen3-ASR (Japanese/multilingual)",
    asrHint: "Granite is best for English. Qwen3 handles Japanese/Korean and multilingual punctuation. Changes apply from the next utterance.",
    sectionTranslation: "Translation",
    labelTargetLang: "Target language",
    sectionContent: "Content type",
    labelPreset: "Preset",
    presetGeneral: "General · chat",
    presetConference: "Conference · lecture",
    presetNews: "News · interview",
    presetStreaming: "Personal stream",
    contentHint: "Applies tone and latency together: lecture=polished/stable, news=balanced, streaming=casual/fast.",
    sectionDisplay: "Caption display",
    labelFontSize: "Font size",
    labelBottom: "Vertical position",
    labelLeft: "Horizontal position",
    showSource: "Show source line (off: hold Alt to peek)",
    sectionSync: "Sync",
    labelDelay: "Playback delay",
    videoDelay: "Delay video too (no DRM)",
    syncHint: "By default only audio is delayed, so captions can appear ahead of video. Enable this to delay video and align captions.",
    sectionAdvanced: "Advanced · raw parameters",
    labelLatency: "Latency mode",
    latencyStable: "Stable (final only)",
    latencyBalanced: "Balanced",
    latencyAggressive: "Aggressive (instant)",
    labelRegister: "Tone",
    registerCasual: "Casual",
    registerLecture: "Lecture · formal",
    registerNews: "News",
    registerChat: "Chat · spoken",
    advancedPresetHint: "Content presets bundle latency and tone. Override them here; changing the preset resets these fields.",
    labelSentSilence: "Sentence wait",
    labelVad: "Voice detect",
    advancedVadHint: "Higher sentence wait gives more context but more latency. Higher voice detect ignores more noise/music.",
    accuracyMode: "Accuracy mode (2-pass sentence retranscribe)",
    autoPrime: "Auto term priming (use title as hint)",
    labelPageStream: "Page output",
    pageStreamPartial: "Live partial",
    pageStreamFinal: "Final only",
    pageBilingual: "Show original (hover translation)",
    pageVerify: "Re-check cached labels when idle",
    labelSyncOffset: "Sync offset",
    debugSync: "Show sync debug",
    labelContextHint: "Term hint",
    contextPlaceholder: "Free-text biasing",
    labelGlossary: "Glossary",
    glossaryPlaceholder: "Name=translation (one per line)\nExample: Blackwell=Blackwell",
    advancedApplyHint: "Glossary and hints apply from the next utterance. Accuracy/VAD applies after restarting captions.",
    sectionPageAdvanced: "Page translation",
    labelPageRegister: "Page tone",
    pageRegisterCasual: "Short UI · general",
    pageRegisterLecture: "Docs · formal",
    pageRegisterNews: "News · report",
    pageRegisterChat: "Comments · chat",
    labelPageContextHint: "Page hint",
    pageContextPlaceholder: "Blank inherits the video term hint",
    labelPageGlossary: "Page glossary",
    pageGlossaryPlaceholder: "Blank inherits the video glossary\nExample: subreddit=subreddit",
    pageAdvancedApplyHint: "Page settings apply only to DOM translation. Blank page glossary inherits the glossary above.",
    connConnected: "Bridge connected",
    connReconnecting: "Bridge reconnecting…",
    stopped: "Stopped",
    noActiveTab: "No active tab found",
    chooseRunMode: "Turn on page translation or video translation first",
    pageCaptionRunning: "Page + video translation running",
    pageRunning: "Page DOM translation running",
    captionRunning: "Video translation running",
    videoMode: "Video-delay mode — the video must be playing",
    captureStarted: "Capture started — waiting for speech",
    failurePrefix: "Failed: ",
    askNeedsCaption: "Start captions before summary/questions.",
    answering: "Answering…",
    summarizing: "Summarizing…",
    noTranscriptYet: "(No transcript yet)",
    noRecord: "(No records)",
    transcriptTitleSuffix: "caption log",
    emptyNativeResponse: "Empty response",
    noHost: "Host not installed",
    off: "Off",
    on: "On",
    alreadyOn: "Already on",
    startRequested: "Start requested…",
    starting: "Starting… model load ~40s",
    startingWithSeconds: "Starting… ({seconds}s · model load ~40s)",
    noResponseLog: "No response — check ~/.lcc-bridge.log",
    setupHost: "Host not installed — run ./setup.sh once in Terminal",
    stopping: "Stopping…",
    stopFailed: "Stop failed",
    downloadingDefault: "Downloading",
    installIdle: "No install in progress — choose a tier again",
    installComplete: "{tier} installed — applies after bridge restart",
    installFailed: "Failed: {error} (check ~/.lcc-install.log)",
    downloading: "{name}  ({index}/{total})",
    installRequest: "{tier} install requested…",
    installed: "{tier} installed",
  }),
});

function tr(key, vars = {}) {
  const lang = globalThis.lccCanonicalUiLang(settings && settings.uiLang);
  let text = (UI_TEXT[lang] && UI_TEXT[lang][key]) || UI_TEXT.ko[key] || key;
  for (const [name, value] of Object.entries(vars)) {
    text = text.replaceAll("{" + name + "}", String(value));
  }
  return text;
}

function populateUiLangSelect() {
  const el = document.getElementById("uiLang");
  if (!el) return;
  const selected = globalThis.lccCanonicalUiLang(settings.uiLang);
  if (!el.options.length) {
    for (const lang of globalThis.LCC_UI_LANGS) {
      const opt = document.createElement("option");
      opt.value = lang.value;
      opt.textContent = lang.label;
      el.appendChild(opt);
    }
  }
  el.value = selected;
}

function applyUiLanguage() {
  settings.uiLang = globalThis.lccCanonicalUiLang(settings.uiLang);
  document.documentElement.lang = settings.uiLang;
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = tr(el.dataset.i18n);
  }
  for (const el of document.querySelectorAll("[data-i18n-placeholder]")) {
    el.placeholder = tr(el.dataset.i18nPlaceholder);
  }
  for (const el of document.querySelectorAll("[data-i18n-title]")) {
    el.title = tr(el.dataset.i18nTitle);
  }
  populateUiLangSelect();
  setState(capturing);
}

function populateTargetLangSelect() {
  const el = document.getElementById("targetLang");
  const selected = globalThis.lccCanonicalTargetLang(el.value || settings.targetLang);
  el.textContent = "";
  for (const lang of globalThis.LCC_TARGET_LANGS) {
    const opt = document.createElement("option");
    opt.value = lang;
    opt.textContent = lang;
    el.appendChild(opt);
  }
  el.value = selected;
}

function formatRangeValue(key, value) {
  if (key === "syncOffsetMs") {
    const n = Number(value) || 0;
    return (n > 0 ? "+" : "") + n + "ms";
  }
  return value;
}

function setState(on) {
  capturing = on;
  btn.textContent = on ? tr("captionStop") : tr("captionStart");
  btn.className = on ? "stop" : "start";
}

// ---- settings ----
let settings = { ...DEFAULTS };
function applyRunModeToggles() {
  const pageToggle = document.getElementById("pageTranslate");
  const captionToggle = document.getElementById("captionTranslate");
  pageToggle.checked = globalThis.lccRunModeIncludesPage(settings.runMode);
  captionToggle.checked = globalThis.lccRunModeIncludesCaption(settings.runMode);
  if (!pageToggle.checked && !captionToggle.checked) captionToggle.checked = true;
}
function runModeFromToggles(changedId) {
  const pageToggle = document.getElementById("pageTranslate");
  const captionToggle = document.getElementById("captionTranslate");
  if (!pageToggle.checked && !captionToggle.checked) {
    if (changedId === "captionTranslate") pageToggle.checked = true;
    else captionToggle.checked = true;
  }
  if (pageToggle.checked && captionToggle.checked) return "both";
  if (pageToggle.checked) return "page";
  return "video";
}
function saveRunModeFromToggles(changedId) {
  settings.runMode = runModeFromToggles(changedId);
  saveSettings();
}
populateTargetLangSelect();
async function loadSettings() {
  const r = await chrome.storage.local.get("lcc-settings");
  settings = globalThis.lccNormalizeSettings({ ...DEFAULTS, ...(r["lcc-settings"] || {}) });
  populateUiLangSelect();
  populateTargetLangSelect();
  for (const [key, id] of Object.entries(RANGES)) {
    const el = document.getElementById(id);
    el.value = settings[key];
    document.getElementById(id + "V").textContent = formatRangeValue(key, settings[key]);
  }
  document.getElementById("showSource").checked = settings.showSource;
  document.getElementById("videoDelay").checked = settings.videoDelay;
  applyRunModeToggles();
  document.getElementById("targetLang").value = settings.targetLang;
  document.getElementById("asrEngine").value = settings.asrEngine;
  document.getElementById("contentType").value = settings.contentType;
  document.getElementById("latencyMode").value = settings.latencyMode;
  document.getElementById("register").value = settings.register;
  document.getElementById("pageRegister").value = settings.pageRegister;
  document.getElementById("accuracyMode").checked = settings.accuracyMode;
  document.getElementById("autoPrime").checked = settings.autoPrime;
  document.getElementById("pageTranslateStream").value = settings.pageTranslateStream;
  document.getElementById("pageBilingual").checked = settings.pageBilingual !== false;
  document.getElementById("pageVerify").checked = settings.pageVerify === true;
  document.getElementById("debugSync").checked = settings.debugSync;
  document.getElementById("contextHint").value = settings.contextHint;
  document.getElementById("glossary").value = settings.glossary;
  document.getElementById("pageContextHint").value = settings.pageContextHint;
  document.getElementById("pageGlossary").value = settings.pageGlossary;
  setMode(settings.uiMode || "simple");
  applyUiLanguage();
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
async function saveSettings(pushConfig = false, resetTranslationContext = false) {
  try {
    await chrome.storage.local.set({ "lcc-settings": settings });
    if (pushConfig) await pushBridgeConfigNow(resetTranslationContext);
  } catch (e) {
    status.textContent = tr("failurePrefix") + (e && e.message || e);
  }
}
async function pushBridgeConfigNow(resetTranslationContext = false) {
  const pushed = await chrome.runtime.sendMessage({ type: "popup-config-update", resetTranslationContext });
  if (pushed && pushed.ok === false) throw new Error(pushed.error || tr("failurePrefix").trim());
}
let _pushCfgTimer = null;
function pushBridgeConfigDebounced(ms = 400, resetTranslationContext = false) {   // free-text inputs fire per keystroke; coalesce the live bridge push
  if (_pushCfgTimer) clearTimeout(_pushCfgTimer);
  _pushCfgTimer = setTimeout(async () => {
    _pushCfgTimer = null;
    try {
      await pushBridgeConfigNow(resetTranslationContext);
    } catch (e) {
      status.textContent = tr("failurePrefix") + (e && e.message || e);
    }
  }, ms);
}
for (const [key, id] of Object.entries(RANGES)) {
  document.getElementById(id).addEventListener("input", (e) => {
    settings[key] = Number(e.target.value);
    document.getElementById(id + "V").textContent = formatRangeValue(key, settings[key]);
    saveSettings();
    if (BRIDGE_RANGE_KEYS.has(key)) pushBridgeConfigDebounced();   // live-tune VAD / sentence-silence on the bridge
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
document.getElementById("pageTranslate").addEventListener("change", () => saveRunModeFromToggles("pageTranslate"));
document.getElementById("captionTranslate").addEventListener("change", () => saveRunModeFromToggles("captionTranslate"));
document.getElementById("targetLang").addEventListener("change", (e) => {
  settings.targetLang = globalThis.lccCanonicalTargetLang(e.target.value);
  e.target.value = settings.targetLang;
  saveSettings(true, true);
});
document.getElementById("uiLang").addEventListener("change", (e) => {
  settings.uiLang = globalThis.lccCanonicalUiLang(e.target.value);
  applyUiLanguage();
  saveSettings();
  refreshBridge();
});
document.getElementById("asrEngine").addEventListener("change", (e) => {
  settings.asrEngine = globalThis.lccCanonicalAsrEngine(e.target.value);
  e.target.value = settings.asrEngine;
  saveSettings(true);
});
document.getElementById("contentType").addEventListener("change", (e) => {
  settings.contentType = globalThis.lccCanonicalContentType(e.target.value);
  e.target.value = settings.contentType;
  const p = LCC_PRESETS[settings.contentType] || LCC_PRESETS.general;   // bundle tone + latency for the content type
  settings.register = p.register;
  settings.latencyMode = p.latencyMode;
  saveSettings(true, true);                                             // shared bridge config pushes register+latencyMode live
});

// ---- capture + connection state ----
function setConn(capturing, wsOpen) {
  const el = document.getElementById("conn");
  if (!capturing) { el.textContent = ""; return; }
  el.textContent = wsOpen ? tr("connConnected") : tr("connReconnecting");
  el.style.color = wsOpen ? "#16a34a" : "#dc2626";
}
chrome.runtime.sendMessage({ type: "popup-status" }, (res) => {
  if (chrome.runtime.lastError) return;
  if (res) {
    setState(res.capturing);
    setConn(res.capturing, res.wsOpen);
    if (res.pageTranslating && res.captioning) status.textContent = tr("pageCaptionRunning");
    else if (res.pageTranslating) status.textContent = tr("pageRunning");
    else if (res.captioning) status.textContent = tr("captionRunning");
  }
});
loadSettings();

btn.onclick = async () => {
  if (capturing) {
    try {
      status.textContent = tr("stopping");
      const stopped = await chrome.runtime.sendMessage({ type: "popup-stop" });   // background tears down the right mode+tab
      if (stopped && stopped.ok === false) throw new Error(stopped.error || tr("stopFailed"));
      setState(false);
      status.textContent = tr("stopped");
    } catch (e) {
      status.textContent = tr("failurePrefix") + (e && e.message || e);
    }
    return;
  }
  const tab = await getActiveTab();
  if (!tab || tab.id == null) { status.textContent = tr("noActiveTab"); return; }
  try {
    const pageContext = await getPageContext(tab.id);
    const wantsCaption = globalThis.lccRunModeIncludesCaption(settings.runMode);
    const wantsPage = globalThis.lccRunModeIncludesPage(settings.runMode);
    if (!wantsCaption && !wantsPage) { status.textContent = tr("chooseRunMode"); return; }
    const cleaned = await chrome.runtime.sendMessage({ type: "popup-cleanup" });   // release stale stream/DOM state before a fresh run
    if (cleaned && cleaned.ok === false) throw new Error(cleaned.error || tr("stopFailed"));
    if (wantsCaption) {
      let started;
      if (settings.videoDelay) {
        // B-2: delay.js captures the page <video> directly; routed via background so state+stop are tracked
        started = await chrome.runtime.sendMessage({ type: "popup-start-video", tabId: tab.id, delaySec: settings.delaySec, pageContext });
      } else {
        const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
        started = await chrome.runtime.sendMessage({ type: "popup-start", streamId, tabId: tab.id, delaySec: settings.delaySec, pageContext });
      }
      if (started && started.ok === false) throw new Error(started.error || tr("captionRunning"));
    }
    if (wantsPage) {
      const startedPage = await chrome.runtime.sendMessage({ type: "popup-start-page", tabId: tab.id, pageContext });
      if (startedPage && startedPage.ok === false) throw new Error(startedPage.error || tr("pageRunning"));
    }
    setState(true);
    if (wantsPage && wantsCaption) status.textContent = tr("pageCaptionRunning");
    else if (wantsPage) status.textContent = tr("pageRunning");
    else status.textContent = settings.videoDelay ? tr("videoMode") : tr("captureStarted");
  } catch (e) {
    status.textContent = tr("failurePrefix") + (e && e.message || e);
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
async function sendAsk(mode, question) {
  const res = document.getElementById("aiResult");
  if (!capturing) { res.textContent = tr("askNeedsCaption"); return; }
  res.textContent = (mode === "qa" ? tr("answering") : tr("summarizing"));
  try {
    const r = await chrome.storage.local.get("lcc-transcript");
    const transcript = (r["lcc-transcript"] || []).map((e) => e.source || e.ko).join(" ").slice(-8000);   // match server window; keep the control msg small
    if (!transcript.trim()) { res.textContent = tr("noTranscriptYet"); return; }
    await chrome.storage.session.remove("lcc-answer");
    const asked = await chrome.runtime.sendMessage({ type: "lcc-ask", mode: mode, question: question || "", transcript: transcript });
    if (asked && asked.ok === false) throw new Error(asked.error || tr("failurePrefix").trim());
  } catch (e) {
    res.textContent = tr("failurePrefix") + (e && e.message || e);
  }
}
document.getElementById("aiSum").onclick = () => sendAsk("summary", "");
const aiQ = document.getElementById("aiQ");
aiQ.addEventListener("keydown", (e) => { if (e.key === "Enter" && aiQ.value.trim()) { sendAsk("qa", aiQ.value.trim()); aiQ.value = ""; } });
document.getElementById("clearTr").onclick = async () => {
  const res = document.getElementById("aiResult");
  try {
    const tab = await getActiveTab();
    const cleared = await chrome.runtime.sendMessage({ type: "popup-clear-transcript", tabId: tab && tab.id });
    if (cleared && cleared.ok === false) throw new Error(cleared.error || tr("failurePrefix").trim());
    await chrome.storage.session.remove("lcc-answer");
    document.getElementById("hist").innerHTML = "";
    res.textContent = "";
  } catch (e) {
    res.textContent = tr("failurePrefix") + (e && e.message || e);
  }
};
document.getElementById("exportMd").onclick = async () => {
  const res = document.getElementById("aiResult");
  const r = await chrome.storage.local.get(["lcc-transcript", "lcc-session"]);
  const rows = r["lcc-transcript"] || [];
  if (!rows.length) { res.textContent = tr("noRecord"); return; }
  const sess = r["lcc-session"] || {};
  const start = sess.start || (rows[0] && rows[0].t) || 0;
  const out = ["# " + (sess.title || "Live Caption") + " — " + tr("transcriptTitleSuffix"), "", new Date().toLocaleString(), ""];
  for (const e of rows) out.push("**[" + lccFmtClock(e.t - start) + "]** " + e.source, "", "> " + e.ko, "");
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
const LCC_NM_HOSTS = [
  "io.github.teukboong.livecaption",
  "com.hesperides.livecaption",
];
function isNativeHostMissingError(message) {
  const text = String(message || "").toLowerCase();
  return text.includes("specified native messaging host not found") ||
    text.includes("no such native application");
}
function nmSend(msg) {
  return LCC_NM_HOSTS.reduce((chain, host) => {
    return chain.then((prev) => {
      if (prev && !prev.noHost) return prev;
      return nmSendOne(host, msg).then((resp) => {
        if (!resp.noHost) return resp;
        return { ...resp, error: resp.error || (prev && prev.error) };
      });
    });
  }, Promise.resolve(null)).then((resp) => resp || { ok: false, noHost: true, error: "native host unavailable" });
}
function nmSendOne(host, msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(host, msg, (resp) => {
        if (chrome.runtime.lastError) {
          const error = chrome.runtime.lastError.message;
          resolve({ ok: false, noHost: isNativeHostMissingError(error), error });
        }
        else resolve(resp || { ok: false, error: tr("emptyNativeResponse") });
      });
    } catch (e) { resolve({ ok: false, noHost: true, error: String(e) }); }
  });
}
const bridgeBtn = document.getElementById("bridgeBtn");
const bridgeStopBtn = document.getElementById("bridgeStopBtn");
const bridgeStatusEl = document.getElementById("bridgeStatus");
let bridgePoll = null;
let bridgePollBusy = false;
function bridgeErrorText(r, fallback) {
  return "" + ((r && (r.error || r.msg)) || fallback);
}
function setBridgeUI(state, text) {           // state: on | off | starting | nohost | blocked
  const errorState = state === "nohost" || state === "blocked";
  bridgeStatusEl.style.color = state === "on" ? "#16a34a" : (errorState ? "#dc2626" : "#666");
  if (text != null) bridgeStatusEl.textContent = text;
  bridgeBtn.disabled = (state === "starting");
  bridgeStopBtn.style.display = (state === "on" || state === "starting") ? "" : "none";
  bridgeBtn.textContent = state === "on" ? tr("bridgeStarted") : tr("bridgeStart");
}
function setBridgeStatusFromReply(r, loadingText) {
  if (r.noHost) { setBridgeUI("nohost", tr("noHost")); return false; }
  if (!r.ok || r.blocked) { setBridgeUI("blocked", bridgeErrorText(r, tr("failurePrefix").trim())); return false; }
  if (r.running) { setBridgeUI("on", tr("on") + (r.pid ? " (pid " + r.pid + ")" : "")); return true; }
  if (r.starting) { setBridgeUI("starting", loadingText || tr("starting")); return true; }
  setBridgeUI("off", tr("off"));
  return true;
}
async function refreshBridge() {
  const r = await nmSend({ cmd: "status" });
  setBridgeStatusFromReply(r);
}
function pollBridgeUntilUp(maxSec) {
  if (bridgePoll) clearInterval(bridgePoll);
  bridgePollBusy = false;
  let t = 0;
  bridgePoll = setInterval(async () => {
    if (bridgePollBusy) return;
    bridgePollBusy = true;
    try {
      t += 2;
      const r = await nmSend({ cmd: "status" });
      if (r.noHost || !r.ok || r.blocked) {
        clearInterval(bridgePoll); bridgePoll = null;
        setBridgeStatusFromReply(r);
      }
      else if (r.running) { clearInterval(bridgePoll); bridgePoll = null; setBridgeUI("on", tr("on")); }
      else if (t >= maxSec) { clearInterval(bridgePoll); bridgePoll = null; setBridgeUI("off", tr("noResponseLog")); }
      else setBridgeUI("starting", tr("startingWithSeconds", { seconds: t }));
    } finally {
      bridgePollBusy = false;
    }
  }, 2000);
}
bridgeBtn.onclick = async () => {
  setBridgeUI("starting", tr("startRequested"));
  const r = await nmSend({ cmd: "start", asrEngine: settings.asrEngine || "granite" });
  if (r.noHost) { setBridgeUI("nohost", tr("setupHost")); return; }
  if (!r.ok || r.blocked) { setBridgeUI("blocked", bridgeErrorText(r, tr("failurePrefix").trim())); return; }
  if (r.running) { setBridgeUI("on", tr("alreadyOn")); return; }
  if (r.starting) setBridgeUI("starting", r.msg || tr("starting"));
  pollBridgeUntilUp(70);
};
bridgeStopBtn.onclick = async () => {
  if (bridgePoll) { clearInterval(bridgePoll); bridgePoll = null; bridgePollBusy = false; }
  setBridgeUI("starting", tr("stopping"));
  const r = await nmSend({ cmd: "stop" });
  if (r.noHost) { setBridgeUI("nohost", tr("noHost")); return; }
  if (!r.ok || r.blocked) { setBridgeUI("blocked", bridgeErrorText(r, tr("stopFailed"))); return; }
  setBridgeUI(r.running ? "on" : "off", r.running ? tr("stopFailed") : tr("off"));
};
refreshBridge();

// ---- Simple / Advanced mode ----
function setMode(mode) {
  const adv = mode === "advanced";
  document.getElementById("adv").hidden = !adv;
  document.getElementById("modeAdv").classList.toggle("active", adv);
  document.getElementById("modeSimple").classList.toggle("active", !adv);
}
document.getElementById("modeSimple").onclick = () => { settings.uiMode = globalThis.lccCanonicalUiMode("simple"); setMode(settings.uiMode); saveSettings(); };
document.getElementById("modeAdv").onclick = () => { settings.uiMode = globalThis.lccCanonicalUiMode("advanced"); setMode(settings.uiMode); saveSettings(); };

// ---- advanced parameter controls (the raw knobs; ranges handled by the generic RANGES loop) ----
document.getElementById("latencyMode").addEventListener("change", (e) => {
  settings.latencyMode = globalThis.lccCanonicalLatencyMode(e.target.value);
  e.target.value = settings.latencyMode;
  saveSettings(true);
});
document.getElementById("register").addEventListener("change", (e) => {
  settings.register = globalThis.lccCanonicalRegister(e.target.value);
  e.target.value = settings.register;
  saveSettings(true, true);
});
document.getElementById("accuracyMode").addEventListener("change", (e) => { settings.accuracyMode = e.target.checked; saveSettings(); });
document.getElementById("autoPrime").addEventListener("change", (e) => { settings.autoPrime = e.target.checked; saveSettings(); });
document.getElementById("pageTranslateStream").addEventListener("change", (e) => {
  settings.pageTranslateStream = globalThis.lccCanonicalPageTranslateStream(e.target.value);
  e.target.value = settings.pageTranslateStream;
  saveSettings(true);
});
document.getElementById("pageBilingual").addEventListener("change", (e) => { settings.pageBilingual = e.target.checked; saveSettings(true); });
document.getElementById("pageVerify").addEventListener("change", (e) => { settings.pageVerify = e.target.checked; saveSettings(true); });
document.getElementById("debugSync").addEventListener("change", (e) => { settings.debugSync = e.target.checked; saveSettings(); });
document.getElementById("contextHint").addEventListener("input", (e) => { settings.contextHint = e.target.value; saveSettings(); pushBridgeConfigDebounced(400, true); });
document.getElementById("glossary").addEventListener("input", (e) => { settings.glossary = e.target.value; saveSettings(); pushBridgeConfigDebounced(400, true); });
document.getElementById("pageRegister").addEventListener("change", (e) => {
  settings.pageRegister = globalThis.lccCanonicalRegister(e.target.value);
  e.target.value = settings.pageRegister;
  saveSettings(true);
});
document.getElementById("pageContextHint").addEventListener("input", (e) => { settings.pageContextHint = e.target.value; saveSettings(); pushBridgeConfigDebounced(400, false); });
document.getElementById("pageGlossary").addEventListener("input", (e) => { settings.pageGlossary = e.target.value; saveSettings(); pushBridgeConfigDebounced(400, false); });

// ---- model install (full/mid/lite): native host spawns the downloader; poll progress ----
const TIER_LABEL = { full: "Full", mid: "Mid", lite: "Lite" };
let instPoll = null;
let instPollBusy = false;
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
  instPollBusy = false;
  setInstBusy(true);
  instPoll = setInterval(async () => {
    if (instPollBusy) return;
    instPollBusy = true;
    try {
      const r = await nmSend({ cmd: "install_status" });
      if (r.noHost) { clearInterval(instPoll); instPoll = null; setInstBusy(false); setInstStatus(tr("noHost"), "#dc2626"); return; }
      if (!r.ok) {
        clearInterval(instPoll); instPoll = null; setInstBusy(false);
        setInstStatus(tr("installFailed", { error: r.error || "" }), "#dc2626");
        return;
      }
      if (r.idle) {
        clearInterval(instPoll); instPoll = null; setInstBusy(false);
        setInstStatus(tr("installIdle"), "#999");
        return;
      }
      if (r.done) {
        clearInterval(instPoll); instPoll = null; setInstBusy(false);
        if (r.ok) setInstStatus(tr("installComplete", { tier: TIER_LABEL[r.tier] || r.tier || "" }), "#16a34a");
        else setInstStatus(tr("installFailed", { error: r.error || "" }), "#dc2626");
        return;
      }
      const n = (r.index || 0) + 1, t = r.total || "?";
      setInstStatus(tr("downloading", { name: r.current || tr("downloadingDefault"), index: n, total: t }), "#666");
    } finally {
      instPollBusy = false;
    }
  }, 2000);
}
async function startInstall(tier) {
  setInstBusy(true);
  setInstStatus(tr("installRequest", { tier: TIER_LABEL[tier] }), "#666");
  const r = await nmSend({ cmd: "install", tier });
  if (r.noHost) { setInstBusy(false); setInstStatus(tr("setupHost"), "#dc2626"); return; }
  if (!r.ok) { setInstBusy(false); setInstStatus("" + (r.error || tr("failurePrefix").trim()), "#dc2626"); return; }
  pollInstall();
}
document.getElementById("instFull").onclick = () => startInstall("full");
document.getElementById("instMid").onclick = () => startInstall("mid");
document.getElementById("instLite").onclick = () => startInstall("lite");
// reflect any in-progress / last install when the popup opens
nmSend({ cmd: "install_status" }).then((r) => {
  if (!r || r.idle || r.noHost) return;
  if (!r.done) pollInstall();
  else {
    setInstBusy(false);
    if (r.ok) setInstStatus(tr("installed", { tier: TIER_LABEL[r.tier] || r.tier || "" }), "#16a34a");
    else setInstStatus(tr("installFailed", { error: r.error || "" }), "#dc2626");
  }
});
window.addEventListener("pagehide", () => {
  if (_pushCfgTimer) { clearTimeout(_pushCfgTimer); _pushCfgTimer = null; }
  if (bridgePoll) { clearInterval(bridgePoll); bridgePoll = null; bridgePollBusy = false; }
  if (instPoll) { clearInterval(instPoll); instPoll = null; instPollBusy = false; }
}, { once: true });
