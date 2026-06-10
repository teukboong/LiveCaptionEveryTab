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
  customPrompt: "",          // user custom translation prompt (advanced/preset); "" => default behavior
  pageContextHint: "",
  pageRegister: "casual",
  pageGlossary: "",
  runMode: "video",
  pageTranslateSelector: "body",
  pageTranslateMinChars: 2,
  pageTranslateMaxChars: 4000,
  pageTranslateStream: "partial",
  pageBilingual: true,
  pageBilingualInline: false,   // inline ghost: original kept visibly under translated prose blocks
  pageVerify: false,
  termMemory: true,          // session term memory + per-domain persistence (auto-glossary)
  writeBack: true,           // input write-back: translate my draft into the page's language on demand
  diarize: false,            // speaker tagging lite (bridge; model auto-downloads on first enable)
  pageOcr: false,            // image OCR translation (content-only; macOS bridge required)
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
const LCC_RUN_MODE_VALUES = Object.freeze(Object.keys(LCC_RUN_MODES));
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
const LCC_ASR_ENGINES = Object.freeze(["granite", "qwen3", "whisper"]);
const LCC_LATENCY_MODES = Object.freeze(["stable", "balanced", "aggressive"]);
const LCC_PAGE_TRANSLATE_STREAMS = Object.freeze(["partial", "final"]);
const LCC_UI_MODES = Object.freeze(["simple", "advanced"]);

function lccCanonicalLowerToken(value, allowed, fallback) {
  const raw = String(value || fallback || "").trim().toLowerCase();
  return allowed.includes(raw) ? raw : fallback;
}

function lccCanonicalBoolean(value, fallback) {
  if (value === true || value === false) return value;
  if (value == null || String(value).trim() === "") return !!fallback;
  const raw = String(value).trim().toLowerCase();
  if (["true", "1", "yes", "on"].includes(raw)) return true;
  if (["false", "0", "no", "off"].includes(raw)) return false;
  return !!fallback;
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
globalThis.LCC_RUN_MODE_VALUES = LCC_RUN_MODE_VALUES;
globalThis.LCC_CONTENT_PRESETS = LCC_CONTENT_PRESETS;
globalThis.LCC_CONTENT_TYPES = LCC_CONTENT_TYPES;
globalThis.LCC_REGISTERS = LCC_REGISTERS;
globalThis.LCC_ASR_ENGINES = LCC_ASR_ENGINES;
globalThis.LCC_LATENCY_MODES = LCC_LATENCY_MODES;
globalThis.LCC_PAGE_TRANSLATE_STREAMS = LCC_PAGE_TRANSLATE_STREAMS;
globalThis.LCC_UI_MODES = LCC_UI_MODES;
globalThis.lccCanonicalBoolean = lccCanonicalBoolean;
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
globalThis.lccCanonicalRunMode = function lccCanonicalRunMode(value, fallback = "video") {
  return lccCanonicalLowerToken(value, LCC_RUN_MODE_VALUES, fallback);
};
globalThis.lccCanonicalPageTranslateStream = function lccCanonicalPageTranslateStream(value, fallback = "partial") {
  return lccCanonicalLowerToken(value, LCC_PAGE_TRANSLATE_STREAMS, fallback);
};
globalThis.lccCanonicalUiMode = function lccCanonicalUiMode(value, fallback = "simple") {
  return lccCanonicalLowerToken(value, LCC_UI_MODES, fallback);
};
const LCC_CUSTOM_PROMPT_MAX = 4000;
globalThis.lccCanonicalCustomPrompt = function lccCanonicalCustomPrompt(value) {
  return String(value == null ? "" : value).slice(0, LCC_CUSTOM_PROMPT_MAX);
};
globalThis.lccBridgeHello = function lccBridgeHello(ws) {
  ws.send(JSON.stringify({ type: "hello", token: globalThis.LCC_WS_TOKEN }));
};
// ISO 639-1 (primary subtag) -> our canonical target-language names, for write-back direction detection
// from <html lang>. Pure data; unknown/empty codes return "".
const LCC_LANG_CODE_NAMES = Object.freeze({
  ko: "Korean", en: "English", ja: "Japanese", zh: "Chinese", es: "Spanish", fr: "French", de: "German",
  pt: "Portuguese", it: "Italian", ru: "Russian", nl: "Dutch", pl: "Polish", tr: "Turkish",
  vi: "Vietnamese", th: "Thai", id: "Indonesian", ar: "Arabic", hi: "Hindi", bn: "Bengali",
  uk: "Ukrainian", cs: "Czech", el: "Greek", he: "Hebrew", iw: "Hebrew", ro: "Romanian", hu: "Hungarian",
  sv: "Swedish", da: "Danish", no: "Norwegian", nb: "Norwegian", nn: "Norwegian", fi: "Finnish",
  fil: "Filipino", tl: "Filipino", ms: "Malay", ta: "Tamil", te: "Telugu", ur: "Urdu", fa: "Persian",
  sw: "Swahili", ca: "Catalan", hr: "Croatian", sk: "Slovak", bg: "Bulgarian", sr: "Serbian",
  lt: "Lithuanian", sl: "Slovenian", et: "Estonian", lv: "Latvian",
});
globalThis.lccLangNameFromCode = function lccLangNameFromCode(code) {
  const primary = String(code || "").trim().toLowerCase().split(/[-_]/)[0];
  return LCC_LANG_CODE_NAMES[primary] || "";
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
  out.runMode = globalThis.lccCanonicalRunMode(out.runMode);
  out.uiMode = globalThis.lccCanonicalUiMode(out.uiMode);
  out.showSource = lccCanonicalBoolean(out.showSource, LCC_DEFAULT_SETTINGS.showSource);
  out.videoDelay = lccCanonicalBoolean(out.videoDelay, LCC_DEFAULT_SETTINGS.videoDelay);
  out.accuracyMode = lccCanonicalBoolean(out.accuracyMode, LCC_DEFAULT_SETTINGS.accuracyMode);
  out.autoPrime = lccCanonicalBoolean(out.autoPrime, LCC_DEFAULT_SETTINGS.autoPrime);
  out.debugSync = lccCanonicalBoolean(out.debugSync, LCC_DEFAULT_SETTINGS.debugSync);
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
  out.pageTranslateStream = globalThis.lccCanonicalPageTranslateStream(out.pageTranslateStream);
  out.pageBilingual = lccCanonicalBoolean(out.pageBilingual, LCC_DEFAULT_SETTINGS.pageBilingual);
  out.pageBilingualInline = lccCanonicalBoolean(out.pageBilingualInline, LCC_DEFAULT_SETTINGS.pageBilingualInline);
  out.pageVerify = lccCanonicalBoolean(out.pageVerify, LCC_DEFAULT_SETTINGS.pageVerify);
  out.termMemory = lccCanonicalBoolean(out.termMemory, LCC_DEFAULT_SETTINGS.termMemory);
  out.writeBack = lccCanonicalBoolean(out.writeBack, LCC_DEFAULT_SETTINGS.writeBack);
  out.diarize = lccCanonicalBoolean(out.diarize, LCC_DEFAULT_SETTINGS.diarize);
  out.pageOcr = lccCanonicalBoolean(out.pageOcr, LCC_DEFAULT_SETTINGS.pageOcr);
  out.customPrompt = lccCanonicalCustomPrompt(out.customPrompt);
  return out;
};
globalThis.lccRunModeIncludesPage = function lccRunModeIncludesPage(mode) {
  const m = globalThis.lccCanonicalRunMode(mode);
  return !!(LCC_RUN_MODES[m] && LCC_RUN_MODES[m].page);
};
globalThis.lccRunModeIncludesCaption = function lccRunModeIncludesCaption(mode) {
  const m = globalThis.lccCanonicalRunMode(mode);
  return !!(LCC_RUN_MODES[m] && LCC_RUN_MODES[m].caption);
};
globalThis.lccBuildBridgeConfig = function lccBuildBridgeConfig(settings, pageContext) {
  const s = globalThis.lccNormalizeSettings(settings);
  const auto = s.autoPrime === true ? (pageContext || "") : "";
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
    customPrompt: lccCanonicalCustomPrompt(s.customPrompt),   // applies to caption + page translation in the bridge
    pageContextHint: pageHint,
    runMode: globalThis.lccCanonicalRunMode(s.runMode),                // content-only: lets the page translator pick the page vs both policy (it isn't a video tab)
    pageRegister: globalThis.lccCanonicalRegister(s.pageRegister),
    pageGlossary: s.pageGlossary || "",
    pageTranslateStream: globalThis.lccCanonicalPageTranslateStream(s.pageTranslateStream),   // content+offscreen read it; bridge ignores
    pageBilingual: s.pageBilingual === true,      // content-only: hover shows the original
    pageBilingualInline: s.pageBilingualInline === true,   // content-only: original kept under translated prose
    pageVerify: s.pageVerify === true,            // content-only: re-check cached labels in idle
    termMemory: s.termMemory === true,            // bridge: mine + auto-pin recurring terms
    diarize: s.diarize === true,                  // bridge: per-clause speaker tagging (CPU)
    autoGlossary: String(s.autoGlossary || ""),   // bridge: domain term seeds injected by the SW (not user-edited)

    accuracyMode: s.accuracyMode === true,
    autoPrime: s.autoPrime === true,
  };
};

// --- User translation presets (named, multiple) — SEPARATE from the built-in LCC_CONTENT_PRESETS ----------
// A user preset saves the full translation-shaping bundle so it can be recalled later, including from Simple.
// The actual persistence (browser storage key 'lcc-user-presets') lives in the popup; protocol.js owns the pure
// data model + canonicalization so popup / content / tests all share one source of truth.
const LCC_USER_PRESETS_KEY = "lcc-user-presets";
const LCC_PRESET_NAME_MAX = 60;
const LCC_MAX_USER_PRESETS = 50;
const LCC_PRESET_BUNDLE_KEYS = ["customPrompt", "register", "targetLang", "latencyMode", "contextHint", "glossary"];
globalThis.LCC_USER_PRESETS_KEY = LCC_USER_PRESETS_KEY;
globalThis.LCC_PRESET_BUNDLE_KEYS = LCC_PRESET_BUNDLE_KEYS;

globalThis.lccCanonicalPresetName = function lccCanonicalPresetName(name) {
  return String(name == null ? "" : name).replace(/\s+/g, " ").trim().slice(0, LCC_PRESET_NAME_MAX);
};
// The translation-shaping bundle pulled from a settings object, each field canonicalized.
globalThis.lccUserPresetBundle = function lccUserPresetBundle(settings) {
  const s = settings || {};
  return {
    customPrompt: globalThis.lccCanonicalCustomPrompt(s.customPrompt),
    register: globalThis.lccCanonicalRegister(s.register),
    targetLang: lccCanonicalTargetLang(s.targetLang),
    latencyMode: globalThis.lccCanonicalLatencyMode(s.latencyMode),
    contextHint: String(s.contextHint == null ? "" : s.contextHint).slice(0, 200),
    glossary: String(s.glossary == null ? "" : s.glossary).slice(0, LCC_CUSTOM_PROMPT_MAX),
  };
};
// One preset = { name, bundle }, fully canonicalized; null when the name is empty.
globalThis.lccNormalizeUserPreset = function lccNormalizeUserPreset(preset) {
  const name = globalThis.lccCanonicalPresetName(preset && preset.name);
  if (!name) return null;
  return { name, bundle: globalThis.lccUserPresetBundle((preset && preset.bundle) || {}) };
};
// Sanitize a stored array: drop invalid, dedupe by case-insensitive name (later wins), cap count.
globalThis.lccNormalizeUserPresets = function lccNormalizeUserPresets(list) {
  const out = [];
  const idx = new Map();
  for (const p of Array.isArray(list) ? list : []) {
    const norm = globalThis.lccNormalizeUserPreset(p);
    if (!norm) continue;
    const key = norm.name.toLowerCase();
    if (idx.has(key)) out[idx.get(key)] = norm;
    else { idx.set(key, out.length); out.push(norm); }
  }
  return out.slice(0, LCC_MAX_USER_PRESETS);
};
// Apply a preset's bundle onto a settings object (new object; only known bundle keys merged).
globalThis.lccApplyUserPreset = function lccApplyUserPreset(settings, preset) {
  const bundle = (preset && preset.bundle) || {};
  const merged = { ...(settings || {}) };
  for (const k of LCC_PRESET_BUNDLE_KEYS) {
    if (Object.hasOwn(bundle, k)) merged[k] = bundle[k];
  }
  return merged;
};
// Add or replace a named preset (case-insensitive). Returns a new normalized list.
globalThis.lccUpsertUserPreset = function lccUpsertUserPreset(list, name, bundle) {
  const norm = globalThis.lccNormalizeUserPreset({ name, bundle });
  if (!norm) return globalThis.lccNormalizeUserPresets(list);
  const kept = (Array.isArray(list) ? list : []).filter(
    (p) => globalThis.lccCanonicalPresetName(p && p.name).toLowerCase() !== norm.name.toLowerCase());
  return globalThis.lccNormalizeUserPresets([...kept, norm]);
};
// Remove a named preset (case-insensitive). Returns a new normalized list.
globalThis.lccDeleteUserPreset = function lccDeleteUserPreset(list, name) {
  const target = globalThis.lccCanonicalPresetName(name).toLowerCase();
  return globalThis.lccNormalizeUserPresets(
    (Array.isArray(list) ? list : []).filter(
      (p) => globalThis.lccCanonicalPresetName(p && p.name).toLowerCase() !== target));
};
// Look up a preset by name (case-insensitive); null if absent.
globalThis.lccFindUserPreset = function lccFindUserPreset(list, name) {
  const target = globalThis.lccCanonicalPresetName(name).toLowerCase();
  if (!target) return null;
  for (const p of globalThis.lccNormalizeUserPresets(list)) {
    if (p.name.toLowerCase() === target) return p;
  }
  return null;
};
