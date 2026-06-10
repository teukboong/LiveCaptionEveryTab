// ---- page translation: direct DOM text replacement, driven by MutationObserver deltas ----
// iframe coverage: page translation runs in EVERY real content frame of the tab (the manifest injects
// all_frames; background broadcasts page-translate messages tab-wide and dom_translate results reach all
// frames). Each frame keeps its own queues/caches keyed off its own document; the frame tag below makes
// request ids tab-unique so a frame never adopts (or tears down on) another frame's results. Pixel/ad
// frames are skipped by size; the caption overlay stays governed by lccShouldRender (unchanged).
const LCC_PAGE_FRAME_TAG = LCC_IS_TOP ? "t" : "f" + Math.random().toString(36).slice(2, 7);
function lccPageFrameAllowed() {
  if (LCC_IS_TOP) return true;
  try {
    if (!/^https?:$/.test(location.protocol)) return false;
    return window.innerWidth >= 200 && window.innerHeight >= 100;   // skip tracking/ad pixel frames
  } catch (_) {
    return false;
  }
}
const LCC_PAGE_EXCLUDE_SELECTOR = [
  "script", "style", "noscript", "template", "svg", "canvas", "video", "audio",
  "input", "textarea", "select", "option", "pre", "code", "kbd", "samp",
  "[contenteditable='true']", "[contenteditable='']", "[aria-hidden='true']",
  "#lcc-overlay", "#lcc-bilingual-ghost",
].join(",");
const LCC_PAGE_BATCH_POLICY = Object.freeze({
  page: Object.freeze({ batchSize: 8, batchChars: 3600, scanLimit: 150, maxInflight: 3, flushMs: 80 }),
  both: Object.freeze({ batchSize: 3, batchChars: 1600, scanLimit: 90, maxInflight: 1, flushMs: 140 }),
});
let lccPageTranslateOn = false;
let lccPageTranslateSettings = { ...globalThis.LCC_DEFAULT_SETTINGS };
let lccPageTranslateObserver = null;
let lccPageTranslateScrollHandler = null;
let lccPageTranslateFlushTimer = null;
let lccPageTranslateScanTimer = null;
let lccPageTranslateReqSeq = 0;
let lccPageTranslateEpoch = 0;
let lccPageTranslateConfigSig = "";
// Work is keyed by normalized source text, not by node: identical strings (the hundreds of repeated
// "Reply"/"Share"/"5h ago" on a real page) collapse to ONE model call and fan out to every node sharing
// the text. Two queues give viewport-first ordering — visible nodes translate before off-screen ones.
const lccPageHotQueue = [];                  // sourceKeys whose nodes are in the viewport now (drained first, reading order)
const lccPageColdQueue = [];                 // sourceKeys allowed but off-screen (drained only when hot is empty)
const lccPageWork = new Map();               // sourceKey -> { text, nodes:Set<node>, status:"queued"|"pending", hot:boolean }
const lccPageTranslateNodes = new Set();     // every node we hold state for (restore on stop)
const lccPageTranslateState = new WeakMap(); // node -> per-node DOM state
const lccPageTranslateRequests = new Map();  // requestId -> { keys:[sourceKey], timer }
const lccPageTranslateStats = {
  resultSeen: 0, partialSeen: 0, applied: 0, partialApplied: 0,
  dropNoNode: 0, dropSource: 0, dropChanged: 0, dropEmpty: 0, blockUnits: 0,
};
const LCC_PAGE_PARTIAL_MAX_CHARS = 420;      // speculative DOM streaming is only for short visible text
// The bridge sentence-chunks any item over PAGE_LONG_CHARS (translate_page_long_once) and the flush timeout
// below scales with item length, so paragraph length is NOT the limiter. This ceiling is only a sanity bound
// against a pathological single node (a whole article or code/data blob collapsed into one text node), where
// chunking would tie the GPU up too long — it is NOT a translation-capacity limit, so it doesn't need to grow
// with content. (The old pageTranslateMaxChars gate at ~4000 dropped long paragraphs the server could handle.)
const LCC_PAGE_NODE_MAX_CHARS = 32000;
// Bilingual ghost: keep each element's pre-translation text so hover/focus reveals the original.
const lccBilingualOrig = new WeakMap();      // marked element -> its original (pre-translation) text
let lccBilingualGhost = null;
let lccBilingualOver = null, lccBilingualOut = null, lccBilingualHide = null;
const LCC_PAGE_BILINGUAL_MAX_CHARS = 1500;   // don't snapshot huge containers
// cache-then-verify: cross-site label/seed hits show instantly, then re-translate in idle and quietly patch
// if the model (with this page's context) disagrees. Best-effort, idle-only, deduped, opt-in (pageVerify).
let lccPageVerifyReqSeq = 0;
let lccPageVerifyTimer = 0;
let lccPageVerifyInflight = "";
const lccPageVerifySeen = new Set();          // sourceKeys already verified/queued this page
const lccPageVerify = new Map();              // sourceKey -> { source, shown, nodes:Set<node> }
const lccPageVerifyQueue = [];                // sourceKeys pending verify
const lccPageVerifyRequests = new Map();      // requestId -> { keys, timer }
const LCC_PAGE_VERIFY_BATCH = 6;
const LCC_PAGE_ROUTE_WARN_INTERVAL_MS = 2000;
let lccPageRouteWarnAt = 0;
// Time-sliced scan: a persistent TreeWalker advanced in idle chunks so a huge DOM never blocks the main
// thread, plus a one-shot below-fold prefetch so scrolling lands on already-translated text.
const LCC_PAGE_SCAN_SLICE_MS = 6;            // main-thread budget per scan chunk
const LCC_PAGE_SCAN_CHUNK_NODES = 250;       // hard node cap per chunk (belt + suspenders)
const LCC_PAGE_PREFETCH_MAX_NODES = 2200;    // bound whole-page prefetch on pathological pages
let lccPageScanCursor = null;                // { walker, prefetch, seen } across chunks
let lccPageScanIdleId = 0;
let lccPagePrefetchTimer = 0;
let lccPagePrefetchDone = false;             // whole-doc prefetch already queued for this page
let lccPagePrefetchScanning = false;         // true only inside a prefetch chunk -> relax the viewport gate
// Cross-site UI-label cache: short labels learned anywhere (model-sourced) render instantly everywhere,
// across sessions. Keyed by target language only; bootstrapped from the static seed in page-seed.js.
const LCC_PAGE_LABEL_KEY = "lcc-page-label-cache-v1";
const LCC_PAGE_LABEL_MAX_CHARS = 24;
const LCC_PAGE_LABEL_MAX_ENTRIES = 600;
let lccPageLabelNs = "";
let lccPageLabelCache = new Map();
let lccPageLabelLoaded = false;
let lccPageLabelPersistTimer = null;
const LCC_PAGE_CACHE_KEY = "lcc-page-translation-cache-v1";
const LCC_PAGE_CACHE_MAX_PAGES = 12;
const LCC_PAGE_CACHE_MAX_ENTRIES = 900;
let lccPageTranslateCacheNs = "";
let lccPageTranslateCache = new Map();
let lccPageTranslateCacheLoaded = false;
let lccPageTranslateCacheSeq = 0;
let lccPageTranslateCachePersistTimer = null;
let lccPageTranslateCachePersistSnapshot = null;
let lccPageTranslateUrl = "";
let lccPageTranslateUrlTimer = null;
let lccPageTranslateLastContext = "";

function lccPageTranslatePolicy() {
  const mode = lccPageTranslateSettings && lccPageTranslateSettings.runMode;
  return globalThis.lccRunModeIncludesCaption(mode) ? LCC_PAGE_BATCH_POLICY.both : LCC_PAGE_BATCH_POLICY.page;
}
function lccPageHash(value) {
  const s = String(value || "").normalize("NFC");
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(36);
}
function lccPageUrlKey() {
  return String(location.href || "").split("#")[0].slice(0, 260);
}
function lccPageCacheNamespace() {
  const s = lccPageTranslateSettings || {};
  return [
    lccPageUrlKey(),
    s.targetLang || "",
    s.pageRegister || "",
    lccPageHash(s.pageContextHint || s.contextHint || ""),
    lccPageHash(s.pageGlossary || s.glossary || ""),
    lccPageHash(s.customPrompt || ""),          // a custom-prompt change must re-translate the page (cache miss)
  ].join("|");
}
function lccPageSourceNorm(source) {
  return String(source || "").normalize("NFC").replace(/\s+/g, " ").trim();
}
function lccPageSourceKey(source) {
  const norm = lccPageSourceNorm(source);
  return norm.length + ":" + lccPageHash(norm);
}
function lccPageLabelNamespace() {
  return String((lccPageTranslateSettings && lccPageTranslateSettings.targetLang) || "");
}
function lccPageConfigSignature() {
  const s = lccPageTranslateSettings || {};
  return [
    s.targetLang || "",
    s.pageRegister || "",
    s.pageTranslateSelector || "body",
    String(s.pageTranslateMinChars || ""),
    String(s.pageTranslateMaxChars || ""),
    lccPageHash(s.pageContextHint || s.contextHint || ""),
    lccPageHash(s.pageGlossary || s.glossary || ""),
    lccPageHash(s.customPrompt || ""),          // re-scan/re-translate the page when the custom prompt changes
  ].join("|");
}
function lccPageLooksLikeUiLabel(norm) {
  const s = String(norm || "").trim();
  if (!s || s.length > LCC_PAGE_LABEL_MAX_CHARS) return false;
  if (/[\r\n]/.test(s)) return false;
  if (/https?:|www\.|@/.test(s)) return false;
  if (/^[\d\s.,:%()+\-–—/\\]+$/.test(s)) return false;
  if (/^[\d#]/.test(s)) return false;
  if (/[.!?。！？]$/.test(s)) return false;
  return s.split(/\s+/).filter(Boolean).length <= 4;
}
function lccPageRequestOwns(requestId, key) {
  const req = lccPageTranslateRequests.get(String(requestId || ""));
  return !!(req && Array.isArray(req.keys) && req.keys.includes(key));
}
async function lccPageLoadLabelCache() {
  const ns = lccPageLabelNamespace();
  if (lccPageLabelLoaded && lccPageLabelNs === ns) return;
  lccPageLabelNs = ns;
  lccPageLabelLoaded = false;
  lccPageLabelCache = new Map();
  try {
    const r = await chrome.storage.local.get(LCC_PAGE_LABEL_KEY);
    if (ns !== lccPageLabelNamespace()) return;          // target changed mid-load -> let the next call redo
    const lang = r[LCC_PAGE_LABEL_KEY] && r[LCC_PAGE_LABEL_KEY].langs && r[LCC_PAGE_LABEL_KEY].langs[ns];
    const entries = lang && lang.entries || {};
    for (const [key, rec] of Object.entries(entries)) {
      if (rec && typeof rec.source === "string" && typeof rec.target === "string") lccPageLabelCache.set(key, rec);
    }
  } catch (_) {}
  const seed = (globalThis.LCC_PAGE_SEED && globalThis.LCC_PAGE_SEED[ns]) || null;   // bootstrap; never overwrites learned
  if (seed) {
    for (const en of Object.keys(seed)) {
      const norm = lccPageSourceNorm(en);
      const rendered = String(seed[en] || "").trim();
      if (!norm || !rendered) continue;
      const key = lccPageSourceKey(norm);
      if (!lccPageLabelCache.has(key)) lccPageLabelCache.set(key, { source: norm, target: rendered, t: 0, seed: true });
    }
  }
  lccPageLabelLoaded = true;
}
function lccPageLabelRemember(norm, key, target) {
  const rendered = String(target || "").trim();
  if (!lccPageLabelLoaded || !lccPageLooksLikeUiLabel(norm) || !rendered || rendered === norm) return;
  lccPageLabelCache.delete(key);
  lccPageLabelCache.set(key, { source: norm, target: rendered, t: Date.now() });
  while (lccPageLabelCache.size > LCC_PAGE_LABEL_MAX_ENTRIES) {
    lccPageLabelCache.delete(lccPageLabelCache.keys().next().value);
  }
  lccPageScheduleLabelPersist();
}
function lccPageScheduleLabelPersist() {
  if (!lccPageLabelNs || lccPageLabelPersistTimer) return;
  lccPageLabelPersistTimer = setTimeout(async () => {
    lccPageLabelPersistTimer = null;
    const ns = lccPageLabelNs;
    const rows = [...lccPageLabelCache.entries()].filter(([, rec]) => rec && !rec.seed)   // seed re-applies on load
      .sort((a, b) => (b[1].t || 0) - (a[1].t || 0)).slice(0, LCC_PAGE_LABEL_MAX_ENTRIES);
    try {
      const r = await chrome.storage.local.get(LCC_PAGE_LABEL_KEY);
      const store = r[LCC_PAGE_LABEL_KEY] && typeof r[LCC_PAGE_LABEL_KEY] === "object" ? r[LCC_PAGE_LABEL_KEY] : {};
      const langs = store.langs && typeof store.langs === "object" ? store.langs : {};
      const entries = {};
      for (const [key, rec] of rows) entries[key] = { source: rec.source, target: rec.target, t: rec.t };
      langs[ns] = { t: Date.now(), entries };
      const keep = Object.entries(langs).sort((a, b) => ((b[1] && b[1].t) || 0) - ((a[1] && a[1].t) || 0)).slice(0, 12);
      await chrome.storage.local.set({ [LCC_PAGE_LABEL_KEY]: { version: 1, langs: Object.fromEntries(keep) } });
    } catch (_) {}
  }, 400);
}
async function lccPageLoadCache() {
  await lccPageLoadLabelCache();
  const ns = lccPageCacheNamespace();
  if (lccPageTranslateCacheLoaded && lccPageTranslateCacheNs === ns) return;
  const seq = ++lccPageTranslateCacheSeq;
  lccPageTranslateCacheNs = ns;
  lccPageTranslateCacheLoaded = false;
  lccPageTranslateCache = new Map();
  try {
    const r = await chrome.storage.local.get(LCC_PAGE_CACHE_KEY);
    if (seq !== lccPageTranslateCacheSeq) return;
    const page = r[LCC_PAGE_CACHE_KEY] && r[LCC_PAGE_CACHE_KEY].pages && r[LCC_PAGE_CACHE_KEY].pages[ns];
    const entries = page && page.entries || {};
    for (const [key, rec] of Object.entries(entries)) {
      if (rec && typeof rec.source === "string" && typeof rec.target === "string") {
        lccPageTranslateCache.set(key, rec);
      }
    }
  } catch (_) {}
  lccPageTranslateCacheLoaded = true;
}
function lccPageScheduleCachePersist() {
  if (!lccPageTranslateCacheNs) return;
  lccPageTranslateCachePersistSnapshot = {
    ns: lccPageTranslateCacheNs,
    rows: [...lccPageTranslateCache.entries()].sort((a, b) => (b[1].t || 0) - (a[1].t || 0)).slice(0, LCC_PAGE_CACHE_MAX_ENTRIES),
  };
  if (lccPageTranslateCachePersistTimer) return;
  lccPageTranslateCachePersistTimer = setTimeout(async () => {
    lccPageTranslateCachePersistTimer = null;
    const snap = lccPageTranslateCachePersistSnapshot;
    if (!snap || !snap.ns) return;
    try {
      const r = await chrome.storage.local.get(LCC_PAGE_CACHE_KEY);
      const store = r[LCC_PAGE_CACHE_KEY] && typeof r[LCC_PAGE_CACHE_KEY] === "object" ? r[LCC_PAGE_CACHE_KEY] : {};
      const pages = store.pages && typeof store.pages === "object" ? store.pages : {};
      const entries = {};
      for (const [key, rec] of snap.rows) entries[key] = rec;
      pages[snap.ns] = { t: Date.now(), entries };
      const keep = Object.entries(pages).sort((a, b) => ((b[1] && b[1].t) || 0) - ((a[1] && a[1].t) || 0)).slice(0, LCC_PAGE_CACHE_MAX_PAGES);
      await chrome.storage.local.set({ [LCC_PAGE_CACHE_KEY]: { version: 1, pages: Object.fromEntries(keep) } });
    } catch (_) {}
  }, 250);
}
function lccPageCachedEntry(source) {
  const norm = lccPageSourceNorm(source);
  const key = lccPageSourceKey(norm);
  if (lccPageTranslateCacheLoaded) {                       // this page's cache first (context-specific, trusted)
    const rec = lccPageTranslateCache.get(key);
    if (rec && rec.source === norm) return { target: rec.target, kind: "url" };
  }
  if (lccPageLabelLoaded) {                                // then the cross-site label cache + seed (context-free)
    const rec = lccPageLabelCache.get(key);
    if (rec && rec.source === norm) return { target: rec.target, kind: rec.seed ? "seed" : "label" };
  }
  return null;
}
function lccPageCachedTarget(source) {
  const e = lccPageCachedEntry(source);
  return e ? e.target : "";
}
function lccPageRememberCache(source, target) {
  const norm = lccPageSourceNorm(source);
  const rendered = String(target || "").trim();
  if (!norm || !rendered) return;
  const key = lccPageSourceKey(norm);
  lccPageTranslateCache.delete(key);
  lccPageTranslateCache.set(key, { source: norm, target: rendered, t: Date.now() });
  while (lccPageTranslateCache.size > LCC_PAGE_CACHE_MAX_ENTRIES) {
    lccPageTranslateCache.delete(lccPageTranslateCache.keys().next().value);
  }
  lccPageScheduleCachePersist();
  if (lccPageLooksLikeUiLabel(norm)) lccPageLabelRemember(norm, key, rendered);   // conservative short UI labels -> cross-site
}
function lccPageTextParts(raw) {
  const text = String(raw || "");
  const pre = (text.match(/^\s*/) || [""])[0];
  const post = (text.match(/\s*$/) || [""])[0];
  const core = text.slice(pre.length, text.length - post.length);
  return { pre, core, post };
}
function lccPageHasLetters(text) {
  try { return /[\p{L}]/u.test(text); }
  catch (_) { return /[A-Za-z\u00c0-\uffff]/.test(text); }
}
function lccPageNodeStyled(parent) {
  if (!parent || !parent.isConnected) return false;
  const style = window.getComputedStyle(parent);
  if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
  const rect = parent.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function lccPageNearViewport(parent) {
  const rect = parent.getBoundingClientRect();
  return rect.bottom >= -200 && rect.top <= (window.innerHeight || 0) + 600;
}
function lccPageInViewport(parent) {
  if (!parent) return false;
  const rect = parent.getBoundingClientRect();
  const h = window.innerHeight || document.documentElement.clientHeight || 0;
  return rect.bottom >= 0 && rect.top <= h && rect.width > 0 && rect.height > 0;
}
// Distinctive-script targets let us skip nodes already written in the target language without a model
// round-trip. Latin-script targets (English/Spanish/French/...) can't be told apart by script alone, so
// they map to null and fall through to the model (which returns already-target text unchanged).
const LCC_PAGE_TARGET_SCRIPT = Object.freeze({
  Korean: /[가-힣ᄀ-ᇿ㄰-㆏]/g,
  // Han characters are shared across Chinese/Japanese/Korean. For Japanese, only kana is distinctive;
  // for Chinese there is no safe script-only skip, so the model decides whether Han-only text is already target.
  Japanese: /[぀-ヿ]/g,
  Russian: /[Ѐ-ӿ]/g, Ukrainian: /[Ѐ-ӿ]/g, Bulgarian: /[Ѐ-ӿ]/g, Serbian: /[Ѐ-ӿ]/g,
  Greek: /[Ͱ-Ͽ]/g,
  Arabic: /[؀-ۿ]/g, Persian: /[؀-ۿ]/g, Urdu: /[؀-ۿ]/g,
  Hebrew: /[֐-׿]/g,
  Thai: /[฀-๿]/g,
  Hindi: /[ऀ-ॿ]/g, Bengali: /[ঀ-৿]/g, Tamil: /[஀-௿]/g, Telugu: /[ఀ-౿]/g,
});
const LCC_PAGE_TARGET_SCRIPT_MIN = Object.freeze({ Japanese: 0.2 });
function lccPageAlreadyTarget(core) {
  const re = LCC_PAGE_TARGET_SCRIPT[lccPageTranslateSettings.targetLang];
  if (!re) return false;
  let letters;
  try { letters = (core.match(/\p{L}/gu) || []).length; }
  catch (_) { letters = (core.match(/[A-Za-zÀ-]/g) || []).length; }
  if (!letters) return false;
  const minRatio = LCC_PAGE_TARGET_SCRIPT_MIN[lccPageTranslateSettings.targetLang] || 0.6;
  return (core.match(re) || []).length / letters >= minRatio;
}
function lccPageNodeAllowed(node) {
  if (!lccPageTranslateOn || !lccPageFrameAllowed() || !node || node.nodeType !== Node.TEXT_NODE) return false;
  const parent = node.parentElement;
  if (!parent || parent.closest(LCC_PAGE_EXCLUDE_SELECTOR)) return false;
  const state = lccPageTranslateState.get(node);
  if (state && state.translatedFull && node.nodeValue === state.translatedFull) return false;
  if (state && state.partialFull && node.nodeValue === state.partialFull) return false;   // a speculative partial is showing
  if (!lccPageNodeStyled(parent)) return false;
  if (!lccPagePrefetchScanning && !lccPageNearViewport(parent)) return false;   // prefetch relaxes the window
  const { core } = lccPageTextParts(node.nodeValue);
  const minChars = Number(lccPageTranslateSettings.pageTranslateMinChars) || 2;
  if (core.length < minChars || core.length > LCC_PAGE_NODE_MAX_CHARS) return false;   // long paragraphs flow to the server's sentence-chunked path; only huge nodes are skipped
  if (!lccPageHasLetters(core)) return false;
  if (/^[\d\s.,:%()+\-–—/\\]+$/.test(core)) return false;
  if (lccPageAlreadyTarget(core)) return false;     // already in a distinctive-script target -> no round-trip
  return true;
}
function lccPageStateFor(node) {
  let state = lccPageTranslateState.get(node);
  if (!state) {
    state = { pending: false, source: "", originalFull: "", partialFull: "", partialTarget: "" };
    lccPageTranslateState.set(node, state);
    lccPageTranslateNodes.add(node);
  }
  return state;
}
function lccPageNodeHoldsPendingSource(node, state) {
  if (!node || !state) return false;
  return node.nodeValue === state.expectedFull || (!!state.partialFull && node.nodeValue === state.partialFull);
}
function lccPageClearPartialState(state) {
  if (!state) return;
  state.partialFull = "";
  state.partialTarget = "";
  state.partialRequestId = "";
}
function lccPageRestorePartialNode(node, state) {
  if (!node || !state || !state.partialFull) return;
  if (node.isConnected && node.nodeValue === state.partialFull) node.nodeValue = state.expectedFull;
  lccPageClearPartialState(state);
}
function lccPagePruneWork(work) {
  // Keep nodes still in the DOM holding either the queued source or a speculative partial for that source.
  for (const n of [...work.nodes]) {
    const st = lccPageTranslateState.get(n);
    if (!n.isConnected || !st || !lccPageNodeHoldsPendingSource(n, st)) work.nodes.delete(n);
  }
  return work.nodes.size > 0;
}
function lccPageApplyToNode(node, state, source, target, expectedFull, pre, post, originalFull) {
  state.source = source;
  state.sourceNorm = lccPageSourceNorm(source);
  state.pre = pre || "";
  state.post = post || "";
  state.expectedFull = expectedFull;
  state.originalFull = originalFull || expectedFull;
  state.pending = false;
  lccPageClearPartialState(state);
  state.translated = target;
  state.translatedFull = state.pre + target + state.post;
  node.nodeValue = state.translatedFull;       // direct browser DOM replacement, not an overlay
  lccPageBilingualInlineMark(node.parentElement);
}
// Speculative partial streaming: paint the model's in-progress translation into the node; a later final
// (dom_translate_result) confirms it, and busy/err/clear restores the original. Partials are never cached.
function lccPagePartialAllowed(node, target, srcLen) {
  if (!node || !node.parentElement || !target) return false;
  // Short nodes never get a huge speculative blob painted; a long node (server sentence-chunks + streams it)
  // may stream its growing cumulative translation in even though it's long.
  if (target.length > LCC_PAGE_PARTIAL_MAX_CHARS && (srcLen || 0) <= LCC_PAGE_PARTIAL_MAX_CHARS) return false;
  if (document.hidden || !lccPageInViewport(node.parentElement)) return false;
  try { if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false; } catch (_) {}
  return true;
}
function lccPageApplyPartialToNode(node, state, source, target, requestId, sourceNorm) {
  if (!lccPagePartialAllowed(node, target, (source || state.source || "").length)) return false;
  const current = node.nodeValue;
  const isExpected = current === state.expectedFull;
  const isPartial = !!state.partialFull && current === state.partialFull;
  let pre = state.pre || "";
  let post = state.post || "";
  if (!isExpected && !isPartial) {                  // node text changed under us -> only paint if it's still the source
    const currentParts = lccPageTextParts(current);
    if (lccPageSourceNorm(currentParts.core) !== sourceNorm) return false;
    pre = currentParts.pre;
    post = currentParts.post;
  }
  state.source = source;
  state.sourceNorm = sourceNorm;
  state.pre = pre;
  state.post = post;
  state.pending = true;
  state.partialTarget = target;
  state.partialRequestId = requestId;
  state.partialFull = pre + target + post;
  node.nodeValue = state.partialFull;
  return true;
}
const LCC_PAGE_BLOCK_TAGS = new Set([
  "P", "LI", "BLOCKQUOTE", "DD", "DT", "FIGCAPTION", "TD", "TH", "CAPTION", "DIV",
  "H1", "H2", "H3", "H4", "H5", "H6", "ARTICLE", "SECTION", "ASIDE", "MAIN", "DETAILS", "SUMMARY",
]);
const LCC_PAGE_BLOCK_CTX_MAX = 600;
// ---- semantic block batching (Policy A: anchor-collapse) ----------------------------------------------
// A leaf text block split by inline STYLE elements (b/i/em/span…) reads as ONE sentence but reaches us as
// several text nodes, each translated on its own — which scrambles word order in reordering languages
// (KO/JA: "Click <b>here</b> to continue" → fragments land in English order). Policy A translates the whole
// block as a single segment and collapses the result into the first fragment node, blanking the others. It
// reuses the existing @@n@@ marker-batch path verbatim (the block is just a longer segment), so there is NO
// new server / sentinel / guard surface. Blocks holding interactive or identifier nodes (links, buttons,
// code) are NOT eligible — those need positional preservation (Policy R, future) — and fall through to the
// per-node path unchanged. Strict node-identity checks make every result idempotent and fully restorable.
const LCC_PAGE_BLOCK_UNIT = true;                       // kill switch for Policy A
const LCC_PAGE_BLOCK_UNIT_MAX_CHARS = 4000;             // never collapse a block bigger than this (server sentence-chunks it as one long item)
const LCC_PAGE_NESTED_BLOCK_SELECTOR = [...LCC_PAGE_BLOCK_TAGS].map((t) => t.toLowerCase()).join(",");
const LCC_PAGE_OPAQUE_SELECTOR = [     // a descendant of any of these makes a block ineligible for collapse
  "a", "button", "code", "kbd", "samp", "input", "textarea", "select", "label",
  "[role='link']", "[role='button']", "[onclick]", "[contenteditable='true']", "[contenteditable='']",
].join(",");
let lccPageBlockSeq = 0;
function lccPageBlockContext(node, core) {
  // Text of the nearest semantic block (p/li/td/heading/...): sent as reference-only context so a fragment
  // split out by inline elements is translated with its surrounding prose. "" when the node IS essentially
  // the whole block (no extra context) or the block is too big to be useful.
  let el = node.parentElement, hops = 0;
  while (el && hops < 6 && !LCC_PAGE_BLOCK_TAGS.has(el.tagName)) { el = el.parentElement; hops += 1; }
  if (!el || !LCC_PAGE_BLOCK_TAGS.has(el.tagName)) return "";
  let txt;
  try { txt = lccPageSourceNorm(el.textContent); } catch (_) { return ""; }
  if (!txt || txt.length > LCC_PAGE_BLOCK_CTX_MAX || txt.length < core.length * 1.3) return "";
  return txt;
}
function lccPageBlockUnitFor(node) {
  // Plan a Policy-A block from an allowed text node. Eligible only when the nearest semantic block is a
  // LEAF text block (no nested block, no interactive/identifier/excluded descendant) split into >=2
  // translatable text nodes. Returns { el, members, source, hot } or null (→ caller uses the per-node path).
  if (!LCC_PAGE_BLOCK_UNIT) return null;
  let el = node.parentElement, hops = 0;
  while (el && hops < 6 && !LCC_PAGE_BLOCK_TAGS.has(el.tagName)) { el = el.parentElement; hops += 1; }
  if (!el || !LCC_PAGE_BLOCK_TAGS.has(el.tagName)) return null;
  if (el.closest(LCC_PAGE_EXCLUDE_SELECTOR)) return null;
  let full;
  try { full = el.textContent || ""; } catch (_) { return null; }
  if (full.length > LCC_PAGE_BLOCK_UNIT_MAX_CHARS) return null;
  const source = lccPageSourceNorm(full);
  if (source.length < 2 || !lccPageHasLetters(source) || lccPageAlreadyTarget(source)) return null;
  try {
    if (el.querySelector(LCC_PAGE_NESTED_BLOCK_SELECTOR)) return null;   // container, not a leaf block → per-node
    if (el.querySelector(LCC_PAGE_OPAQUE_SELECTOR)) return null;         // links/buttons/code → Policy R (future)
  } catch (_) { return null; }
  const members = [];
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const parent = t.parentElement;
    if (!parent || parent.closest(LCC_PAGE_EXCLUDE_SELECTOR)) return null;   // excluded text mixed in → bail to per-node
    const core = lccPageTextParts(t.nodeValue).core;
    if (!core || !lccPageHasLetters(core) || /^[\d\s.,:%()+\-–—/\\]+$/.test(core)) continue;
    members.push(t);
  }
  if (members.length < 2) return null;        // single fragment → the existing per-node path handles it better
  return { el, members, source, hot: lccPageInViewport(el) };
}
function lccPageBindBlockMembers(members, anchor, key) {
  // Record each fragment's pre-translation value so the result is verifiable (node-identity) and restorable.
  for (const m of members) {
    const st = lccPageStateFor(m);
    lccPageClearPartialState(st);
    st.blockKey = key;
    st.blockAnchor = (m === anchor);
    st.emptied = false;
    st.expectedFull = m.nodeValue;
    st.originalFull = m.nodeValue;
    st.pending = !!key;
  }
}
function lccPageApplyBlockTarget(members, anchor, source, target) {
  // Collapse: whole-block translation into the anchor, blank the rest. Each node is touched only if it still
  // holds exactly its recorded source (else a later scan re-queues it) — never corrupts a node that changed.
  let applied = 0;
  for (const m of members) {
    const st = lccPageTranslateState.get(m);
    if (!m || !st || !m.isConnected || m.nodeValue !== st.expectedFull) continue;
    if (m === anchor) {
      st.source = source; st.sourceNorm = lccPageSourceNorm(source);
      st.pre = ""; st.post = "";
      st.translated = target; st.translatedFull = target;
      st.emptied = false; st.pending = false;
      m.nodeValue = target;                    // direct DOM replacement, not an overlay
    } else {
      st.translatedFull = ""; st.emptied = true; st.pending = false;
      m.nodeValue = "";                        // fragment folded into the anchor's full-block translation
    }
    applied += 1;
  }
  return applied;
}
function lccPageQueueBlockUnit(unit) {
  const { el, members, source, hot } = unit;
  const anchor = members[0];
  const anchorState = lccPageStateFor(anchor);
  if (anchorState.translatedFull && anchor.nodeValue === anchorState.translatedFull) return false;  // already done
  lccPageBilingualCaptureEl(el);               // hover over the collapsed anchor reveals the whole block's original
  const cached = lccPageCachedEntry(source);
  if (cached) {
    lccPageBindBlockMembers(members, anchor, "");
    if (lccPageApplyBlockTarget(members, anchor, source, cached.target) > 0) {
      lccPageTranslateStats.applied += 1;
      lccPageBilingualInlineMark(el);
      if (cached.kind !== "url") lccPageVerifyEnqueue(anchor, source, cached.target);
    }
    return false;
  }
  const key = "blk" + lccPageTranslateEpoch + "-" + (++lccPageBlockSeq);
  lccPageBindBlockMembers(members, anchor, key);
  lccPageWork.set(key, {
    text: source, norm: lccPageSourceNorm(source), ctx: "",
    nodes: new Set(members), status: "queued", hot,
    block: { el, anchor, members },
  });
  (hot ? lccPageHotQueue : lccPageColdQueue).push(key);
  lccPageTranslateStats.blockUnits += 1;
  lccPageScheduleFlush();
  return true;
}
function lccPageApplyBlockResult(key, work, source, target, engine) {
  if (work.block.kind === "R") { lccPageApplyBlockResultR(key, work, target); return; }
  const { anchor, members } = work.block;
  if (!target) {
    for (const m of members) { const st = lccPageTranslateState.get(m); if (st) st.pending = false; }
    lccPageTranslateStats.dropEmpty += 1;      // model rendered empty: keep source, next scan re-queues
  } else {
    const applied = lccPageApplyBlockTarget(members, anchor, source || work.text, target);
    if (applied > 0) {
      lccPageRememberCache(work.text, target);
      lccPageTranslateStats.applied += applied;
      lccPageBilingualInlineMark(work.block.el);
      if (engine === "aux") lccPageVerifyEnqueue(anchor, source || work.text, target);   // speed layer painted -> quality layer re-checks in idle
    }
    else lccPageTranslateStats.dropChanged += 1;
  }
  lccPageWork.delete(key);
}
// ---- semantic block batching (Policy R: placeholder-preserving inline translation) -------------------
// A block holding interactive/identifier nodes (links, buttons, code) can't be collapsed — those must keep
// their DOM position and never be translated. Policy R serializes the block with each preserved node as an
// inline ⟦n⟧ placeholder, so the model translates the surrounding text as one sentence and the placeholders
// ride along to wherever the target word order puts them. The model must echo every ⟦n⟧ verbatim, in
// ascending order, exactly once (strict — like the @@n@@ markers); the result is split on the placeholders
// back into per-gap text segments, each collapsed (Policy A) into its gap's first text node. If the model
// corrupts the placeholders, the block opts out of block mode and re-queues per-node (existing behavior).
const LCC_PAGE_PH_OPEN = "⟦", LCC_PAGE_PH_CLOSE = "⟧";   // ⟦ ⟧
function lccPagePh(i) { return LCC_PAGE_PH_OPEN + i + LCC_PAGE_PH_CLOSE; }
function lccPageAdvancePastSubtree(walker, root) {
  let n = walker.nextNode();
  while (n && root.contains(n)) n = walker.nextNode();   // skip the preserved subtree's descendants
  return n;
}
function lccPageBlockUnitForR(node) {
  if (!LCC_PAGE_BLOCK_UNIT) return null;
  const st0 = lccPageTranslateState.get(node);
  if (st0 && st0.blockOptOut) return null;               // a prior R attempt corrupted → stay per-node
  if (node.parentElement && node.parentElement.closest(LCC_PAGE_OPAQUE_SELECTOR)) return null;   // link text etc. is a preserved node — translate it per-node in place, don't let it (re)trigger the block's R
  let el = node.parentElement, hops = 0;
  while (el && hops < 6 && !LCC_PAGE_BLOCK_TAGS.has(el.tagName)) { el = el.parentElement; hops += 1; }
  if (!el || !LCC_PAGE_BLOCK_TAGS.has(el.tagName)) return null;
  if (el.closest(LCC_PAGE_EXCLUDE_SELECTOR)) return null;
  let full; try { full = el.textContent || ""; } catch (_) { return null; }
  const fullNorm = lccPageSourceNorm(full);
  if (full.length > LCC_PAGE_BLOCK_UNIT_MAX_CHARS || fullNorm.length < 2 || lccPageAlreadyTarget(fullNorm)) return null;
  let opaque;
  try {
    if (el.querySelector(LCC_PAGE_NESTED_BLOCK_SELECTOR)) return null;   // not a leaf text block
    opaque = el.querySelectorAll(LCC_PAGE_OPAQUE_SELECTOR);
  } catch (_) { return null; }
  if (!opaque || !opaque.length) return null;            // no preserved node → Policy A's territory
  const opaqueEls = new Set(opaque);
  const segs = [{ nodes: [] }];                          // gap 0 (before the first placeholder)
  let serial = "", phCount = 0, hasText = false;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let cur = walker.nextNode();
  while (cur) {
    if (cur.nodeType === Node.ELEMENT_NODE) {
      if (opaqueEls.has(cur)) {                          // preserved subtree → one inline placeholder, skip inside
        phCount += 1;
        serial += " " + lccPagePh(phCount) + " ";
        segs.push({ nodes: [] });
        cur = lccPageAdvancePastSubtree(walker, cur);
        continue;
      }
      cur = walker.nextNode();
      continue;
    }
    const parent = cur.parentElement;                    // text node
    if (!parent || parent.closest(LCC_PAGE_EXCLUDE_SELECTOR)) return null;
    const val = cur.nodeValue || "";
    serial += val;
    const core = lccPageTextParts(val).core;
    if (core && lccPageHasLetters(core) && !/^[\d\s.,:%()+\-–—/\\]+$/.test(core)) {
      segs[segs.length - 1].nodes.push(cur);             // translatable fragment in the current gap
      hasText = true;
    }
    cur = walker.nextNode();
  }
  if (!hasText || phCount < 1) return null;
  const members = [];
  for (const seg of segs) { seg.anchor = seg.nodes[0] || null; for (const n of seg.nodes) members.push(n); }
  const serialNorm = lccPageSourceNorm(serial);
  if (!members.length || !serialNorm) return null;
  return { el, segs, members, serial: serialNorm, phCount, hot: lccPageInViewport(el) };
}
function lccPageBindBlockMembersR(members, key) {
  for (const m of members) {
    const st = lccPageStateFor(m);
    lccPageClearPartialState(st);
    st.blockKey = key; st.emptied = false; st.blockOptOut = false;
    st.expectedFull = m.nodeValue; st.originalFull = m.nodeValue; st.pending = true;
  }
}
function lccPageQueueBlockUnitR(unit) {
  const { el, members, segs, serial, phCount, hot } = unit;
  lccPageBilingualCaptureEl(el);
  const key = "blkr" + lccPageTranslateEpoch + "-" + (++lccPageBlockSeq);
  lccPageBindBlockMembersR(members, key);
  lccPageWork.set(key, {
    text: serial, norm: lccPageSourceNorm(serial), ctx: "",
    nodes: new Set(members), status: "queued", hot,
    block: { kind: "R", el, segs, phCount, members },
  });
  (hot ? lccPageHotQueue : lccPageColdQueue).push(key);
  lccPageTranslateStats.blockUnits += 1;
  lccPageScheduleFlush();
  return true;
}
function lccPageMapPhSegments(text, phCount) {
  // Split on ⟦n⟧ placeholders, requiring them to appear as the strict ascending 1..phCount sequence exactly
  // once each (a misordered/missing/duplicated placeholder would mis-map links → reject). Returns phCount+1
  // gap strings (the text between/around placeholders), or null to trigger per-node fallback.
  const re = /⟦\s*(\d+)\s*⟧/g;
  const out = []; let m, segStart = 0, seen = 0;
  while ((m = re.exec(text))) {
    if (Number(m[1]) !== seen + 1) return null;
    out.push(text.slice(segStart, m.index));
    segStart = m.index + m[0].length;
    seen += 1;
  }
  if (seen !== phCount) return null;
  out.push(text.slice(segStart));
  return out;
}
function lccPageApplySegCollapse(seg, target) {
  let applied = 0;
  for (let i = 0; i < seg.nodes.length; i += 1) {
    const m = seg.nodes[i];
    const st = lccPageTranslateState.get(m);
    if (!m || !st || !m.isConnected || m.nodeValue !== st.expectedFull) continue;   // changed under us → skip
    st.pending = false; st.pre = ""; st.post = ""; st.source = "";
    if (i === 0 && target) { st.translatedFull = target; st.emptied = false; m.nodeValue = target; }
    else { st.translatedFull = ""; st.emptied = true; m.nodeValue = ""; }   // folded into the gap's first node, or word order emptied it
    applied += 1;
  }
  return applied;
}
function lccPageBlockOptOutAndRequeue(work) {
  for (const m of work.block.members) {
    const st = lccPageTranslateState.get(m);
    if (st) { st.blockOptOut = true; st.blockKey = ""; st.pending = false; }   // never block-batch these nodes again
  }
  lccPageScheduleScan(0);                                                       // re-queue them via the per-node path
}
function lccPageApplyBlockResultR(key, work, target) {
  const { segs, phCount } = work.block;
  if (!target) { lccPageBlockOptOutAndRequeue(work); lccPageTranslateStats.dropEmpty += 1; lccPageWork.delete(key); return; }
  const segTexts = lccPageMapPhSegments(String(target), phCount);
  if (!segTexts || segTexts.length !== segs.length) {
    lccPageBlockOptOutAndRequeue(work);                  // model broke the placeholders → per-node fallback
    lccPageTranslateStats.dropChanged += 1;
    lccPageWork.delete(key);
    return;
  }
  let applied = 0;
  for (let i = 0; i < segs.length; i += 1) applied += lccPageApplySegCollapse(segs[i], segTexts[i].trim());
  lccPageTranslateStats.applied += applied;
  if (applied > 0) lccPageBilingualInlineMark(work.block.el);
  lccPageWork.delete(key);
}
function lccPageQueueNode(node) {
  if (!lccPageNodeAllowed(node)) return false;
  const prior = lccPageTranslateState.get(node);
  if (prior && prior.blockKey && lccPageWork.has(prior.blockKey)) return false;   // already claimed by a live block unit
  if (LCC_PAGE_BLOCK_UNIT) {
    const unit = lccPageBlockUnitFor(node);             // Policy A: pure style-split block → collapse to anchor
    if (unit) return lccPageQueueBlockUnit(unit);
    const unitR = lccPageBlockUnitForR(node);           // Policy R: block with links/buttons → placeholder-preserving
    if (unitR) return lccPageQueueBlockUnitR(unitR);
  }
  const state = lccPageStateFor(node);
  lccPageBilingualCapture(node);   // snapshot the parent's original text before any partial/final mutates it
  const expectedFull = node.nodeValue;
  let sourceFull = expectedFull;
  if (state.translatedFull && expectedFull.startsWith(state.translatedFull)) {
    sourceFull = (state.originalFull || "") + expectedFull.slice(state.translatedFull.length);
  }
  const parts = lccPageTextParts(sourceFull);
  const cached = lccPageCachedEntry(parts.core);
  if (cached) {
    lccPageApplyToNode(node, state, parts.core, cached.target, expectedFull, parts.pre, parts.post, sourceFull);
    lccPageTranslateStats.applied += 1;
    if (cached.kind !== "url") lccPageVerifyEnqueue(node, parts.core, cached.target);   // cross-site/seed -> re-check when idle
    return false;
  }
  state.source = parts.core;
  state.sourceNorm = lccPageSourceNorm(parts.core);
  state.pre = parts.pre;
  state.post = parts.post;
  state.expectedFull = expectedFull;
  state.originalFull = sourceFull;
  state.pending = false;
  const key = lccPageSourceKey(parts.core);
  const hot = lccPageInViewport(node.parentElement);
  let work = lccPageWork.get(key);
  if (work) {
    work.nodes.add(node);                 // another node with the same text -> rides the one in-flight call
    if (hot && !work.hot && work.status === "queued") {   // scrolled into view -> jump to the hot queue
      work.hot = true;
      lccPageHotQueue.push(key);
    }
    return false;
  }
  work = { text: parts.core, norm: lccPageSourceNorm(parts.core), ctx: lccPageBlockContext(node, parts.core), nodes: new Set([node]), status: "queued", hot };
  lccPageWork.set(key, work);
  (hot ? lccPageHotQueue : lccPageColdQueue).push(key);
  lccPageScheduleFlush();
  return true;
}
function lccPageScanNode(root, limit) {
  if (!root || !lccPageTranslateOn || !lccPageFrameAllowed()) return 0;
  limit = limit == null ? lccPageTranslatePolicy().scanLimit : limit;
  let count = 0;
  if (root.nodeType === Node.TEXT_NODE) {
    return lccPageQueueNode(root) ? 1 : 0;
  }
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node && count < limit) {
    if (lccPageQueueNode(node)) count += 1;
    node = walker.nextNode();
  }
  return count;
}
function lccPageRoot() {
  const selector = String(lccPageTranslateSettings.pageTranslateSelector || "body").trim() || "body";
  try { return document.querySelector(selector) || document.body || document.documentElement; }
  catch (_) { return document.body || document.documentElement; }
}
function lccPageRequestIdle(fn, timeout) {
  if (typeof requestIdleCallback === "function") return requestIdleCallback(fn, { timeout });
  return setTimeout(fn, 16);
}
function lccPageCancelIdle(id) {
  if (!id) return;
  if (typeof cancelIdleCallback === "function") { try { cancelIdleCallback(id); } catch (_) {} }
  clearTimeout(id);
}
function lccPageNow() {
  return (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
}
function lccPageStartScan(prefetch) {
  lccPageCancelIdle(lccPageScanIdleId); lccPageScanIdleId = 0;
  if (!lccPageTranslateOn || !lccPageFrameAllowed()) { lccPageScanCursor = null; return; }
  const root = lccPageRoot();
  if (!root) { lccPageScanCursor = null; return; }
  lccPageScanCursor = { walker: document.createTreeWalker(root, NodeFilter.SHOW_TEXT), prefetch: !!prefetch, seen: 0 };
  lccPageScanChunk();
}
function lccPageScanChunk() {
  lccPageScanIdleId = 0;
  const cur = lccPageScanCursor;
  if (!cur || !lccPageTranslateOn) { lccPageScanCursor = null; return; }
  const t0 = lccPageNow();
  let processed = 0;
  lccPagePrefetchScanning = cur.prefetch;     // lccPageNodeAllowed relaxes the viewport gate while this is set
  try {
    let node = cur.walker.nextNode();
    while (node) {
      lccPageQueueNode(node);
      cur.seen += 1;
      processed += 1;
      if (cur.prefetch && cur.seen >= LCC_PAGE_PREFETCH_MAX_NODES) { cur.capped = true; node = null; break; }
      if (processed >= LCC_PAGE_SCAN_CHUNK_NODES || (lccPageNow() - t0) > LCC_PAGE_SCAN_SLICE_MS) {
        lccPageScanIdleId = lccPageRequestIdle(lccPageScanChunk, 400);   // resume next idle tick
        return;                                                          // (finally restores the flag)
      }
      node = cur.walker.nextNode();
    }
  } finally {
    lccPagePrefetchScanning = false;
  }
  lccPageScanCursor = null;                    // walker exhausted (or prefetch hit its cap)
  if (cur.prefetch) {
    lccPagePrefetchDone = true;
    if (cur.capped) try { console.debug("[lcc] page prefetch capped at", LCC_PAGE_PREFETCH_MAX_NODES, "nodes"); } catch (_) {}
  } else {
    lccPageMaybePrefetch();                    // viewport pass done -> prefetch the rest when idle
  }
}
function lccPageMaybePrefetch() {
  if (!lccPageTranslateOn || !lccPageFrameAllowed() || lccPagePrefetchDone || lccPageScanCursor || lccPagePrefetchTimer) return;
  if (lccPageHotQueue.length || lccPageColdQueue.length || lccPageTranslateRequests.size) return;
  lccPagePrefetchTimer = lccPageRequestIdle(() => {
    lccPagePrefetchTimer = 0;
    if (lccPagePrefetchDone || lccPageScanCursor) return;
    if (lccPageHotQueue.length || lccPageColdQueue.length || lccPageTranslateRequests.size) return;   // still idle?
    lccPageStartScan(true);                    // whole-doc relaxed scan -> off-screen text queued as cold
  }, 1500);
}
// On a dual-model bridge the speed layer (aux/E2B) paints first — the quality re-check on the main
// model is part of the DESIGN, not an option, so seeing any engine:"aux" result enables the verify
// pipeline for the session. The pageVerify toggle still opts single-model setups in (cached labels).
let lccPageAuxSeen = false;
function lccPageVerifyEnabled() {
  return lccPageTranslateSettings.pageVerify === true || lccPageAuxSeen;
}
function lccPageVerifyEnqueue(node, source, shown) {
  if (!lccPageVerifyEnabled()) return;
  const key = lccPageSourceKey(source);
  const v = lccPageVerify.get(key);
  if (v) { v.nodes.add(node); return; }      // already pending -> just collect the node for fan-out patch
  if (lccPageVerifySeen.has(key)) return;    // already checked this page
  lccPageVerifySeen.add(key);
  lccPageVerify.set(key, { source, shown, nodes: new Set([node]) });
  lccPageVerifyQueue.push(key);
  lccPageVerifyScheduleIdle();
}
function lccPageVerifyScheduleIdle() {
  if (lccPageVerifyTimer || !lccPageVerifyEnabled()) return;
  lccPageVerifyTimer = lccPageRequestIdle(() => { lccPageVerifyTimer = 0; lccPageVerifyFlush(); }, 3000);
}
function lccPageRouteFailureReason(e) {
  if (!e) return "unknown error";
  if (typeof e === "string") return e;
  return String(e.error || e.msg || e.message || (e.routed === false ? "not routed" : e));
}
function lccPageWarnBatchRouteFailure(requestId, reason) {
  const now = Date.now();
  if (now - lccPageRouteWarnAt < LCC_PAGE_ROUTE_WARN_INTERVAL_MS) return;
  lccPageRouteWarnAt = now;
  console.warn("[lcc] page batch routing failed:", requestId, lccPageRouteFailureReason(reason));
}
function lccPageBatchRouteFailed(requestId, reason) {
  lccPageWarnBatchRouteFailure(requestId, reason);
  if (lccPageVerifyRequests.has(requestId)) { lccPageVerifyDone(requestId); return; }
  lccPageTranslateRetry({ request_id: requestId, retry_ms: 500 });
}
function lccPageSendBatch(requestId, items, verify) {
  try {
    const sent = chrome.runtime.sendMessage({ type: "page-translate-batch", requestId, items, verify: verify === true });
    if (sent && typeof sent.then === "function") {
      sent
        .then((res) => { if (res && (res.ok === false || res.routed === false)) lccPageBatchRouteFailed(requestId, res); })
        .catch((e) => lccPageBatchRouteFailed(requestId, e));
    }
  } catch (e) {
    lccPageBatchRouteFailed(requestId, e);
  }
}
function lccPageVerifyFlush() {
  if (!lccPageTranslateOn || !lccPageVerifyEnabled() || !lccPageVerifyQueue.length) return;
  // never compete with visible translation — only when the whole pipeline is idle
  if (lccPageVerifyInflight || lccPageHotQueue.length || lccPageColdQueue.length ||
      lccPageTranslateRequests.size || lccPageScanCursor) { lccPageVerifyScheduleIdle(); return; }
  const items = [];
  while (lccPageVerifyQueue.length && items.length < LCC_PAGE_VERIFY_BATCH) {
    const key = lccPageVerifyQueue.shift();
    const v = lccPageVerify.get(key);
    if (!v) continue;
    for (const n of [...v.nodes]) {            // keep only nodes still showing the cached value
      const st = lccPageTranslateState.get(n);
      if (!n.isConnected || !st || n.nodeValue !== st.translatedFull) v.nodes.delete(n);
    }
    if (!v.nodes.size) { lccPageVerify.delete(key); continue; }
    items.push({ id: key, text: v.source });
  }
  if (!items.length) return;
  const requestId = "ptv" + LCC_PAGE_FRAME_TAG + lccPageTranslateEpoch + "-" + (++lccPageVerifyReqSeq);
  const keys = items.map((it) => it.id);
  const timer = setTimeout(() => lccPageVerifyDone(requestId), 30000);
  lccPageVerifyRequests.set(requestId, { keys, timer });
  lccPageVerifyInflight = requestId;
  lccPageSendBatch(requestId, items, true);   // verify rides the MAIN model (quality re-check)
}
function lccPageVerifyApply(msg) {
  const key = String(msg.item_id || "");
  const v = lccPageVerify.get(key);
  if (v) lccPageVerify.delete(key);
  if (!v) return;
  const source = String(msg.source || "");
  const target = String(msg.target || "").trim();
  if (!target || lccPageSourceNorm(source) !== lccPageSourceNorm(v.source)) return;
  lccPageRememberCache(v.source, target);    // store the context-correct rendering for this page
  if (target === v.shown) return;            // cache was right -> nothing to patch
  for (const node of v.nodes) {              // quietly patch nodes still showing the superseded cached value
    const st = lccPageTranslateState.get(node);
    if (!node || !st || !node.isConnected || node.nodeValue !== st.translatedFull) continue;
    lccPageApplyToNode(node, st, v.source, target, st.expectedFull, st.pre, st.post, st.originalFull);
  }
}
function lccPageVerifyDone(requestId) {
  const req = lccPageVerifyRequests.get(requestId);
  if (req && req.timer) clearTimeout(req.timer);
  lccPageVerifyRequests.delete(requestId);
  if (lccPageVerifyInflight === requestId) lccPageVerifyInflight = "";
  if (req) for (const k of req.keys) lccPageVerify.delete(k);   // drop any that got no result (best-effort)
  if (lccPageVerifyQueue.length) lccPageVerifyScheduleIdle();
}
function lccPageScheduleScan(ms = 180) {
  if (lccPageTranslateScanTimer) clearTimeout(lccPageTranslateScanTimer);
  lccPageTranslateScanTimer = setTimeout(() => {
    lccPageTranslateScanTimer = null;
    lccPageStartScan(false);                   // near-viewport, time-sliced; prefetch fires separately when idle
  }, ms);
}
function lccPageScheduleFlush(ms) {
  if (lccPageTranslateFlushTimer) return;
  ms = ms == null ? lccPageTranslatePolicy().flushMs : ms;
  lccPageTranslateFlushTimer = setTimeout(lccPageFlush, ms);
}
function lccPageFlush() {
  lccPageTranslateFlushTimer = null;
  if (!lccPageTranslateOn) return;
  const policy = lccPageTranslatePolicy();
  if (lccPageTranslateRequests.size >= policy.maxInflight) return;
  const items = [];
  let chars = 0;
  // Drain hot (in-viewport, reading order) before cold. Both arrays may hold stale keys (already batched, or
  // promoted hot->cold dupes) — those are skipped lazily. Returns false when the char budget stops the batch.
  const pull = (queue, wantHot) => {
    while (queue.length && items.length < policy.batchSize) {
      const key = queue[0];
      const work = lccPageWork.get(key);
      if (!work || work.status !== "queued" || work.hot !== wantHot) { queue.shift(); continue; }
      if (!lccPagePruneWork(work)) { queue.shift(); lccPageWork.delete(key); continue; }
      if (chars + work.text.length > policy.batchChars && items.length) return false;
      queue.shift();
      work.status = "pending";
      for (const n of work.nodes) { const st = lccPageTranslateState.get(n); if (st) st.pending = true; }
      items.push(work.ctx ? { id: key, text: work.text, ctx: work.ctx } : { id: key, text: work.text });
      chars += work.text.length;
    }
    return true;
  };
  if (pull(lccPageHotQueue, true)) pull(lccPageColdQueue, false);
  if (items.length) {
    const requestId = "ptr" + LCC_PAGE_FRAME_TAG + lccPageTranslateEpoch + "-" + (++lccPageTranslateReqSeq);
    const keys = items.map((it) => it.id);
    let maxLen = 0;
    for (const it of items) maxLen = Math.max(maxLen, (it.text || "").length);
    // Long items are sentence-chunked server-side; budget ~2s per ~500-char chunk on top of the base so a
    // genuinely long paragraph isn't declared timed-out mid-translation. Incremental partials also re-arm this.
    const timeoutMs = Math.min(180000, 30000 + Math.floor(maxLen / 500) * 2000);
    const timer = setTimeout(() => lccPageTranslateRetry({ request_id: requestId, retry_ms: 500 }), timeoutMs);
    lccPageTranslateRequests.set(requestId, { keys, timer, timeoutMs });
    lccPageSendBatch(requestId, items);
  }
  if ((lccPageHotQueue.length || lccPageColdQueue.length) && lccPageTranslateRequests.size < policy.maxInflight) {
    lccPageScheduleFlush(Math.max(160, policy.flushMs * 2));
  }
}
function lccPageClearTransient(restore) {
  if (lccPageTranslateFlushTimer) { clearTimeout(lccPageTranslateFlushTimer); lccPageTranslateFlushTimer = null; }
  if (lccPageTranslateScanTimer) { clearTimeout(lccPageTranslateScanTimer); lccPageTranslateScanTimer = null; }
  lccPageCancelIdle(lccPageScanIdleId); lccPageScanIdleId = 0;
  lccPageCancelIdle(lccPagePrefetchTimer); lccPagePrefetchTimer = 0;
  lccPageScanCursor = null;
  lccPagePrefetchScanning = false;
  lccPagePrefetchDone = false;
  lccPageHotQueue.length = 0;
  lccPageColdQueue.length = 0;
  lccPageWork.clear();
  for (const req of lccPageTranslateRequests.values()) {
    if (req && req.timer) clearTimeout(req.timer);
  }
  lccPageTranslateRequests.clear();
  if (restore) {
    for (const node of lccPageTranslateNodes) {
      const state = lccPageTranslateState.get(node);
      if (state && node.isConnected) {
        const isFinal = state.translatedFull && node.nodeValue === state.translatedFull;
        const isPartial = state.partialFull && node.nodeValue === state.partialFull;
        const isEmptied = state.emptied && node.nodeValue === "";   // a fragment collapsed into its block anchor
        if (isFinal || isPartial || isEmptied) node.nodeValue = state.originalFull || state.expectedFull || node.nodeValue;
      }
      lccPageTranslateState.delete(node);
    }
  } else {
    for (const node of lccPageTranslateNodes) {
      const state = lccPageTranslateState.get(node);   // URL-change clear: revert only in-flight speculative partials
      if (state && node.isConnected && state.partialFull && node.nodeValue === state.partialFull) {
        node.nodeValue = state.originalFull || state.expectedFull || node.nodeValue;
      }
      lccPageTranslateState.delete(node);
    }
  }
  lccPageTranslateNodes.clear();
  lccPageTranslateReqSeq = 0;
  lccPageTranslateEpoch += 1;
  lccPageCancelIdle(lccPageVerifyTimer); lccPageVerifyTimer = 0;
  for (const req of lccPageVerifyRequests.values()) { if (req && req.timer) clearTimeout(req.timer); }
  lccPageVerifyRequests.clear();
  lccPageVerify.clear();
  lccPageVerifyQueue.length = 0;
  lccPageVerifySeen.clear();
  lccPageVerifyInflight = "";
}
function lccPageNotifyReady() {
  if (!lccPageFrameAllowed()) return;
  try {
    // Subframes report empty context/url so background keeps the TOP frame's values; they still get the
    // pageTranslating + settings reply and self-start (covers iframes loaded mid-session).
    chrome.runtime.sendMessage({
      type: "content-ready",
      isTop: LCC_IS_TOP,
      pageContext: LCC_IS_TOP ? lccPageContext() : "",
      pageUrl: LCC_IS_TOP ? location.href : "",
    }, (res) => {
      if (chrome.runtime.lastError || !res || !res.pageTranslating) return;
      lccPageTranslateStart(res.settings || {});
    });
  } catch (_) {}
}
function lccPageHandleUrlOrContextChange() {
  if (!lccPageTranslateOn || !lccPageFrameAllowed()) return;
  const url = location.href;
  const ctx = lccPageContext();
  const urlChanged = url !== lccPageTranslateUrl;
  const ctxChanged = ctx !== lccPageTranslateLastContext;
  if (!urlChanged && !ctxChanged) return;
  lccPageTranslateUrl = url;
  lccPageTranslateLastContext = ctx;
  if (urlChanged) lccPageClearTransient(false);
  lccPageNotifyReady();
  lccPageLoadCache().finally(() => lccPageScheduleScan(urlChanged ? 200 : 0));
}
function lccPageStartUrlWatch() {
  if (lccPageTranslateUrlTimer) return;
  lccPageTranslateUrlTimer = setInterval(lccPageHandleUrlOrContextChange, 700);
}
function lccPageStopUrlWatch() {
  if (lccPageTranslateUrlTimer) {
    clearInterval(lccPageTranslateUrlTimer);
    lccPageTranslateUrlTimer = null;
  }
}
function lccPageBilingualEnabled() {
  return lccPageTranslateSettings.pageBilingual !== false || lccPageBilingualInlineEnabled();
}
// Inline ghost: keep the original visibly under the translated block (no hover needed). Long prose only —
// labels/buttons would double in size — and final translations only (partials stay clean).
const LCC_PAGE_INLINE_MIN_CHARS = 40;
const LCC_PAGE_INLINE_MAX_CHARS = 600;
function lccPageBilingualInlineEnabled() {
  return lccPageTranslateSettings.pageBilingualInline === true;
}
function lccPageBilingualInlineMark(el) {
  if (!lccPageBilingualInlineEnabled() || !el) return;
  const orig = lccBilingualOrig.get(el);
  if (!orig) return;
  const trimmed = orig.replace(/\s+/g, " ").trim();
  if (trimmed.length < LCC_PAGE_INLINE_MIN_CHARS) return;
  try {
    el.setAttribute("data-lcc-orig",
      trimmed.length > LCC_PAGE_INLINE_MAX_CHARS ? trimmed.slice(0, LCC_PAGE_INLINE_MAX_CHARS) + "…" : trimmed);
    el.classList.add("lcc-bi-inline");
  } catch (_) {}
}
function lccPageBilingualInlineClearAll() {
  try {
    for (const el of document.querySelectorAll(".lcc-bi-inline")) {
      el.classList.remove("lcc-bi-inline");
      el.removeAttribute("data-lcc-orig");
    }
  } catch (_) {}
}
function lccPageBilingualCapture(node) {
  if (!lccPageBilingualEnabled()) return;
  const parent = node.parentElement;
  if (!parent || lccBilingualOrig.has(parent)) return;
  try {
    const orig = parent.textContent;
    if (!orig || !orig.trim() || orig.length > LCC_PAGE_BILINGUAL_MAX_CHARS) return;
    lccBilingualOrig.set(parent, orig);
    parent.classList.add("lcc-bi-src");
  } catch (_) {}
}
function lccPageBilingualCaptureEl(el) {
  // Block units collapse into the anchor, so snapshot the whole block element (not a single fragment's
  // parent) — hover over the collapsed anchor then reveals the entire original block.
  if (!lccPageBilingualEnabled() || !el || lccBilingualOrig.has(el)) return;
  try {
    const orig = el.textContent;
    if (!orig || !orig.trim() || orig.length > LCC_PAGE_BILINGUAL_MAX_CHARS) return;
    lccBilingualOrig.set(el, orig);
    el.classList.add("lcc-bi-src");
  } catch (_) {}
}
function lccPageBilingualEnsureGhost() {
  if (lccBilingualGhost && lccBilingualGhost.isConnected) return lccBilingualGhost;
  const g = document.createElement("div");
  g.id = "lcc-bilingual-ghost";
  g.setAttribute("aria-hidden", "true");
  g.style.cssText = [
    "position:fixed", "z-index:2147483647", "max-width:min(480px,90vw)", "padding:6px 9px",
    "background:rgba(20,20,22,0.92)", "color:#f4f1ea", "font:13px/1.45 -apple-system,system-ui,sans-serif",
    "border-radius:6px", "box-shadow:0 4px 18px rgba(0,0,0,0.35)", "pointer-events:none",
    "white-space:pre-wrap", "overflow-wrap:anywhere", "display:none", "left:0", "top:0", "margin:0",
  ].join(";");
  (document.body || document.documentElement).appendChild(g);
  lccBilingualGhost = g;
  return g;
}
function lccPageBilingualShow(el) {
  const orig = lccBilingualOrig.get(el);
  if (!orig) return;
  const g = lccPageBilingualEnsureGhost();
  g.textContent = orig;
  g.style.display = "block";
  const rect = el.getBoundingClientRect();
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const gw = Math.min(g.offsetWidth || 300, vw - 8);
  const gh = g.offsetHeight || 24;
  let top = rect.bottom + 6;
  if (top + gh > vh - 4) top = Math.max(4, rect.top - gh - 6);   // flip above near the bottom edge
  g.style.left = Math.max(4, Math.min(rect.left, vw - gw - 4)) + "px";
  g.style.top = top + "px";
}
function lccPageBilingualHideGhost() {
  if (lccBilingualGhost) lccBilingualGhost.style.display = "none";
}
function lccPageBilingualOnOver(e) {
  if (!lccPageBilingualEnabled()) return;
  const t = e.target;
  const el = t && t.closest ? t.closest(".lcc-bi-src") : null;
  if (el && lccBilingualOrig.has(el)) lccPageBilingualShow(el);
  else lccPageBilingualHideGhost();
}
function lccPageBilingualOnOut(e) {
  const to = e.relatedTarget;
  if (!to || !(to.closest && to.closest(".lcc-bi-src"))) lccPageBilingualHideGhost();
}
function lccPageBilingualStart() {
  if (lccBilingualOver) return;
  lccBilingualOver = lccPageBilingualOnOver;
  lccBilingualOut = lccPageBilingualOnOut;
  lccBilingualHide = () => lccPageBilingualHideGhost();
  document.addEventListener("mouseover", lccBilingualOver, true);
  document.addEventListener("mouseout", lccBilingualOut, true);
  window.addEventListener("scroll", lccBilingualHide, { passive: true, capture: true });
}
function lccPageBilingualStop() {
  if (lccBilingualOver) { document.removeEventListener("mouseover", lccBilingualOver, true); lccBilingualOver = null; }
  if (lccBilingualOut) { document.removeEventListener("mouseout", lccBilingualOut, true); lccBilingualOut = null; }
  if (lccBilingualHide) { window.removeEventListener("scroll", lccBilingualHide, true); lccBilingualHide = null; }
  if (lccBilingualGhost) { try { lccBilingualGhost.remove(); } catch (_) {} lccBilingualGhost = null; }
  try { for (const el of document.querySelectorAll(".lcc-bi-src")) el.classList.remove("lcc-bi-src"); } catch (_) {}
  lccPageBilingualInlineClearAll();
}
function lccPageTranslateStart(rawSettings) {
  if (!lccPageFrameAllowed()) return;
  const wasOn = lccPageTranslateOn;
  lccPageTranslateSettings = globalThis.lccNormalizeSettings({ ...lccPageTranslateSettings, ...(rawSettings || {}) });
  if (!wasOn) { lccPageTranslateEpoch += 1; lccPageTranslateReqSeq = 0; }
  lccPageTranslateConfigSig = lccPageConfigSignature();
  lccPageTranslateOn = true;
  lccPageTranslateUrl = location.href;
  lccPageTranslateLastContext = lccPageContext();
  document.documentElement.dataset.lccPageTranslate = "on";
  if (!lccPageTranslateObserver) {
    lccPageTranslateObserver = new MutationObserver((records) => {
      if (!lccPageTranslateOn) return;
      for (const r of records) {
        if (r.type === "characterData") lccPageQueueNode(r.target);
        else if (r.type === "childList") {
          for (const n of r.addedNodes) lccPageScanNode(n, 30);
        }
      }
    });
    lccPageTranslateObserver.observe(document.documentElement, { subtree: true, childList: true, characterData: true });
  }
  if (!lccPageTranslateScrollHandler) {
    lccPageTranslateScrollHandler = () => lccPageScheduleScan(220);
    window.addEventListener("scroll", lccPageTranslateScrollHandler, { passive: true });
    window.addEventListener("resize", lccPageTranslateScrollHandler, { passive: true });
  }
  lccPageStartUrlWatch();
  lccPageBilingualStart();   // hover shows the original; capture happens at queue time
  lccPageLoadCache().finally(() => lccPageScheduleScan(0));
}
function lccPageTranslateConfig(rawSettings) {
  if (!lccPageTranslateOn) return;
  const prevSig = lccPageTranslateConfigSig || lccPageConfigSignature();
  lccPageTranslateSettings = globalThis.lccNormalizeSettings({ ...lccPageTranslateSettings, ...(rawSettings || {}) });
  const nextSig = lccPageConfigSignature();
  if (nextSig !== prevSig) lccPageClearTransient(true);
  lccPageTranslateConfigSig = nextSig;
  lccPageLoadCache().finally(() => lccPageScheduleScan(0));
}
function lccPageTranslateStop(restore) {
  lccPageTranslateOn = false;
  delete document.documentElement.dataset.lccPageTranslate;
  if (lccPageTranslateObserver) { lccPageTranslateObserver.disconnect(); lccPageTranslateObserver = null; }
  if (lccPageTranslateScrollHandler) {
    window.removeEventListener("scroll", lccPageTranslateScrollHandler);
    window.removeEventListener("resize", lccPageTranslateScrollHandler);
    lccPageTranslateScrollHandler = null;
  }
  lccPageStopUrlWatch();
  lccPageBilingualStop();
  lccPageClearTransient(restore);
}
function lccPageTranslatePartial(msg) {
  if (!lccPageTranslateOn || lccPageTranslateSettings.pageTranslateStream === "final") return;
  lccPageTranslateStats.partialSeen += 1;
  const key = String(msg.item_id || "");
  const requestId = String(msg.request_id || "");
  if (!lccPageRequestOwns(requestId, key)) return;          // late/foreign result must not paint current work
  const reqRec = lccPageTranslateRequests.get(requestId);   // a streaming partial means the long request is alive → re-arm its timeout
  if (reqRec && reqRec.timer) {
    clearTimeout(reqRec.timer);
    reqRec.timer = setTimeout(() => lccPageTranslateRetry({ request_id: requestId, retry_ms: 500 }), reqRec.timeoutMs || 30000);
  }
  const source = String(msg.source || "");
  const sourceNorm = lccPageSourceNorm(source);
  const target = String(msg.target || "").trim();
  if (!target) return;
  const work = lccPageWork.get(key);
  if (!work || work.block) return;          // block units are final-only (no mid-stream collapse)
  const nodes = work.nodes;
  if (!nodes || !nodes.size) return;
  let applied = 0;
  for (const node of nodes) {
    const state = lccPageTranslateState.get(node);
    if (!node || !state || !node.isConnected) continue;
    if ((state.sourceNorm || lccPageSourceNorm(state.source)) !== sourceNorm) continue;
    if (lccPageApplyPartialToNode(node, state, source, target, requestId, sourceNorm)) applied += 1;
  }
  lccPageTranslateStats.partialApplied += applied;
}
function lccPageTranslateApply(msg) {
  if (!lccPageTranslateOn) return;
  if (msg.engine === "aux" && !lccPageAuxSeen) {
    lccPageAuxSeen = true;          // dual-model bridge detected -> idle 26B re-check becomes implicit
  }
  lccPageTranslateStats.resultSeen += 1;
  const key = String(msg.item_id || "");
  const requestId = String(msg.request_id || "");
  if (lccPageVerifyRequests.has(requestId)) { lccPageVerifyApply(msg); return; }   // cache-then-verify result
  if (!lccPageRequestOwns(requestId, key)) return;
  const source = String(msg.source || "");
  const sourceNorm = lccPageSourceNorm(source);
  const target = String(msg.target || "").trim();
  const work = lccPageWork.get(key);
  if (work && work.block) { lccPageApplyBlockResult(key, work, source, target, msg.engine); return; }   // Policy A collapse
  const nodes = work ? work.nodes : null;
  if (!nodes || !nodes.size) { lccPageTranslateStats.dropNoNode += 1; if (work) lccPageWork.delete(key); return; }
  if (!target) {
    // Drop on empty: clear pending + work so the node keeps its source text. Not a permanent loss — the
    // next scan (scroll/mutation) re-queues it. Requeuing here instead would loop forever on any string
    // the model deterministically renders empty.
    lccPageTranslateStats.dropEmpty += 1;
    for (const n of nodes) { const st = lccPageTranslateState.get(n); if (st) st.pending = false; }
    lccPageWork.delete(key);
    return;
  }
  let applied = 0;
  for (const node of nodes) {                     // one result paints every node that shared the normalized source text
    const state = lccPageTranslateState.get(node);
    if (!node || !state || !node.isConnected) { lccPageTranslateStats.dropNoNode += 1; continue; }
    if ((state.sourceNorm || lccPageSourceNorm(state.source)) !== sourceNorm) { lccPageTranslateStats.dropSource += 1; continue; }
    const isExpected = node.nodeValue === state.expectedFull;
    const isPartial = !!state.partialFull && node.nodeValue === state.partialFull;   // confirming a speculative partial
    const currentParts = lccPageTextParts(node.nodeValue);
    if (!isExpected && !isPartial && lccPageSourceNorm(currentParts.core) !== sourceNorm) { lccPageTranslateStats.dropChanged += 1; continue; }
    const pre = (isExpected || isPartial) ? (state.pre || "") : currentParts.pre;
    const post = (isExpected || isPartial) ? (state.post || "") : currentParts.post;
    const expectedForState = isPartial ? state.expectedFull : node.nodeValue;
    lccPageApplyToNode(node, state, source, target, expectedForState, pre, post);
    applied += 1;
  }
  if (applied > 0) {
    lccPageRememberCache(source, target);
    if (msg.engine === "aux") {                 // speed layer painted -> quality layer re-checks in idle
      const first = [...nodes].find((n) => n && n.isConnected);
      if (first) lccPageVerifyEnqueue(first, source, target);
    }
  }
  lccPageTranslateStats.applied += applied;
  lccPageWork.delete(key);
}
function lccPageTranslateDrop(msg) {            // dom_translate_err for one item -> give up on it (no requeue)
  const key = String(msg.item_id || "");
  const requestId = String(msg.request_id || "");
  if (lccPageVerifyRequests.has(requestId)) { lccPageVerifyDone(requestId); return; }
  if (!lccPageRequestOwns(requestId, key)) return;
  const work = lccPageWork.get(key);
  if (!work) return;
  for (const n of work.nodes) { const st = lccPageTranslateState.get(n); if (st) { lccPageRestorePartialNode(n, st); st.pending = false; } }
  lccPageWork.delete(key);
}
function lccPageRequeueKey(key) {
  const work = lccPageWork.get(key);
  if (!work || work.status !== "pending") return;
  if (!lccPagePruneWork(work)) { lccPageWork.delete(key); return; }
  for (const n of work.nodes) { const st = lccPageTranslateState.get(n); if (st) { lccPageRestorePartialNode(n, st); st.pending = false; } }
  work.status = "queued";
  (work.hot ? lccPageHotQueue : lccPageColdQueue).push(key);
}
function lccPageTranslateDone(msg) {            // batch finished; any still-pending key got no result -> requeue
  const requestId = String(msg.request_id || "");
  if (lccPageVerifyRequests.has(requestId)) { lccPageVerifyDone(requestId); return; }
  const req = lccPageTranslateRequests.get(requestId);
  if (req && req.timer) clearTimeout(req.timer);
  if (req) for (const key of req.keys) lccPageRequeueKey(key);
  lccPageTranslateRequests.delete(requestId);
  if (lccPageHotQueue.length || lccPageColdQueue.length) lccPageScheduleFlush(120);
  else lccPageMaybePrefetch();                  // visible + queued work drained -> prefetch the rest when idle
}
function lccPageTranslateRetry(msg) {           // busy/timeout -> requeue the whole request with backoff
  const requestId = String(msg.request_id || "");
  if (lccPageVerifyRequests.has(requestId)) { lccPageVerifyDone(requestId); return; }
  const req = lccPageTranslateRequests.get(requestId);
  if (req && req.timer) clearTimeout(req.timer);
  lccPageTranslateRequests.delete(requestId);
  if (req) for (const key of req.keys) lccPageRequeueKey(key);
  lccPageScheduleFlush(Math.max(500, Math.min(5000, Number(msg.retry_ms) || 1600)));
}

function lccResumePageTranslateIfActive() {
  lccPageNotifyReady();
}
