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

async function loadOffscreen({ failTypes = [] } = {}) {
  let listener = null;
  const runtimeMessages = [];
  const warnings = [];

  const context = {
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
    WebSocket: { OPEN: 1, CLOSED: 3, CLOSING: 2 },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(extensionRoot, "offscreen.js"), "utf8"), context, { filename: "offscreen.js" });
  assert.equal(typeof listener, "function");
  await flushMicrotasks();
  await flushMicrotasks();
  return { listener, runtimeMessages, warnings };
}

(async () => {
  const readyFailure = await loadOffscreen({ failTypes: ["offscreen-ready"] });
  assert.deepEqual(plain(readyFailure.runtimeMessages), [{ route: "background", type: "offscreen-ready" }]);
  assert.match(readyFailure.warnings.join("\n"), /background delivery failed: offscreen-ready background offscreen-ready failed/);

  const askFailure = await loadOffscreen({ failTypes: ["answer"] });
  const response = await new Promise((resolve) => {
    const keepsChannelOpen = askFailure.listener({ target: "offscreen", cmd: "ask", mode: "summary" }, {}, resolve);
    assert.equal(keepsChannelOpen, undefined);
  });
  await flushMicrotasks();
  await flushMicrotasks();
  assert.deepEqual(plain(response), { ok: true });
  assert.deepEqual(plain(askFailure.runtimeMessages.slice(1)), [
    { route: "background", type: "answer", text: "자막을 시작한 상태에서만 요약/질문이 됩니다." },
  ]);
  assert.match(askFailure.warnings.join("\n"), /background delivery failed: answer background answer failed/);

  console.log("test_offscreen_runtime: OK (offscreen background delivery failures are observable)");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
