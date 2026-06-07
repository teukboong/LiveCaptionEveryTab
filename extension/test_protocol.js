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
assert.equal(JSON.stringify(context.LCC_RUN_MODE_VALUES), JSON.stringify(["video", "page", "both"]));
assert.equal(JSON.stringify(context.LCC_CONTENT_TYPES), JSON.stringify(["general", "conference", "news", "streaming"]));
assert.equal(JSON.stringify(context.LCC_LATENCY_MODES), JSON.stringify(["stable", "balanced", "aggressive"]));
assert.equal(JSON.stringify(context.LCC_PAGE_TRANSLATE_STREAMS), JSON.stringify(["partial", "final"]));
assert.equal(JSON.stringify(context.LCC_UI_MODES), JSON.stringify(["simple", "advanced"]));
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
assert.equal(context.lccCanonicalRunMode(" BOTH "), "both");
assert.equal(context.lccCanonicalRunMode("dual"), "video");
assert.equal(context.lccCanonicalPageTranslateStream("FINAL"), "final");
assert.equal(context.lccCanonicalPageTranslateStream("streaming"), "partial");
assert.equal(context.lccCanonicalUiMode("ADVANCED"), "advanced");
assert.equal(context.lccCanonicalUiMode("expert"), "simple");
assert.equal(context.lccCanonicalBoolean("false", true), false);
assert.equal(context.lccCanonicalBoolean("ON", false), true);
assert.equal(context.lccCanonicalBoolean("maybe", true), true);
assert.equal(context.lccCanonicalBoolean("", false), false);
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
assert.equal(context.lccNormalizeSettings({ runMode: "PAGE" }).runMode, "page");
assert.equal(context.lccNormalizeSettings({ runMode: " BOTH " }).runMode, "both");
assert.equal(context.lccNormalizeSettings({ runMode: "bogus" }).runMode, "video");
assert.equal(context.lccNormalizeSettings({ pageTranslateStream: "FINAL" }).pageTranslateStream, "final");
assert.equal(context.lccNormalizeSettings({ pageTranslateStream: "bogus" }).pageTranslateStream, "partial");
assert.equal(context.lccNormalizeSettings({ uiMode: "ADVANCED" }).uiMode, "advanced");
assert.equal(context.lccNormalizeSettings({ uiMode: "expert" }).uiMode, "simple");
assert.equal(context.lccNormalizeSettings({ showSource: "false" }).showSource, false);
assert.equal(context.lccNormalizeSettings({ videoDelay: "1" }).videoDelay, true);
assert.equal(context.lccNormalizeSettings({ accuracyMode: "true" }).accuracyMode, true);
assert.equal(context.lccNormalizeSettings({ autoPrime: "off" }).autoPrime, false);
assert.equal(context.lccNormalizeSettings({ debugSync: "yes" }).debugSync, true);
assert.equal(context.lccNormalizeSettings({ pageBilingual: "false" }).pageBilingual, false);
assert.equal(context.lccNormalizeSettings({}).pageBilingual, true);
assert.equal(context.lccNormalizeSettings({ pageVerify: "true" }).pageVerify, true);
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
assert.equal(context.lccRunModeIncludesPage("BOTH"), true);
assert.equal(context.lccRunModeIncludesCaption("BOTH"), true);
assert.equal(context.lccBuildBridgeConfig({ targetLang: "Hindi" }, "").targetLang, "Hindi");
assert.equal(context.lccBuildBridgeConfig({ targetLang: "hindi" }, "").targetLang, "Hindi");
assert.equal(context.lccBuildBridgeConfig({ asrEngine: "QWEN3" }, "").asrEngine, "qwen3");
assert.equal(context.lccBuildBridgeConfig({ asrEngine: "parakeet" }, "").asrEngine, "granite");
assert.equal(context.lccBuildBridgeConfig({ register: "NEWS" }, "").register, "news");
assert.equal(context.lccBuildBridgeConfig({ latencyMode: "fast" }, "").latencyMode, "aggressive");
assert.equal(context.lccBuildBridgeConfig({ runMode: "BOTH" }, "").runMode, "both");
assert.equal(context.lccBuildBridgeConfig({ pageTranslateStream: "FINAL" }, "").pageTranslateStream, "final");
assert.equal(context.lccBuildBridgeConfig({ accuracyMode: "true" }, "").accuracyMode, true);
assert.equal(context.lccBuildBridgeConfig({ autoPrime: "false" }, "page title").autoPrime, false);
assert.equal(context.lccBuildBridgeConfig({ autoPrime: "false" }, "page title").contextHint, "");
assert.equal(context.lccBuildBridgeConfig({ pageBilingual: "false" }, "").pageBilingual, false);
assert.equal(context.lccBuildBridgeConfig({ pageVerify: "true" }, "").pageVerify, true);
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
const contentJs = fs.readFileSync(path.join(root, "extension", "content.js"), "utf8");
const backgroundJs = fs.readFileSync(path.join(root, "extension", "background.js"), "utf8");
const offscreenJs = fs.readFileSync(path.join(root, "extension", "offscreen.js"), "utf8");
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
assert.match(
  popupJs,
  /status\.textContent = tr\("stopping"\);[\s\S]*const stopped = await chrome\.runtime\.sendMessage\(\{ type: "popup-stop" \}\);[\s\S]*if \(stopped && stopped\.ok === false\) throw new Error/,
  "popup waits for stop acknowledgement before showing stopped",
);
assert.match(
  popupJs,
  /async function sendAsk\(mode, question\) \{[\s\S]*const asked = await chrome\.runtime\.sendMessage\(\{ type: "lcc-ask", mode: mode, question: question \|\| "", transcript: transcript \}\);[\s\S]*if \(asked && asked\.ok === false\) throw new Error/,
  "popup waits for AI request acknowledgement before leaving the pending state",
);
assert.match(
  popupJs,
  /async function pushBridgeConfigNow\(resetTranslationContext = false\) \{[\s\S]*const pushed = await chrome\.runtime\.sendMessage\(\{ type: "popup-config-update", resetTranslationContext \}\);[\s\S]*if \(pushed && pushed\.ok === false\) throw new Error/,
  "popup waits for live config push acknowledgement",
);
assert.match(
  popupJs,
  /_pushCfgTimer = setTimeout\(async \(\) => \{[\s\S]*await pushBridgeConfigNow\(resetTranslationContext\);[\s\S]*status\.textContent = tr\("failurePrefix"\) \+ \(e && e\.message \|\| e\);/,
  "popup reports debounced live config push failures",
);
assert.match(
  popupJs,
  /const cleaned = await chrome\.runtime\.sendMessage\(\{ type: "popup-cleanup" \}\);[\s\S]*if \(cleaned && cleaned\.ok === false\) throw new Error/,
  "popup waits for stale cleanup acknowledgement before starting",
);
assert.match(
  contentJs,
  /let settings = globalThis\.lccNormalizeSettings\(\{\}\);/,
  "content overlay starts from canonical shared defaults",
);
assert.match(
  contentJs,
  /settings = globalThis\.lccNormalizeSettings\(\{ \.\.\.settings, \.\.\.r\["lcc-settings"\] \}\);/,
  "content overlay normalizes stored settings before applying them",
);
assert.match(
  contentJs,
  /settings = globalThis\.lccNormalizeSettings\(\{ \.\.\.settings, \.\.\.ch\["lcc-settings"\]\.newValue \}\);/,
  "content overlay normalizes live setting changes before applying them",
);
assert.match(
  contentJs,
  /const pushed = await chrome\.runtime\.sendMessage\(\{ type: "popup-config-update", resetTranslationContext: false \}\);[\s\S]*if \(pushed && pushed\.ok === false\) throw new Error/,
  "content glossary waits for live config push acknowledgement",
);
assert.match(
  contentJs,
  /const res = await lccAddGlossary\(src\.value, tgt\.value\);[\s\S]*if \(res\.ok\)[\s\S]*else \{ msg\.textContent = res\.error \|\| "원문·번역 둘 다 필요"; \}/,
  "content glossary reports live config push failures",
);
assert.match(
  backgroundJs,
  /async function ensureContentScript\(tabId\) \{[\s\S]*return false;[\s\S]*return true;[\s\S]*return false;[\s\S]*\}/,
  "background content-script injection reports success or failure",
);
assert.match(
  backgroundJs,
  /function requireContentScript\(ok\) \{[\s\S]*throw new Error\("이 탭에는 확장 스크립트를 주입할 수 없어요\./,
  "background reports unsupported tabs before claiming a run started",
);
assert.match(
  backgroundJs,
  /requireContentScript\(await ensureContentScript\(tabId\)\);[\s\S]*const dsec = Math\.min\(12, Math\.max\(0, Number\(delaySec\) \|\| 0\)\);/,
  "audio start requires content-script injection before session state",
);
assert.match(
  backgroundJs,
  /requireContentScript\(await ensureContentScript\(tabId\)\);[\s\S]*const dsec = Math\.min\(12, Math\.max\(0\.5, Number\(delaySec\) \|\| 3\.5\)\);/,
  "video start requires content-script injection before session state",
);
assert.match(
  backgroundJs,
  /requireContentScript\(await ensureContentScript\(tabId\)\);[\s\S]*const config = await bridgeConfig\(\);/,
  "page start requires content-script injection before session state",
);
assert.match(
  backgroundJs,
  /if \(msg\.type === "popup-stop"\) \{[\s\S]*cleanup\(\)[\s\S]*sendResponse\(\{ ok: true \}\)[\s\S]*sendResponse\(\{ ok: false, error: String\(e && e\.message \|\| e\) \}\)[\s\S]*return true;/,
  "background acknowledges popup stop success or failure",
);
assert.match(
  backgroundJs,
  /if \(msg\.type === "popup-config-update"\) \{[\s\S]*if \(!captioning && !pageTranslating\) return \{ ok: true, applied: false \};[\s\S]*await ensureOffscreen\(\);[\s\S]*cmd: "config", config[\s\S]*return \{ ok: true, applied: true \};[\s\S]*sendResponse\(res \|\| \{ ok: true \}\)[\s\S]*return true;/,
  "background acknowledges live config update success or failure",
);
assert.match(
  backgroundJs,
  /if \(msg\.type === "lcc-ask"\) \{[\s\S]*chrome\.runtime\.sendMessage\(\{ target: "offscreen", cmd: "ask"[\s\S]*sendResponse\(res && res\.ok === false \? res : \{ ok: true \}\)[\s\S]*sendResponse\(\{ ok: false, error: String\(e && e\.message \|\| e\) \}\)[\s\S]*return true;/,
  "background acknowledges AI request delivery failures",
);
assert.match(
  backgroundJs,
  /if \(msg\.type === "page-translate-batch"\) \{[\s\S]*chrome\.storage\.session\.get\(\["pageTranslating", "pageTabId"\]\)[\s\S]*if \(!pageTranslating \|\| tabId == null \|\| pageTabId !== tabId\) return null;[\s\S]*cmd: "dom-translate-batch"/,
  "background routes page translation batches only for the active page tab",
);
assert.match(
  offscreenJs,
  /else if \(msg\.cmd === "ask"\) \{[\s\S]*sendResponse\(\{ ok: true \}\);[\s\S]*sendResponse\(\{ ok: false, error: String\(e && e\.message \|\| e\) \}\);[\s\S]*\}/,
  "offscreen acknowledges AI request handling",
);
assert.match(
  offscreenJs,
  /else if \(msg\.cmd === "config"\) \{[\s\S]*sendBridgeConfig\(\);[\s\S]*sendResponse\(\{ ok: true \}\);[\s\S]*sendResponse\(\{ ok: false, error: String\(e && e\.message \|\| e\) \}\);[\s\S]*\}/,
  "offscreen acknowledges live config updates",
);
assert.match(
  offscreenJs,
  /function queueOrSendDomBatch\(msg\) \{[\s\S]*if \(!msg\.requestId \|\| !Array\.isArray\(msg\.items\) \|\| !msg\.items\.length\) return;[\s\S]*if \(pageActive && lccWsCanSendControl\(\) && domBatchQueue\.length === 0\)/,
  "offscreen preserves page translation batches while page mode is warming",
);
assert.match(
  offscreenJs,
  /function flushDomBatches\(\) \{[\s\S]*if \(!pageActive \|\| !lccWsCanSendControl\(\) \|\| !domBatchQueue\.length\) return;/,
  "offscreen flushes page translation batches only after page mode is active",
);

console.log("test_protocol: OK (target/UI language settings stay canonical through protocol.js)");
