const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionRoot = __dirname;

function flushMicrotasks() {
  return new Promise((resolve) => setImmediate(resolve));
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

async function loadOffscreen({ failSocket = false, failTypes = [] } = {}) {
  let listener = null;
  const runtimeMessages = [];
  const sockets = [];
  const warnings = [];
  class FakeWebSocket {
    static OPEN = 1;
    static CLOSED = 3;
    static CLOSING = 2;

    constructor(url) {
      if (failSocket) throw new Error("socket failed");
      this.url = url;
      this.readyState = FakeWebSocket.OPEN;
      this.sent = [];
      this.bufferedAmount = 0;
      sockets.push(this);
    }

    send(data) {
      this.sent.push(data);
    }

    close() {
      this.readyState = FakeWebSocket.CLOSED;
    }

    message(data) {
      this.onmessage && this.onmessage({ data });
    }
  }

  const context = {
    LCC_BRIDGE_URL: "ws://127.0.0.1:8765",
    chrome: {
      runtime: {
        getURL(file) {
          return file;
        },
        onMessage: {
          addListener(fn) {
            listener = fn;
          },
        },
        sendMessage(msg) {
          runtimeMessages.push(msg);
          if (failTypes.includes(msg && msg.type)) {
            return Promise.reject(new Error("background " + msg.type + " failed"));
          }
          return Promise.resolve({ ok: true });
        },
      },
    },
    console: {
      log() {},
      warn(...args) {
        warnings.push(args.map((arg) => String(arg)).join(" "));
      },
    },
    Int16Array,
    JSON,
    WebSocket: FakeWebSocket,
    lccBridgeHello(ws) {
      ws.send(JSON.stringify({ type: "hello" }));
    },
    lccBuildBridgeConfig() {
      return { type: "config" };
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(extensionRoot, "offscreen.js"), "utf8"), context, { filename: "offscreen.js" });
  assert.equal(typeof listener, "function");
  await flushMicrotasks();
  await flushMicrotasks();
  return { listener, runtimeMessages, sockets, warnings };
}

function sendOffscreenMessage(harness, msg) {
  return new Promise((resolve) => {
    const keepsChannelOpen = harness.listener(msg, {}, resolve);
    assert.equal(keepsChannelOpen, undefined);
  });
}

(async () => {
  const readyFailure = await loadOffscreen({ failTypes: ["offscreen-ready"] });
  assert.deepEqual(plain(readyFailure.runtimeMessages), [{ route: "background", type: "offscreen-ready" }]);
  assert.match(readyFailure.warnings.join("\n"), /background delivery failed: offscreen-ready background offscreen-ready failed/);

  const askFailure = await loadOffscreen({ failTypes: ["answer"] });
  const response = await sendOffscreenMessage(askFailure, { target: "offscreen", cmd: "ask", mode: "summary" });
  await flushMicrotasks();
  await flushMicrotasks();
  assert.deepEqual(plain(response), { ok: true });
  assert.deepEqual(plain(askFailure.runtimeMessages.slice(1)), [
    { route: "background", type: "answer", text: "자막을 시작한 상태에서만 요약/질문이 됩니다." },
  ]);
  assert.match(askFailure.warnings.join("\n"), /background delivery failed: answer background answer failed/);

  const batchRouting = await loadOffscreen();
  const batchResponse = await sendOffscreenMessage(batchRouting, {
    target: "offscreen",
    cmd: "dom-translate-batch",
    requestId: "req-1",
    items: [{ id: "node-1", text: "Hello world" }],
  });
  assert.deepEqual(plain(batchResponse), { ok: true });

  const brokenItem = {};
  Object.defineProperty(brokenItem, "id", {
    get() {
      throw new Error("bad item id");
    },
  });
  const failedBatchResponse = await sendOffscreenMessage(batchRouting, {
    target: "offscreen",
    cmd: "dom-translate-batch",
    requestId: "req-2",
    items: [brokenItem],
  });
  assert.deepEqual(plain(failedBatchResponse), { ok: false, error: "bad item id" });

  const bridgeParseFailure = await loadOffscreen();
  const startPageResponse = await sendOffscreenMessage(bridgeParseFailure, { target: "offscreen", cmd: "start-page", pageContext: "", config: {} });
  await flushMicrotasks();
  assert.deepEqual(plain(startPageResponse), { ok: true });
  assert.equal(bridgeParseFailure.sockets.length, 1);
  bridgeParseFailure.sockets[0].message("{not json");
  assert.match(bridgeParseFailure.warnings.join("\n"), /bridge message ignored: .*JSON/);

  const startPageFailure = await loadOffscreen({ failSocket: true });
  const failedStartPageResponse = await sendOffscreenMessage(startPageFailure, { target: "offscreen", cmd: "start-page", pageContext: "", config: {} });
  await flushMicrotasks();
  await flushMicrotasks();
  assert.deepEqual(plain(failedStartPageResponse), { ok: true });
  assert.deepEqual(plain(startPageFailure.runtimeMessages.slice(1)), [
    { route: "background", type: "err", text: "페이지 번역 시작 실패: socket failed" },
  ]);

  console.log("test_offscreen_runtime: OK (offscreen runtime failures are observable)");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
