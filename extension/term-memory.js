// Tab semantic memory — the per-domain term store ("Archive Lens"). The bridge mines recurring
// proper-noun terms from caption finals and streams them as {type:"term_memory"}; the service worker
// persists them per DOMAIN here and seeds them back into the bridge config (autoGlossary) on the next
// start, so a returning visitor gets consistent names/terms from the first line — for captions AND the
// page DOM translator (one tab, one term memory). Pure data model up top (loadable in node tests like
// protocol.js); the two chrome.storage wrappers at the bottom run only in the service worker.
const LCC_TERM_MEMORY_KEY = "lcc-term-memory-v1";
const LCC_TERM_MEMORY_MAX_DOMAINS = 30;
const LCC_TERM_MEMORY_MAX_TERMS = 40;     // per domain, recency-ordered
const LCC_TERM_MEMORY_SEED_MAX = 60;      // lines handed to the bridge per session

globalThis.LCC_TERM_MEMORY_KEY = LCC_TERM_MEMORY_KEY;

globalThis.lccTermDomainOf = function lccTermDomainOf(url) {
  try {
    const u = new URL(String(url || ""));
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    return u.hostname.replace(/^www\./, "");
  } catch (_) {
    return "";
  }
};

// Merge one bridge term_memory payload into the store. Pure — returns a NEW store object. Re-seen terms
// move to the recency tail; an existing verbatim rendering survives an empty update; domain count is
// bounded by least-recently-updated eviction.
globalThis.lccTermMemoryMerge = function lccTermMemoryMerge(store, domain, terms, now) {
  if (!domain || !Array.isArray(terms) || !terms.length) return store || {};
  const prev = (store && store[domain] && Array.isArray(store[domain].terms)) ? store[domain].terms : [];
  const merged = new Map(prev
    .filter((p) => Array.isArray(p) && p[0])
    .map((p) => [String(p[0]).toLowerCase(), [String(p[0]), String(p[1] || "")]]));
  for (const it of terms) {
    if (!Array.isArray(it) || !it[0]) continue;
    const term = String(it[0]).slice(0, 80);
    const rendering = String(it[1] || "").slice(0, 80);
    const key = term.toLowerCase();
    const old = merged.get(key);
    merged.delete(key);                                      // re-insert -> recency order
    merged.set(key, [term, rendering || (old ? old[1] : "")]);
  }
  const next = { ...(store || {}) };
  next[domain] = { t: now, terms: [...merged.values()].slice(-LCC_TERM_MEMORY_MAX_TERMS) };
  const keep = Object.entries(next)
    .sort((a, b) => ((b[1] && b[1].t) || 0) - ((a[1] && a[1].t) || 0))
    .slice(0, LCC_TERM_MEMORY_MAX_DOMAINS);
  return Object.fromEntries(keep);
};

// Glossary-format seed lines ("term=rendering" / bare "term") for the bridge's autoGlossary config
// field, drawn from every distinct domain among the given URLs. Pure.
globalThis.lccTermSeedLines = function lccTermSeedLines(store, urls) {
  const domains = [...new Set((urls || []).map((u) => globalThis.lccTermDomainOf(u)).filter(Boolean))];
  const lines = [];
  for (const d of domains) {
    const rec = store && store[d];
    if (!rec || !Array.isArray(rec.terms)) continue;
    for (const p of rec.terms) {
      if (!Array.isArray(p) || !p[0]) continue;
      lines.push(p[1] ? `${p[0]}=${p[1]}` : String(p[0]));
    }
  }
  return [...new Set(lines)].slice(-LCC_TERM_MEMORY_SEED_MAX).join("\n");
};

// --- chrome.storage wrappers (service worker only; node tests exercise the pure parts above) ---
async function lccTermMemorySaveNow(terms, url) {
  const domain = globalThis.lccTermDomainOf(url);
  if (!domain || !Array.isArray(terms) || !terms.length) return;
  const raw = (await chrome.storage.local.get("lcc-settings"))["lcc-settings"] || {};
  if (globalThis.lccNormalizeSettings(raw).termMemory !== true) return;
  const store = (await chrome.storage.local.get(LCC_TERM_MEMORY_KEY))[LCC_TERM_MEMORY_KEY] || {};
  await chrome.storage.local.set({
    [LCC_TERM_MEMORY_KEY]: globalThis.lccTermMemoryMerge(store, domain, terms, Date.now()),
  });
}
let lccTermSaveChain = Promise.resolve();
globalThis.lccTermMemorySave = function lccTermMemorySave(terms, url) {
  // read-modify-write on a shared store: two close term_memory messages (caption mining + page mining)
  // interleave at the awaits and the later set() overwrites the earlier merge — chain saves instead.
  lccTermSaveChain = lccTermSaveChain
    .then(() => lccTermMemorySaveNow(terms, url))
    .catch((e) => console.warn("[lcc] term-memory save failed:", e && e.message || e));
  return lccTermSaveChain;
};

globalThis.lccTermMemorySeeds = async function lccTermMemorySeeds(urls) {
  const store = (await chrome.storage.local.get(LCC_TERM_MEMORY_KEY))[LCC_TERM_MEMORY_KEY] || {};
  return globalThis.lccTermSeedLines(store, urls);
};
