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

async function loadPopup({ runtimeReplies = {}, settings = {}, activeTab = { id: 123 } } = {}) {
  const document = makeDocument();
  const runtimeMessages = [];
  const streamRequests = [];
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
        if (msg && Object.hasOwn(runtimeReplies, msg.type)) return Promise.resolve(runtimeReplies[msg.type]);
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
          if (keys === "lcc-settings") return Promise.resolve({ "lcc-settings": settings });
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
      query() { return Promise.resolve(activeTab ? [activeTab] : []); },
      sendMessage() { return Promise.resolve({ context: "" }); },
    },
    tabCapture: {
      getMediaStreamId(request) {
        streamRequests.push(request);
        return Promise.resolve("stream-id");
      },
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

  return { chrome, document, nativeMessages, runtimeMessages, sessionRemoved, streamRequests };
}

async function runPopupClearTranscript(reply) {
  const popup = await loadPopup({ runtimeReplies: { "popup-clear-transcript": reply } });

  const hist = popup.document.getElementById("hist");
  const aiResult = popup.document.getElementById("aiResult");
  hist.innerHTML = "old transcript";
  aiResult.textContent = "old answer";

  await popup.document.getElementById("clearTr").onclick();
  await flushMicrotasks();

  return { ...popup, aiResult, hist };
}

async function runPopupStartWithCleanup(reply) {
  const popup = await loadPopup({ runtimeReplies: { "popup-cleanup": reply } });
  await popup.document.getElementById("btn").onclick();
  await flushMicrotasks();
  return popup;
}

function loadBackgroundHarness({
  failLocalRemove = false,
  failOffscreenClose = false,
  failOffscreenMessage = false,
  failOffscreenPcm = false,
  failSessionSet = false,
  failTabMessage = false,
  hasOffscreenDocument = false,
  pageTranslating = false,
  senderTabId,
} = {}) {
  let listener = null;
  const localRemoved = [];
  const runtimeMessages = [];
  const sessionSet = [];
  const sessionRemoved = [];
  const tabMessages = [];
  const warnings = [];

  const chrome = {
    action: {
      setBadgeText() {},
      setBadgeBackgroundColor() {},
    },
    offscreen: {
      hasDocument() { return Promise.resolve(hasOffscreenDocument); },
      closeDocument() {
        if (failOffscreenClose) return Promise.reject(new Error("offscreen close failed"));
        return Promise.resolve();
      },
      createDocument() { return Promise.resolve(); },
    },
    runtime: {
      onMessage: {
        addListener(fn) {
          listener = fn;
        },
      },
      sendMessage(msg) {
        runtimeMessages.push(msg);
        if (failOffscreenMessage && msg && msg.cmd === "dom-translate-batch") {
          return Promise.resolve({ ok: false, error: "offscreen batch failed" });
        }
        if (failOffscreenPcm && msg && msg.cmd === "pcm") {
          return Promise.resolve({ ok: false, error: "offscreen pcm failed" });
        }
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
        get() { return Promise.resolve({ capturedTabId: 456, pageTabId: 123, pageTranslating }); },
        set(value) {
          sessionSet.push(value);
          if (failSessionSet) return Promise.reject(new Error("session set failed"));
          return Promise.resolve();
        },
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
        if (failTabMessage) return Promise.reject(new Error("tab message failed"));
        return Promise.resolve({ ok: true });
      },
      onRemoved: { addListener() {} },
      onReplaced: { addListener() {} },
      onUpdated: { addListener() {} },
    },
  };

  const context = {
    chrome,
    console: {
      error: console.error,
      log() {},
      warn(...args) { warnings.push(args.map((arg) => String(arg)).join(" ")); },
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  context.importScripts = (...files) => {
    for (const file of files) {
      vm.runInContext(fs.readFileSync(path.join(extensionRoot, file), "utf8"), context, { filename: file });
    }
  };
  vm.runInContext(fs.readFileSync(path.join(extensionRoot, "background.js"), "utf8"), context, { filename: "background.js" });
  assert.equal(typeof listener, "function");
  return { listener, localRemoved, runtimeMessages, sessionRemoved, sessionSet, tabMessages, warnings, senderTabId };
}

async function runBackgroundMessage(message, options = {}) {
  const harness = loadBackgroundHarness(options);

  const response = await new Promise((resolve) => {
    const sender = harness.senderTabId == null ? {} : { tab: { id: harness.senderTabId } };
    const keepsChannelOpen = harness.listener(message, sender, resolve);
    assert.equal(keepsChannelOpen, true);
  });
  await flushMicrotasks();
  await flushMicrotasks();

  return { ...harness, response };
}

async function runBackgroundOneWay(message, options = {}) {
  const harness = loadBackgroundHarness(options);
  const responses = [];
  const sender = harness.senderTabId == null ? {} : { tab: { id: harness.senderTabId } };
  const keepsChannelOpen = harness.listener(message, sender, (res) => responses.push(res));
  assert.equal(keepsChannelOpen, undefined);
  await flushMicrotasks();
  await flushMicrotasks();
  return { ...harness, responses };
}

function runBackgroundClearTranscript(options) {
  return runBackgroundMessage({ type: "popup-clear-transcript", tabId: 123 }, options);
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

  const startCleanupFailure = await runPopupStartWithCleanup({ ok: false, error: "cleanup failed" });
  const startMessagesAfterCleanupFailure = startCleanupFailure.runtimeMessages
    .filter((msg) => msg && msg.type && msg.type !== "popup-status");
  assert.deepEqual(
    plain(startMessagesAfterCleanupFailure),
    [{ type: "popup-cleanup" }],
  );
  assert.deepEqual(plain(startCleanupFailure.streamRequests), []);
  assert.equal(startCleanupFailure.document.getElementById("status").textContent, "실패: cleanup failed");

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

  const backgroundTabFailure = await runBackgroundClearTranscript({ failTabMessage: true });
  assert.deepEqual(plain(backgroundTabFailure.response), { ok: true });
  assert.deepEqual(plain(backgroundTabFailure.tabMessages), [
    { tabId: 456, msg: { type: "transcript-clear" } },
    { tabId: 123, msg: { type: "transcript-clear" } },
  ]);
  assert.match(backgroundTabFailure.warnings.join("\n"), /tab delivery failed: 456 transcript-clear tab message failed/);

  const backgroundCleanupFailure = await runBackgroundMessage({ type: "popup-cleanup" }, { failSessionSet: true });
  assert.deepEqual(plain(backgroundCleanupFailure.response), { ok: false, error: "session set failed" });
  assert.equal(plain(backgroundCleanupFailure.sessionSet).length, 1);

  const backgroundOffscreenCloseFailure = await runBackgroundMessage(
    { type: "popup-cleanup" },
    { failOffscreenClose: true, hasOffscreenDocument: true },
  );
  assert.deepEqual(plain(backgroundOffscreenCloseFailure.response), { ok: false, error: "offscreen close failed" });
  assert.deepEqual(plain(backgroundOffscreenCloseFailure.sessionSet), []);
  assert.deepEqual(plain(backgroundOffscreenCloseFailure.tabMessages), []);

  const backgroundAnswerFailure = await runBackgroundMessage(
    { route: "background", type: "answer", text: "summary" },
    { failSessionSet: true },
  );
  assert.deepEqual(plain(backgroundAnswerFailure.response), { ok: false, error: "session set failed" });
  assert.deepEqual(plain(backgroundAnswerFailure.sessionSet), [{ "lcc-answer": { text: "summary", done: true } }]);

  const backgroundWsStateFailure = await runBackgroundMessage(
    { route: "background", type: "wsstate", open: true },
    { failSessionSet: true },
  );
  assert.deepEqual(plain(backgroundWsStateFailure.response), { ok: false, error: "session set failed" });
  assert.deepEqual(plain(backgroundWsStateFailure.sessionSet), [{ wsOpen: true }]);

  const backgroundCaptionStateFailure = await runBackgroundMessage(
    { route: "background", type: "caption", text: "hello", ko: "안녕" },
    { failSessionSet: true },
  );
  assert.deepEqual(plain(backgroundCaptionStateFailure.response), { ok: false, error: "session set failed" });
  assert.deepEqual(plain(backgroundCaptionStateFailure.tabMessages), [
    { tabId: 456, msg: { type: "caption", text: "hello", ko: "안녕" } },
  ]);
  assert.deepEqual(plain(backgroundCaptionStateFailure.sessionSet), [{ wsOpen: true }]);

  const pageBatch = { type: "page-translate-batch", requestId: "ptr1", items: [{ id: "n1", text: "Hello" }] };
  const backgroundPageBatch = await runBackgroundMessage(pageBatch, { pageTranslating: true, senderTabId: 123 });
  assert.deepEqual(plain(backgroundPageBatch.response), { ok: true, routed: true });
  assert.deepEqual(plain(backgroundPageBatch.runtimeMessages), [
    { target: "offscreen", cmd: "dom-translate-batch", tabId: 123, requestId: "ptr1", items: [{ id: "n1", text: "Hello" }] },
  ]);

  const backgroundPageBatchInactive = await runBackgroundMessage(pageBatch, { pageTranslating: true, senderTabId: 999 });
  assert.deepEqual(plain(backgroundPageBatchInactive.response), { ok: true, routed: false });
  assert.deepEqual(plain(backgroundPageBatchInactive.runtimeMessages), []);

  const backgroundPageBatchFailure = await runBackgroundMessage(pageBatch, {
    failOffscreenMessage: true,
    pageTranslating: true,
    senderTabId: 123,
  });
  assert.deepEqual(plain(backgroundPageBatchFailure.response), { ok: false, error: "offscreen batch failed" });

  const backgroundPcmFailure = await runBackgroundOneWay(
    { type: "vd-pcm", pcm: [1, 2, 3] },
    { failOffscreenPcm: true },
  );
  assert.deepEqual(plain(backgroundPcmFailure.runtimeMessages), [
    { target: "offscreen", cmd: "pcm", pcm: [1, 2, 3] },
  ]);
  assert.match(backgroundPcmFailure.warnings.join("\n"), /offscreen delivery failed: vd-pcm offscreen pcm failed/);

  console.log("test_extension_actions: OK (popup/background cleanup, transcript clear, page batch, and video PCM paths pass)");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
