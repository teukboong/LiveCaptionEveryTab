#!/usr/bin/env node
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "manifest.json"), "utf8"));
const scripts = manifest.content_scripts?.[0]?.js || [];
assert.ok(scripts.length > 0, "manifest content_scripts[0].js must list content scripts");

function makeNode(tagName = "DIV", nodeType = 1) {
  const node = {
    tagName,
    nodeName: tagName,
    nodeType,
    nodeValue: "",
    textContent: "",
    innerText: "",
    innerHTML: "",
    value: "",
    type: "",
    placeholder: "",
    isConnected: true,
    parentElement: null,
    parentNode: null,
    children: [],
    childNodes: [],
    dataset: {},
    style: {},
    classList: {
      add() {},
      remove() {},
      contains() { return false; },
      toggle() { return false; },
    },
    appendChild(child) {
      if (child && typeof child === "object") {
        child.parentElement = this;
        child.parentNode = this;
      }
      this.children.push(child);
      this.childNodes.push(child);
      return child;
    },
    append(...items) {
      for (const item of items) this.appendChild(item);
    },
    remove() {
      this.isConnected = false;
    },
    removeChild(child) {
      this.children = this.children.filter((c) => c !== child);
      this.childNodes = this.childNodes.filter((c) => c !== child);
      return child;
    },
    setAttribute(name, value) {
      this[name] = String(value);
    },
    removeAttribute(name) {
      delete this[name];
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    contains(other) { return other === this; },
    focus() {},
    blur() {},
    select() {},
    getBoundingClientRect() {
      return { left: 0, top: 0, right: 100, bottom: 30, width: 100, height: 30 };
    },
    matches() { return false; },
  };
  return node;
}

const documentElement = makeNode("HTML");
const head = makeNode("HEAD");
const body = makeNode("BODY");
documentElement.appendChild(head);
documentElement.appendChild(body);

const documentStub = {
  hidden: false,
  title: "Live Caption Test",
  fullscreenElement: null,
  documentElement,
  head,
  body,
  createElement(tagName) {
    return makeNode(String(tagName || "div").toUpperCase());
  },
  createTextNode(text) {
    const node = makeNode("#text", 3);
    node.nodeValue = String(text || "");
    node.textContent = node.nodeValue;
    return node;
  },
  createTreeWalker(root) {
    return {
      root,
      currentNode: root,
      nextNode() { return null; },
    };
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  elementsFromPoint() { return []; },
  addEventListener() {},
  removeEventListener() {},
  execCommand() { return true; },
};

const listeners = [];
const chromeStub = {
  runtime: {
    lastError: null,
    getURL(file) { return `chrome-extension://test/${file}`; },
    onMessage: {
      addListener(fn) { listeners.push(fn); },
      removeListener(fn) {
        const i = listeners.indexOf(fn);
        if (i >= 0) listeners.splice(i, 1);
      },
    },
    sendMessage(_msg, callback) {
      if (typeof callback === "function") callback({});
      return Promise.resolve({});
    },
  },
  storage: {
    local: {
      get(_keys, callback) {
        const value = {};
        if (typeof callback === "function") callback(value);
        return Promise.resolve(value);
      },
      set(_value, callback) {
        if (typeof callback === "function") callback();
        return Promise.resolve();
      },
      remove(_keys, callback) {
        if (typeof callback === "function") callback();
        return Promise.resolve();
      },
    },
    onChanged: {
      addListener() {},
      removeListener() {},
    },
  },
};

class MutationObserverStub {
  constructor(callback) {
    this.callback = callback;
  }
  observe() {}
  disconnect() {}
  takeRecords() { return []; }
}

class EventStub {
  constructor(type, options = {}) {
    this.type = type;
    this.bubbles = !!options.bubbles;
  }
}

const context = {
  console,
  chrome: chromeStub,
  document: documentStub,
  window: null,
  globalThis: null,
  self: null,
  location: { href: "https://example.test/page", origin: "https://example.test" },
  navigator: { userAgent: "lcc-content-load-test" },
  performance: { now: () => Date.now() },
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
  cancelAnimationFrame: clearTimeout,
  requestIdleCallback: (fn) => setTimeout(() => fn({ timeRemaining: () => 50, didTimeout: false }), 0),
  cancelIdleCallback: clearTimeout,
  MutationObserver: MutationObserverStub,
  Event: EventStub,
  Node: { TEXT_NODE: 3, ELEMENT_NODE: 1 },
  NodeFilter: { SHOW_TEXT: 4, SHOW_ELEMENT: 1 },
  AudioContext: class AudioContextStub {},
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  getComputedStyle: () => ({
    display: "block",
    visibility: "visible",
    opacity: "1",
    whiteSpace: "normal",
  }),
};
context.window = context;
context.globalThis = context;
context.self = context;
context.top = context;
context.parent = context;
context.innerWidth = 1280;
context.innerHeight = 720;
context.devicePixelRatio = 1;
context.addEventListener = () => {};
context.removeEventListener = () => {};

const vmContext = vm.createContext(context);
for (const script of scripts) {
  const file = path.join(__dirname, script);
  const source = fs.readFileSync(file, "utf8");
  vm.runInContext(source, vmContext, { filename: path.relative(ROOT, file) });
}

const requiredExpressions = [
  "typeof setLines === 'function'",
  "typeof setLinesSplit === 'function'",
  "typeof lccHandleBridgeMessage === 'function'",
  "typeof lccPageTranslateStart === 'function'",
  "typeof lccPageTranslateStop === 'function'",
  "typeof lccPageTranslateApply === 'function'",
  "typeof lccPageTranslatePartial === 'function'",
  "typeof lccWbHandleResult === 'function'",
  "typeof lccOcrHandleResult === 'function'",
  "!!window.__lccOverlay && typeof window.__lccOverlay.setLines === 'function'",
];
for (const expr of requiredExpressions) {
  assert.equal(vm.runInContext(expr, vmContext), true, expr);
}

console.log(`test_content_load: OK (${scripts.join(" -> ")})`);
