const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const context = { console };
context.globalThis = context;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "protocol.js"), "utf8"), context);

assert.ok(context.LCC_TARGET_LANGS.includes("Hindi"), "target list exposes Hindi");
assert.equal(JSON.stringify(context.LCC_UI_LANGS.map((lang) => lang.value)), JSON.stringify(["ko", "en"]));
assert.equal(JSON.stringify(context.LCC_ASR_ENGINES), JSON.stringify(["granite", "qwen3"]));
assert.equal(JSON.stringify(context.LCC_CONTENT_TYPES), JSON.stringify(["general", "conference", "news", "streaming"]));
assert.equal(JSON.stringify(context.LCC_LATENCY_MODES), JSON.stringify(["stable", "balanced", "aggressive"]));
assert.equal(context.lccCanonicalTargetLang("hindi"), "Hindi");
assert.equal(context.lccCanonicalUiLang("EN"), "en");
assert.equal(context.lccCanonicalAsrEngine("QWEN3"), "qwen3");
assert.equal(context.lccCanonicalAsrEngine("parakeet"), "granite");
assert.equal(context.lccCanonicalContentType("NEWS"), "news");
assert.equal(context.lccCanonicalContentType("documentary"), "general");
assert.equal(context.lccCanonicalRegister("LECTURE"), "lecture");
assert.equal(context.lccCanonicalRegister("formal"), "casual");
assert.equal(context.lccCanonicalLatencyMode("BALANCED"), "balanced");
assert.equal(context.lccCanonicalLatencyMode("fast"), "aggressive");
assert.equal(context.lccCanonicalLatencyMode("low-latency"), "aggressive");
assert.equal(context.lccCanonicalLatencyMode("low_latency"), "aggressive");
assert.equal(context.lccCanonicalLatencyMode("safe"), "stable");
assert.equal(context.lccCanonicalLatencyMode("turbo"), "aggressive");
assert.equal(context.lccNormalizeSettings({ targetLang: "hindi" }).targetLang, "Hindi");
assert.equal(context.lccNormalizeSettings({ uiLang: "EN" }).uiLang, "en");
assert.equal(context.lccNormalizeSettings({ asrEngine: "QWEN3" }).asrEngine, "qwen3");
assert.equal(context.lccNormalizeSettings({ asrEngine: "parakeet" }).asrEngine, "granite");
assert.equal(context.lccNormalizeSettings({ contentType: "NEWS" }).contentType, "news");
assert.equal(context.lccNormalizeSettings({ contentType: "documentary" }).contentType, "general");
assert.equal(context.lccNormalizeSettings({ contentType: "conference" }).register, "lecture");
assert.equal(context.lccNormalizeSettings({ contentType: "conference" }).latencyMode, "stable");
assert.equal(context.lccNormalizeSettings({ register: "NEWS" }).register, "news");
assert.equal(context.lccNormalizeSettings({ register: "formal" }).register, "casual");
assert.equal(context.lccNormalizeSettings({ pageRegister: "CHAT" }).pageRegister, "chat");
assert.equal(context.lccNormalizeSettings({ latencyMode: "BALANCED" }).latencyMode, "balanced");
assert.equal(context.lccNormalizeSettings({ latencyMode: "fast" }).latencyMode, "aggressive");
assert.equal(context.lccNormalizeSettings({ runMode: "page" }).runMode, "page");
assert.equal(context.lccNormalizeSettings({ runMode: "bogus" }).runMode, "video");
assert.equal(context.lccNormalizeSettings({ pageTranslateStream: "final" }).pageTranslateStream, "final");
assert.equal(context.lccNormalizeSettings({ pageTranslateStream: "bogus" }).pageTranslateStream, "partial");
assert.equal(context.lccNormalizeSettings({ pageBilingual: false }).pageBilingual, false);
assert.equal(context.lccNormalizeSettings({}).pageBilingual, true);
assert.equal(context.lccNormalizeSettings({ pageVerify: true }).pageVerify, true);
assert.equal(context.lccNormalizeSettings({}).pageVerify, false);
const clamped = context.lccNormalizeSettings({
  fontSize: "999",
  bottomPct: "-5",
  leftPct: "NaN",
  delaySec: "0",
  sentSilenceMs: "99999",
  vadLevel: "2.7",
  syncOffsetMs: "-9999",
  pageTranslateMinChars: "0",
  pageTranslateMaxChars: "99999",
});
assert.equal(clamped.fontSize, 44);
assert.equal(clamped.bottomPct, 2);
assert.equal(clamped.leftPct, context.LCC_DEFAULT_SETTINGS.leftPct);
assert.equal(clamped.delaySec, 0);
assert.equal(clamped.sentSilenceMs, 2500);
assert.equal(clamped.vadLevel, 3);
assert.equal(clamped.syncOffsetMs, -2000);
assert.equal(clamped.pageTranslateMinChars, 1);
assert.equal(clamped.pageTranslateMaxChars, 8000);
assert.equal(context.lccNormalizeSettings({ fontSize: "" }).fontSize, context.LCC_DEFAULT_SETTINGS.fontSize);
assert.equal(context.lccRunModeIncludesPage("page"), true);
assert.equal(context.lccRunModeIncludesCaption("page"), false);
assert.equal(context.lccRunModeIncludesPage("both"), true);
assert.equal(context.lccRunModeIncludesCaption("both"), true);
assert.equal(context.lccBuildBridgeConfig({ targetLang: "Hindi" }, "").targetLang, "Hindi");
assert.equal(context.lccBuildBridgeConfig({ targetLang: "hindi" }, "").targetLang, "Hindi");
assert.equal(context.lccBuildBridgeConfig({ asrEngine: "QWEN3" }, "").asrEngine, "qwen3");
assert.equal(context.lccBuildBridgeConfig({ asrEngine: "parakeet" }, "").asrEngine, "granite");
assert.equal(context.lccBuildBridgeConfig({ register: "NEWS" }, "").register, "news");
assert.equal(context.lccBuildBridgeConfig({ latencyMode: "fast" }, "").latencyMode, "aggressive");
assert.equal(context.lccBuildBridgeConfig({ vadLevel: "-7" }, "").vadLevel, 0);
assert.equal(context.lccBuildBridgeConfig({ sentSilenceMs: "99999" }, "").sentSilenceMs, 2500);
assert.equal(Object.hasOwn(context.lccBuildBridgeConfig({ uiLang: "en" }, ""), "uiLang"), false);
const pageCfg = context.lccBuildBridgeConfig({
  contextHint: "video terms",
  pageContextHint: "reddit thread",
  pageRegister: "chat",
  pageGlossary: "OP=원글쓴이",
}, "r/SipsTea");
assert.equal(pageCfg.pageRegister, "chat");
assert.match(pageCfg.pageContextHint, /reddit thread/);
assert.match(pageCfg.pageContextHint, /r\/SipsTea/);
assert.equal(pageCfg.pageGlossary, "OP=원글쓴이");
assert.equal(context.lccBuildBridgeConfig({ pageRegister: "bogus" }, "").pageRegister, "casual");
assert.equal(context.lccBuildBridgeConfig({ pageRegister: "CHAT" }, "").pageRegister, "chat");

const popupHtml = fs.readFileSync(path.join(root, "extension", "popup.html"), "utf8");
const popupJs = fs.readFileSync(path.join(root, "extension", "popup.js"), "utf8");
assert.match(popupHtml, /<select id="targetLang"><\/select>/, "popup target select is populated from protocol.js");
assert.match(popupHtml, /<select id="uiLang"><\/select>/, "popup UI-language select is populated from protocol.js");
assert.match(popupHtml, /id="pageTranslate"/, "popup exposes the page translation toggle");
assert.match(popupHtml, /id="captionTranslate"/, "popup exposes the video translation toggle");
assert.match(popupHtml, /id="pageContextHint"/, "popup exposes a page-only hint");
assert.match(popupHtml, /id="pageGlossary"/, "popup exposes a page-only glossary");
assert.match(popupHtml, /id="pageTranslateStream"/, "popup exposes page streaming mode");
assert.match(popupHtml, /id="pageBilingual"/, "popup exposes bilingual ghost toggle");
assert.match(popupHtml, /id="pageVerify"/, "popup exposes cache-then-verify toggle");
assert.match(
  popupJs,
  /if \(r\.idle\) \{[\s\S]*clearInterval\(instPoll\);[\s\S]*instPoll = null;[\s\S]*setInstBusy\(false\);/,
  "popup install polling unlocks buttons if native status goes idle",
);
assert.match(
  popupJs,
  /if \(!r\.ok\) \{[\s\S]*clearInterval\(instPoll\);[\s\S]*instPoll = null;[\s\S]*setInstBusy\(false\);[\s\S]*installFailed/,
  "popup install polling unlocks buttons if native status fails",
);
assert.match(
  popupJs,
  /if \(!r\.done\) pollInstall\(\);[\s\S]*else \{[\s\S]*setInstBusy\(false\);[\s\S]*if \(r\.ok\)[\s\S]*else setInstStatus\(tr\("installFailed"/,
  "popup install resume shows both completed and failed terminal status",
);

console.log("test_protocol: OK (target/UI language settings stay canonical through protocol.js)");
