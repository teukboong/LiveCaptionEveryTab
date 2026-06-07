// Shared local bridge protocol constants. This token is not a secret against local users;
// it is a guard so ordinary web pages cannot accidentally drive the localhost bridge.
globalThis.LCC_BRIDGE_URL = "ws://127.0.0.1:8765";
globalThis.LCC_WS_TOKEN = "lcc-local-extension-v1";
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
  syncOffsetMs: 0,
  debugSync: false,
});
const LCC_CONTENT_PRESETS = Object.freeze({
  general: Object.freeze({ register: "casual", latencyMode: "aggressive" }),
  conference: Object.freeze({ register: "lecture", latencyMode: "stable" }),
  news: Object.freeze({ register: "news", latencyMode: "balanced" }),
  streaming: Object.freeze({ register: "chat", latencyMode: "aggressive" }),
});

globalThis.LCC_DEFAULT_SETTINGS = LCC_DEFAULT_SETTINGS;
globalThis.LCC_CONTENT_PRESETS = LCC_CONTENT_PRESETS;
globalThis.lccBridgeHello = function lccBridgeHello(ws) {
  ws.send(JSON.stringify({ type: "hello", token: globalThis.LCC_WS_TOKEN }));
};
globalThis.lccNormalizeSettings = function lccNormalizeSettings(settings) {
  const raw = settings || {};
  const out = { ...LCC_DEFAULT_SETTINGS, ...raw };
  const preset = LCC_CONTENT_PRESETS[out.contentType] || LCC_CONTENT_PRESETS.general;
  if (raw.register == null) out.register = preset.register;
  if (raw.latencyMode == null) out.latencyMode = preset.latencyMode;
  return out;
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
    targetLang: s.targetLang || "Korean",
    register: s.register || "casual",
    latencyMode: s.latencyMode || "aggressive",
    contextHint: hint,
    glossary: s.glossary || "",
    accuracyMode: s.accuracyMode ?? false,
    autoPrime: s.autoPrime ?? true,
  };
};
