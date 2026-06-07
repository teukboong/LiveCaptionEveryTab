const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionRoot = __dirname;

function makeElement(idOrTag) {
  const children = [];
  const el = {
    id: idOrTag,
    tagName: String(idOrTag).toUpperCase(),
    value: "",
    checked: false,
    textContent: "",
    innerHTML: "",
    placeholder: "",
    title: "",
    hidden: false,
    disabled: false,
    className: "",
    style: {},
    dataset: {},
    options: [],
    listeners: {},
    classList: {
      toggle() {},
    },
    appendChild(child) {
      children.push(child);
      if (child && child.tagName === "OPTION") this.options.push(child);
      return child;
    },
    remove() {},
    click() {},
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
    querySelector() {
      return makeElement("query");
    },
  };
  return el;
}

function makeDocument() {
  const elements = new Map();
  const document = {
    documentElement: { lang: "" },
    body: makeElement("body"),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement(tag) {
      return makeElement(tag);
    },
    querySelectorAll() {
      return [];
    },
  };
  document.elements = elements;
  return document;
}

function flushMicrotasks() {
  return new Promise((resolve) => setImmediate(resolve));
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

async function runPopupClearTranscript(reply) {
  const document = makeDocument();
  const runtimeMessages = [];
  const sessionRemoved = [];
  const nativeMessages = [];

  const chrome = {
    runtime: {
      lastError: null,
      sendMessage(msg, cb) {
        runtimeMessages.push(msg);
        if (typeof cb === "function") {
          cb({ capturing: false, captioning: false, pageTranslating: false, wsOpen: false });
          return undefined;
        }
        if (msg && msg.type === "popup-clear-transcript") return Promise.resolve(reply);
        if (msg && msg.type === "popup-config-update") return Promise.resolve({ ok: true });
        return Promise.resolve({ ok: true });
      },
      sendNativeMessage(_host, msg, cb) {
        nativeMessages.push(msg);
        if (msg.cmd === "install_status") cb({ ok: true, idle: true });
        else if (msg.cmd === "status") cb({ ok: true, running: false });
        else cb({ ok: true });
      },
    },
    storage: {
      local: {
        get(keys) {
          if (keys === "lcc-settings") return Promise.resolve({ "lcc-settings": {} });
          if (Array.isArray(keys)) return Promise.resolve({ "lcc-transcript": [], "lcc-session": null });
          return Promise.resolve({});
        },
        set() { return Promise.resolve(); },
        remove() { return Promise.resolve(); },
      },
      session: {
        get() { return Promise.resolve({}); },
        remove(key) {
          sessionRemoved.push(key);
          return Promise.resolve();
        },
      },
      onChanged: {
        addListener() {},
      },
    },
    tabs: {
      query() { return Promise.resolve([{ id: 123 }]); },
      sendMessage() { return Promise.resolve({ context: "" }); },
    },
    tabCapture: {
      getMediaStreamId() { return Promise.resolve("stream-id"); },
    },
  };

  const context = {
    Blob: class Blob {
      constructor(parts, options) {
        this.parts = parts;
        this.options = options;
      }
    },
    URL: {
      createObjectURL() { return "blob:test"; },
      revokeObjectURL() {},
    },
    chrome,
    clearInterval() {},
    clearTimeout,
    console,
    document,
    setInterval() { return 1; },
    setTimeout,
    window: { addEventListener() {} },
  };
  context.globalThis = context;
  vm.runInNewContext(fs.readFileSync(path.join(extensionRoot, "protocol.js"), "utf8"), context, { filename: "protocol.js" });
  vm.runInNewContext(fs.readFileSync(path.join(extensionRoot, "popup.js"), "utf8"), context, { filename: "popup.js" });
  await flushMicrotasks();
  await flushMicrotasks();

  const hist = document.getElementById("hist");
  const aiResult = document.getElementById("aiResult");
  hist.innerHTML = "old transcript";
  aiResult.textContent = "old answer";

  await document.getElementById("clearTr").onclick();
  await flushMicrotasks();

  return { aiResult, hist, nativeMessages, runtimeMessages, sessionRemoved };
}

async function runBackgroundClearTranscript({ failLocalRemove = false } = {}) {
  let listener = null;
  const localRemoved = [];
  const sessionRemoved = [];
  const tabMessages = [];

  const chrome = {
    action: {
      setBadgeText() {},
      setBadgeBackgroundColor() {},
    },
    offscreen: {
      hasDocument() { return Promise.resolve(false); },
      closeDocument() { return Promise.resolve(); },
      createDocument() { return Promise.resolve(); },
    },
    runtime: {
      onMessage: {
        addListener(fn) {
          listener = fn;
        },
      },
      sendMessage() {
        return Promise.resolve({ ok: true });
      },
    },
    scripting: {
      executeScript() { return Promise.resolve(); },
      insertCSS() { return Promise.resolve(); },
    },
    storage: {
      local: {
        get() { return Promise.resolve({}); },
        remove(keys) {
          localRemoved.push(keys);
          if (failLocalRemove) return Promise.reject(new Error("local remove failed"));
          return Promise.resolve();
        },
      },
      session: {
        get() { return Promise.resolve({ capturedTabId: 456 }); },
        set() { return Promise.resolve(); },
        remove(key) {
          sessionRemoved.push(key);
          return Promise.resolve();
        },
      },
    },
    tabs: {
      get() { return Promise.resolve({ url: "https://example.test" }); },
      sendMessage(tabId, msg) {
        tabMessages.push({ tabId, msg });
        return Promise.resolve({ ok: true });
      },
      onRemoved: { addListener() {} },
      onReplaced: { addListener() {} },
      onUpdated: { addListener() {} },
    },
  };

  const context = { chrome, console: { error: console.error, log() {}, warn: console.warn } };
  context.globalThis = context;
  vm.createContext(context);
  context.importScripts = (...files) => {
    for (const file of files) {
      vm.runInContext(fs.readFileSync(path.join(extensionRoot, file), "utf8"), context, { filename: file });
    }
  };
  vm.runInContext(fs.readFileSync(path.join(extensionRoot, "background.js"), "utf8"), context, { filename: "background.js" });
  assert.equal(typeof listener, "function");

  const response = await new Promise((resolve) => {
    const keepsChannelOpen = listener({ type: "popup-clear-transcript", tabId: 123 }, {}, resolve);
    assert.equal(keepsChannelOpen, true);
  });

  return { localRemoved, response, sessionRemoved, tabMessages };
}

(async () => {
  const popupSuccess = await runPopupClearTranscript({ ok: true });
  assert.deepEqual(
    plain(popupSuccess.runtimeMessages.filter((msg) => msg && msg.type === "popup-clear-transcript")),
    [{ type: "popup-clear-transcript", tabId: 123 }],
  );
  assert.deepEqual(popupSuccess.sessionRemoved, ["lcc-answer"]);
  assert.equal(popupSuccess.hist.innerHTML, "");
  assert.equal(popupSuccess.aiResult.textContent, "");

  const popupFailure = await runPopupClearTranscript({ ok: false, error: "clear failed" });
  assert.deepEqual(popupFailure.sessionRemoved, []);
  assert.equal(popupFailure.hist.innerHTML, "old transcript");
  assert.equal(popupFailure.aiResult.textContent, "실패: clear failed");

  const backgroundSuccess = await runBackgroundClearTranscript();
  assert.deepEqual(plain(backgroundSuccess.localRemoved), [["lcc-transcript", "lcc-session"]]);
  assert.deepEqual(plain(backgroundSuccess.sessionRemoved), ["lcc-answer"]);
  assert.deepEqual(plain(backgroundSuccess.response), { ok: true });
  assert.deepEqual(plain(backgroundSuccess.tabMessages), [
    { tabId: 456, msg: { type: "transcript-clear" } },
    { tabId: 123, msg: { type: "transcript-clear" } },
  ]);

  const backgroundFailure = await runBackgroundClearTranscript({ failLocalRemove: true });
  assert.deepEqual(plain(backgroundFailure.response), { ok: false, error: "local remove failed" });
  assert.deepEqual(plain(backgroundFailure.sessionRemoved), []);
  assert.deepEqual(plain(backgroundFailure.tabMessages), []);

  console.log("test_extension_actions: OK (popup/background transcript clear success and failure paths pass)");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
