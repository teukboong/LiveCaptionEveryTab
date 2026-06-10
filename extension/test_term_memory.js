// Tests for the tab semantic memory pure data model (term-memory.js): domain extraction,
// per-domain merge (recency, rendering retention, caps), and seed-line building.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const context = { console, URL };
context.globalThis = context;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "term-memory.js"), "utf8"), context);

// --- lccTermDomainOf ---
assert.equal(context.lccTermDomainOf("https://www.youtube.com/watch?v=x"), "youtube.com");
assert.equal(context.lccTermDomainOf("http://news.ycombinator.com/item"), "news.ycombinator.com");
assert.equal(context.lccTermDomainOf("chrome://extensions"), "");
assert.equal(context.lccTermDomainOf("not a url"), "");
assert.equal(context.lccTermDomainOf(""), "");

// --- lccTermMemoryMerge: new domain, rendering retention, recency, caps ---
// NOTE: vm-context arrays have a foreign prototype -> compare via JSON, not deepEqual (like test_protocol.js).
const J = (v) => JSON.stringify(v);
let store = context.lccTermMemoryMerge({}, "youtube.com", [["Blackwell", ""], ["GPU", "GPU"]], 100);
assert.equal(J(store["youtube.com"].terms), J([["Blackwell", ""], ["GPU", "GPU"]]));
assert.equal(store["youtube.com"].t, 100);

// re-seen term keeps its verbatim rendering even when the update omits it, and moves to recency tail
store = context.lccTermMemoryMerge(store, "youtube.com", [["GPU", ""], ["Gemma", ""]], 200);
const terms = store["youtube.com"].terms;
assert.equal(J(terms[terms.length - 1]), J(["Gemma", ""]));
assert.equal(J(terms.find((t) => t[0] === "GPU")), J(["GPU", "GPU"]));

// invalid payloads are no-ops
assert.equal(J(context.lccTermMemoryMerge(store, "", [["X", ""]], 1)), J(store));
assert.equal(J(context.lccTermMemoryMerge(store, "youtube.com", [], 1)), J(store));
assert.equal(J(context.lccTermMemoryMerge(store, "youtube.com", "nope", 1)), J(store));

// per-domain term cap (40): oldest fall off
let flood = {};
const many = Array.from({ length: 50 }, (_, i) => [`Term${i}`, ""]);
flood = context.lccTermMemoryMerge(flood, "a.com", many, 1);
assert.equal(flood["a.com"].terms.length, 40);
assert.equal(flood["a.com"].terms[0][0], "Term10");

// domain cap (30): least-recently-updated evicted
let domains = {};
for (let i = 0; i < 32; i += 1) {
  domains = context.lccTermMemoryMerge(domains, `site${i}.com`, [["X", ""]], i);
}
assert.equal(Object.keys(domains).length, 30);
assert.ok(!("site0.com" in domains) && !("site1.com" in domains));
assert.ok("site31.com" in domains);

// --- lccTermSeedLines ---
const seedStore = {
  "youtube.com": { t: 1, terms: [["Blackwell", ""], ["GPU", "GPU"]] },
  "reddit.com": { t: 2, terms: [["Gemma", ""]] },
};
assert.equal(
  context.lccTermSeedLines(seedStore, ["https://www.youtube.com/watch"]),
  "Blackwell\nGPU=GPU");
assert.equal(
  context.lccTermSeedLines(seedStore, ["https://youtube.com/", "https://reddit.com/r/x"]),
  "Blackwell\nGPU=GPU\nGemma");
assert.equal(context.lccTermSeedLines(seedStore, ["https://unknown.org/"]), "");
assert.equal(context.lccTermSeedLines(seedStore, []), "");
assert.equal(context.lccTermSeedLines({}, ["https://youtube.com/"]), "");

console.log("test_term_memory: OK (domain extraction + merge caps/recency + seed lines pass)");
