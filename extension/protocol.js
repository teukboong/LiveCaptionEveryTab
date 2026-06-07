// Shared local bridge protocol constants. This token is not a secret against local users;
// it is a guard so ordinary web pages cannot accidentally drive the localhost bridge.
globalThis.LCC_BRIDGE_URL = "ws://127.0.0.1:8765";
globalThis.LCC_WS_TOKEN = "lcc-local-extension-v1";
const LCC_TARGET_LANGS = Object.freeze([
  "Korean", "English", "Japanese", "Chinese", "Spanish", "French", "German", "Portuguese", "Italian",
  "Russian", "Dutch", "Polish", "Turkish", "Vietnamese", "Thai", "Indonesian", "Arabic", "Hindi",
  "Bengali", "Ukrainian", "Czech", "Greek", "Hebrew", "Romanian", "Hungarian", "Swedish", "Danish",
  "Norwegian", "Finnish", "Filipino", "Malay", "Tamil", "Telugu", "Urdu", "Persian", "Swahili",
  "Catalan", "Croatian", "Slovak", "Bulgarian", "Serbian", "Lithuanian", "Slovenian", "Estonian", "Latvian",
]);
const LCC_DEFAULT_SETTINGS = Object.freeze({
  fontSize: 25,
  bottomPct: 12,
  leftPct: 50,
  showSource: true,
  delaySec: 3.5,
  videoDelay: false,
  targetLang: "Korean",
  asrEngine: "granite",
  contentType: "general",
  register: "casual",
  latencyMode: "aggressive",
  sentSilenceMs: 1300,
  vadLevel: 2,
  accuracyMode: false,
  autoPrime: true,
  contextHint: "",
  glossary: "",
  pageContextHint: "",
  pageRegister: "casual",
  pageGlossary: "",
  runMode: "video",
  pageTranslateSelector: "body",
  pageTranslateMinChars: 2,
  pageTranslateMaxChars: 4000,
  pageTranslateStream: "partial",
  pageBilingual: true,
  pageVerify: false,
  syncOffsetMs: 0,
  debugSync: false,
  uiMode: "simple",
  uiLang: "ko",
});
const LCC_REGISTERS = Object.freeze(["casual", "lecture", "news", "chat"]);
const LCC_RUN_MODES = Object.freeze({
  video: Object.freeze({ page: false, caption: true }),
  page: Object.freeze({ page: true, caption: false }),
  both: Object.freeze({ page: true, caption: true }),
});
const LCC_CONTENT_PRESETS = Object.freeze({
  general: Object.freeze({ register: "casual", latencyMode: "aggressive" }),
  conference: Object.freeze({ register: "lecture", latencyMode: "stable" }),
  news: Object.freeze({ register: "news", latencyMode: "balanced" }),
  streaming: Object.freeze({ register: "chat", latencyMode: "aggressive" }),
});
const LCC_CONTENT_TYPES = Object.freeze(Object.keys(LCC_CONTENT_PRESETS));
const LCC_UI_LANGS = Object.freeze([
  Object.freeze({ value: "ko", label: "한국어" }),
  Object.freeze({ value: "en", label: "English" }),
]);
const LCC_ASR_ENGINES = Object.freeze(["granite", "qwen3"]);
const LCC_LATENCY_MODES = Object.freeze(["stable", "balanced", "aggressive"]);

function lccCanonicalLowerToken(value, allowed, fallback) {
  const raw = String(value || fallback || "").trim().toLowerCase();
  return allowed.includes(raw) ? raw : fallback;
}

function lccClampNumber(value, fallback, min, max) {
  const n = (value == null || String(value).trim() === "") ? Number(fallback) : Number(value);
  const safe = Number.isFinite(n) ? n : fallback;
  return Math.max(min, Math.min(max, safe));
}

function lccClampInteger(value, fallback, min, max) {
  return Math.round(lccClampNumber(value, fallback, min, max));
}

function lccCanonicalTargetLang(value, fallback = "Korean") {
  const raw = String(value || fallback || "Korean").trim().toLowerCase();
  return LCC_TARGET_LANGS.find((lang) => lang.toLowerCase() === raw) || fallback;
}

globalThis.LCC_TARGET_LANGS = LCC_TARGET_LANGS;
globalThis.LCC_UI_LANGS = LCC_UI_LANGS;
globalThis.LCC_DEFAULT_SETTINGS = LCC_DEFAULT_SETTINGS;
globalThis.LCC_RUN_MODES = LCC_RUN_MODES;
globalThis.LCC_CONTENT_PRESETS = LCC_CONTENT_PRESETS;
globalThis.LCC_CONTENT_TYPES = LCC_CONTENT_TYPES;
globalThis.LCC_REGISTERS = LCC_REGISTERS;
globalThis.LCC_ASR_ENGINES = LCC_ASR_ENGINES;
globalThis.LCC_LATENCY_MODES = LCC_LATENCY_MODES;
globalThis.lccCanonicalTargetLang = lccCanonicalTargetLang;
globalThis.lccCanonicalUiLang = function lccCanonicalUiLang(value, fallback = "ko") {
  return lccCanonicalLowerToken(value, LCC_UI_LANGS.map((lang) => lang.value), fallback);
};
globalThis.lccCanonicalAsrEngine = function lccCanonicalAsrEngine(value, fallback = "granite") {
  return lccCanonicalLowerToken(value, LCC_ASR_ENGINES, fallback);
};
globalThis.lccCanonicalContentType = function lccCanonicalContentType(value, fallback = "general") {
  return lccCanonicalLowerToken(value, LCC_CONTENT_TYPES, fallback);
};
globalThis.lccCanonicalRegister = function lccCanonicalRegister(value, fallback = "casual") {
  return lccCanonicalLowerToken(value, LCC_REGISTERS, fallback);
};
globalThis.lccCanonicalLatencyMode = function lccCanonicalLatencyMode(value, fallback = "aggressive") {
  const aliases = {
    fast: "aggressive",
    low: "aggressive",
    "low-latency": "aggressive",
    low_latency: "aggressive",
    safe: "stable",
    quality: "stable",
  };
  const raw = String(value || fallback || "aggressive").trim().toLowerCase();
  return lccCanonicalLowerToken(aliases[raw] || raw, LCC_LATENCY_MODES, fallback);
};
globalThis.lccBridgeHello = function lccBridgeHello(ws) {
  ws.send(JSON.stringify({ type: "hello", token: globalThis.LCC_WS_TOKEN }));
};
globalThis.lccNormalizeSettings = function lccNormalizeSettings(settings) {
  const raw = settings || {};
  const out = { ...LCC_DEFAULT_SETTINGS, ...raw };
  out.contentType = globalThis.lccCanonicalContentType(out.contentType);
  const preset = LCC_CONTENT_PRESETS[out.contentType] || LCC_CONTENT_PRESETS.general;
  if (raw.register == null) out.register = preset.register;
  if (raw.latencyMode == null) out.latencyMode = preset.latencyMode;
  out.targetLang = lccCanonicalTargetLang(out.targetLang);
  out.uiLang = globalThis.lccCanonicalUiLang(out.uiLang);
  out.asrEngine = globalThis.lccCanonicalAsrEngine(out.asrEngine);
  out.register = globalThis.lccCanonicalRegister(out.register);
  out.pageRegister = globalThis.lccCanonicalRegister(out.pageRegister, LCC_DEFAULT_SETTINGS.pageRegister);
  out.latencyMode = globalThis.lccCanonicalLatencyMode(out.latencyMode);
  out.runMode = LCC_RUN_MODES[out.runMode] ? out.runMode : LCC_DEFAULT_SETTINGS.runMode;
  out.fontSize = lccClampNumber(out.fontSize, LCC_DEFAULT_SETTINGS.fontSize, 14, 44);
  out.bottomPct = lccClampNumber(out.bottomPct, LCC_DEFAULT_SETTINGS.bottomPct, 2, 80);
  out.leftPct = lccClampNumber(out.leftPct, LCC_DEFAULT_SETTINGS.leftPct, 5, 95);
  out.delaySec = lccClampNumber(out.delaySec, LCC_DEFAULT_SETTINGS.delaySec, 0, 12);
  out.sentSilenceMs = lccClampInteger(out.sentSilenceMs, LCC_DEFAULT_SETTINGS.sentSilenceMs, 500, 2500);
  out.vadLevel = lccClampInteger(out.vadLevel, LCC_DEFAULT_SETTINGS.vadLevel, 0, 3);
  out.syncOffsetMs = lccClampInteger(out.syncOffsetMs, LCC_DEFAULT_SETTINGS.syncOffsetMs, -2000, 2000);
  out.pageTranslateSelector = String(out.pageTranslateSelector || LCC_DEFAULT_SETTINGS.pageTranslateSelector).trim() || "body";
  out.pageTranslateMinChars = lccClampInteger(out.pageTranslateMinChars, LCC_DEFAULT_SETTINGS.pageTranslateMinChars, 1, 80);
  out.pageTranslateMaxChars = lccClampInteger(out.pageTranslateMaxChars, LCC_DEFAULT_SETTINGS.pageTranslateMaxChars, 80, 8000);
  out.pageTranslateStream = (out.pageTranslateStream === "final") ? "final" : "partial";
  out.pageBilingual = out.pageBilingual !== false;
  out.pageVerify = out.pageVerify === true;
  return out;
};
globalThis.lccRunModeIncludesPage = function lccRunModeIncludesPage(mode) {
  return !!(LCC_RUN_MODES[mode] && LCC_RUN_MODES[mode].page);
};
globalThis.lccRunModeIncludesCaption = function lccRunModeIncludesCaption(mode) {
  return !!(LCC_RUN_MODES[mode] && LCC_RUN_MODES[mode].caption);
};
globalThis.lccBuildBridgeConfig = function lccBuildBridgeConfig(settings, pageContext) {
  const s = globalThis.lccNormalizeSettings(settings);
  const auto = (s.autoPrime ?? true) ? (pageContext || "") : "";
  const hint = [s.contextHint || "", auto].filter(Boolean).join("; ").slice(0, 200);
  const pageHint = [s.pageContextHint || s.contextHint || "", auto].filter(Boolean).join("; ").slice(0, 240);
  return {
    type: "config",
    asrEngine: globalThis.lccCanonicalAsrEngine(s.asrEngine),
    vadLevel: s.vadLevel ?? 2,
    sentSilenceMs: s.sentSilenceMs ?? 1300,
    targetLang: lccCanonicalTargetLang(s.targetLang),
    register: globalThis.lccCanonicalRegister(s.register),
    latencyMode: globalThis.lccCanonicalLatencyMode(s.latencyMode),
    contextHint: hint,
    glossary: s.glossary || "",
    pageContextHint: pageHint,
    runMode: s.runMode || "video",                // content-only: lets the page translator pick the page vs both policy (it isn't a video tab)
    pageRegister: globalThis.lccCanonicalRegister(s.pageRegister),
    pageGlossary: s.pageGlossary || "",
    pageTranslateStream: (s.pageTranslateStream === "final") ? "final" : "partial",   // content+offscreen read it; bridge ignores
    pageBilingual: s.pageBilingual !== false,     // content-only: hover shows the original
    pageVerify: s.pageVerify === true,            // content-only: re-check cached labels in idle

    accuracyMode: s.accuracyMode ?? false,
    autoPrime: s.autoPrime ?? true,
  };
};
