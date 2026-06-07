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
  runMode: "video",
  pageTranslateSelector: "body",
  pageTranslateMinChars: 2,
  pageTranslateMaxChars: 900,
  syncOffsetMs: 0,
  debugSync: false,
  uiMode: "simple",
  uiLang: "ko",
});
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
const LCC_UI_LANGS = Object.freeze([
  Object.freeze({ value: "ko", label: "한국어" }),
  Object.freeze({ value: "en", label: "English" }),
]);

function lccCanonicalTargetLang(value, fallback = "Korean") {
  const raw = String(value || fallback || "Korean").trim().toLowerCase();
  return LCC_TARGET_LANGS.find((lang) => lang.toLowerCase() === raw) || fallback;
}

globalThis.LCC_TARGET_LANGS = LCC_TARGET_LANGS;
globalThis.LCC_UI_LANGS = LCC_UI_LANGS;
globalThis.LCC_DEFAULT_SETTINGS = LCC_DEFAULT_SETTINGS;
globalThis.LCC_RUN_MODES = LCC_RUN_MODES;
globalThis.LCC_CONTENT_PRESETS = LCC_CONTENT_PRESETS;
globalThis.lccCanonicalTargetLang = lccCanonicalTargetLang;
globalThis.lccCanonicalUiLang = function lccCanonicalUiLang(value, fallback = "ko") {
  const raw = String(value || fallback || "ko").trim().toLowerCase();
  return LCC_UI_LANGS.some((lang) => lang.value === raw) ? raw : fallback;
};
globalThis.lccBridgeHello = function lccBridgeHello(ws) {
  ws.send(JSON.stringify({ type: "hello", token: globalThis.LCC_WS_TOKEN }));
};
globalThis.lccNormalizeSettings = function lccNormalizeSettings(settings) {
  const raw = settings || {};
  const out = { ...LCC_DEFAULT_SETTINGS, ...raw };
  const preset = LCC_CONTENT_PRESETS[out.contentType] || LCC_CONTENT_PRESETS.general;
  if (raw.register == null) out.register = preset.register;
  if (raw.latencyMode == null) out.latencyMode = preset.latencyMode;
  out.targetLang = lccCanonicalTargetLang(out.targetLang);
  out.uiLang = globalThis.lccCanonicalUiLang(out.uiLang);
  out.runMode = LCC_RUN_MODES[out.runMode] ? out.runMode : LCC_DEFAULT_SETTINGS.runMode;
  out.pageTranslateSelector = String(out.pageTranslateSelector || LCC_DEFAULT_SETTINGS.pageTranslateSelector).trim() || "body";
  out.pageTranslateMinChars = Math.max(1, Math.min(80, Number(out.pageTranslateMinChars) || LCC_DEFAULT_SETTINGS.pageTranslateMinChars));
  out.pageTranslateMaxChars = Math.max(80, Math.min(2000, Number(out.pageTranslateMaxChars) || LCC_DEFAULT_SETTINGS.pageTranslateMaxChars));
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
  return {
    type: "config",
    asrEngine: s.asrEngine || "granite",
    vadLevel: s.vadLevel ?? 2,
    sentSilenceMs: s.sentSilenceMs ?? 1300,
    targetLang: lccCanonicalTargetLang(s.targetLang),
    register: s.register || "casual",
    latencyMode: s.latencyMode || "aggressive",
    contextHint: hint,
    glossary: s.glossary || "",
    accuracyMode: s.accuracyMode ?? false,
    autoPrime: s.autoPrime ?? true,
  };
};
