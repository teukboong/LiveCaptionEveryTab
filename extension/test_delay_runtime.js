const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionRoot = __dirname;

function flushMicrotasks() {
  return new Promise((resolve) => setImmediate(resolve));
}

function makeAudioNode(name) {
  return {
    name,
    connectedTo: null,
    disconnects: 0,
    connect(next) {
      this.connectedTo = next || null;
      return next || this;
    },
    disconnect() {
      this.connectedTo = null;
      this.disconnects += 1;
    },
  };
}

async function runVideoDelayPcmFailure() {
  const runtimeMessages = [];
  const warnings = [];
  let listener = null;
  let workletNode = null;

  const video = {
    videoWidth: 640,
    videoHeight: 360,
    paused: false,
    getBoundingClientRect() {
      return { left: 10, top: 20, width: 640, height: 360 };
    },
  };

  class FakeAudioContext {
    constructor() {
      this.sampleRate = 16000;
      this.state = "running";
      this.destination = makeAudioNode("destination");
      this.audioWorklet = {
        addModule() {
          return Promise.resolve();
        },
      };
    }
    createMediaElementSource() {
      return makeAudioNode("source");
    }
    createDelay() {
      const node = makeAudioNode("delay");
      node.delayTime = { value: 0 };
      return node;
    }
    createGain() {
      const node = makeAudioNode("gain");
      node.gain = { value: 1 };
      return node;
    }
  }

  class FakeAudioWorkletNode {
    constructor(_ctx, name) {
      this.name = name;
      this.port = { onmessage: null };
      workletNode = this;
    }
    connect(next) {
      return next || this;
    }
    disconnect() {}
  }

  const context = {
    AudioContext: FakeAudioContext,
    AudioWorkletNode: FakeAudioWorkletNode,
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
          return Promise.resolve({ ok: false, error: "background missing" });
        },
      },
    },
    console: {
      log() {},
      warn(...args) {
        warnings.push(args.map((arg) => String(arg)).join(" "));
      },
    },
    document: {
      documentElement: {
        appendChild() {},
      },
      querySelector(selector) {
        return selector === "video" ? video : null;
      },
      querySelectorAll(selector) {
        return selector === "video" ? [video] : [];
      },
      createElement(tag) {
        if (tag === "canvas") {
          return {
            id: "",
            style: {},
            getContext() {
              return { drawImage() {} };
            },
          };
        }
        return { style: {} };
      },
    },
    performance: {
      now() {
        return 1000;
      },
    },
    requestAnimationFrame() {
      return 1;
    },
    cancelAnimationFrame() {},
    setInterval() {
      return 1;
    },
    clearInterval() {},
    window: {
      __lccOverlay: {
        setLines() {},
        setPlaybackDelay() {},
      },
      addEventListener() {},
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(extensionRoot, "pcm.js"), "utf8"), context, { filename: "pcm.js" });
  vm.runInContext(fs.readFileSync(path.join(extensionRoot, "delay.js"), "utf8"), context, { filename: "delay.js" });
  assert.equal(typeof listener, "function");

  listener({ type: "vdelay-start", delaySec: 1 });
  await flushMicrotasks();
  await flushMicrotasks();
  assert.ok(workletNode && workletNode.port && typeof workletNode.port.onmessage === "function");

  workletNode.port.onmessage({ data: new Float32Array(2000).fill(0.25) });
  await flushMicrotasks();
  await flushMicrotasks();

  return { runtimeMessages, warnings };
}

(async () => {
  const result = await runVideoDelayPcmFailure();
  assert.equal(result.runtimeMessages.length, 1);
  assert.equal(result.runtimeMessages[0].target, "background");
  assert.equal(result.runtimeMessages[0].type, "vd-pcm");
  assert.ok(result.runtimeMessages[0].pcm.length >= 1600);
  assert.match(result.warnings.join("\n"), /runtime delivery failed: vd-pcm background missing/);
  console.log("test_delay_runtime: OK (video-delay PCM delivery failures are observable)");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
