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
assert.equal(context.lccCanonicalTargetLang("hindi"), "Hindi");
assert.equal(context.lccCanonicalUiLang("EN"), "en");
assert.equal(context.lccNormalizeSettings({ targetLang: "hindi" }).targetLang, "Hindi");
assert.equal(context.lccNormalizeSettings({ uiLang: "EN" }).uiLang, "en");
assert.equal(context.lccNormalizeSettings({ runMode: "page" }).runMode, "page");
assert.equal(context.lccNormalizeSettings({ runMode: "bogus" }).runMode, "video");
assert.equal(context.lccNormalizeSettings({ pageTranslateStream: "final" }).pageTranslateStream, "final");
assert.equal(context.lccNormalizeSettings({ pageTranslateStream: "bogus" }).pageTranslateStream, "partial");
assert.equal(context.lccNormalizeSettings({ pageBilingual: false }).pageBilingual, false);
assert.equal(context.lccNormalizeSettings({}).pageBilingual, true);
assert.equal(context.lccNormalizeSettings({ pageVerify: true }).pageVerify, true);
assert.equal(context.lccNormalizeSettings({}).pageVerify, false);
assert.equal(context.lccRunModeIncludesPage("page"), true);
assert.equal(context.lccRunModeIncludesCaption("page"), false);
assert.equal(context.lccRunModeIncludesPage("both"), true);
assert.equal(context.lccRunModeIncludesCaption("both"), true);
assert.equal(context.lccBuildBridgeConfig({ targetLang: "Hindi" }, "").targetLang, "Hindi");
assert.equal(context.lccBuildBridgeConfig({ targetLang: "hindi" }, "").targetLang, "Hindi");
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

const popupHtml = fs.readFileSync(path.join(root, "extension", "popup.html"), "utf8");
assert.match(popupHtml, /<select id="targetLang"><\/select>/, "popup target select is populated from protocol.js");
assert.match(popupHtml, /<select id="uiLang"><\/select>/, "popup UI-language select is populated from protocol.js");
assert.match(popupHtml, /id="pageTranslate"/, "popup exposes the page translation toggle");
assert.match(popupHtml, /id="captionTranslate"/, "popup exposes the video translation toggle");
assert.match(popupHtml, /id="pageContextHint"/, "popup exposes a page-only hint");
assert.match(popupHtml, /id="pageGlossary"/, "popup exposes a page-only glossary");
assert.match(popupHtml, /id="pageTranslateStream"/, "popup exposes page streaming mode");
assert.match(popupHtml, /id="pageBilingual"/, "popup exposes bilingual ghost toggle");
assert.match(popupHtml, /id="pageVerify"/, "popup exposes cache-then-verify toggle");

console.log("test_protocol: OK (target/UI language settings stay canonical through protocol.js)");
