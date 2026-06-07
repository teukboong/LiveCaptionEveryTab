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
assert.equal(context.lccBuildBridgeConfig({ targetLang: "Hindi" }, "").targetLang, "Hindi");
assert.equal(context.lccBuildBridgeConfig({ targetLang: "hindi" }, "").targetLang, "Hindi");
assert.equal(Object.hasOwn(context.lccBuildBridgeConfig({ uiLang: "en" }, ""), "uiLang"), false);

const popupHtml = fs.readFileSync(path.join(root, "extension", "popup.html"), "utf8");
assert.match(popupHtml, /<select id="targetLang"><\/select>/, "popup target select is populated from protocol.js");
assert.match(popupHtml, /<select id="uiLang"><\/select>/, "popup UI-language select is populated from protocol.js");

console.log("test_protocol: OK (target/UI language settings stay canonical through protocol.js)");
