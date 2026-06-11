// Caption overlay over the YouTube/Twitch player. Display settings come from storage.local.
let box = null;
let lccPeek = false;   // #4 ghost: Alt (Option) held -> temporarily reveal the source line even when hidden
let lccLastSrc = "";   // #7: latest source line, used to prefill the live glossary bar
const LCC_IS_TOP = (window.top === window);
// With all_frames injection, exactly ONE frame renders captions: the video frame in video mode
// (it holds window.__lccVideoSub), otherwise the top frame. Prevents duplicate captions/transcript
// across iframes (and lets video mode reach a <video> inside a cross-origin iframe, e.g. Vimeo).
function lccShouldRender() { return (lccDelayMode === "video") ? !!window.__lccVideoSub : LCC_IS_TOP; }

function host() {
  return document.fullscreenElement || document.documentElement;
}
function ensureBox() {
  if (box && box.isConnected) return box;
  box = document.createElement("div");
  box.id = "lcc-overlay";
  box.innerHTML = '<div id="lcc-src"></div><div id="lcc-ko"></div><div id="lcc-debug"></div>';
  host().appendChild(box);
  applySettings();
  return box;
}
function applySettings() {
  if (!box) return;
  box.style.bottom = settings.bottomPct + "%";
  box.style.left = settings.leftPct + "%";
  box.style.right = "auto";
  box.style.transform = "translateX(-50%)";
  const ko = box.querySelector("#lcc-ko");
  const src = box.querySelector("#lcc-src");
  const dbg = box.querySelector("#lcc-debug");
  if (ko) ko.style.fontSize = settings.fontSize + "px";
  if (src) {
    src.style.fontSize = Math.round(settings.fontSize * 0.7) + "px";
    src.style.display = (settings.showSource || lccPeek) ? "block" : "none";
  }
  if (dbg) dbg.style.display = settings.debugSync ? "block" : "none";
}
function setSrc(text) {
  if (!lccShouldRender()) return;
  const b = ensureBox();
  b.style.display = "block";
  b.querySelector("#lcc-src").textContent = text || "";
  applySettings();
}
// #9 Trust gradient: when the number guard flags a translated line as number-uncertain, underline the
// digit runs (dotted) so the viewer knows to verify them against the source (hold Alt to peek it).
function lccRenderKoText(koEl, text, mark) {
  const s = text || "";
  if (!mark || !/\d/.test(s)) { koEl.textContent = s; return; }
  koEl.textContent = "";
  const re = /\d[\d.,:%\/\-]*/g;
  let last = 0, m;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) koEl.appendChild(document.createTextNode(s.slice(last, m.index)));
    const sp = document.createElement("span");
    sp.textContent = m[0];
    sp.style.borderBottom = "1px dotted rgba(255,196,0,.95)";
    sp.style.textUnderlineOffset = "2px";
    sp.title = "숫자 불확실 — 원문과 대조 (Alt: 원문 보기)";
    koEl.appendChild(sp);
    last = m.index + m[0].length;
  }
  if (last < s.length) koEl.appendChild(document.createTextNode(s.slice(last)));
}
function setLines(srcText, koText, debugText, isDraft, opts) {
  if (!lccShouldRender()) return;
  const b = ensureBox();
  b.style.display = "block";
  b.querySelector("#lcc-src").textContent = srcText || "";
  const ko = b.querySelector("#lcc-ko");
  lccRenderKoText(ko, koText, opts && opts.numUncertain);
  // Optimistic captioning: an in-progress (draft) translation is dimmed+italic; once committed
  // (stable) it snaps to solid. Reads as "the caption is completing", not "the caption flickered".
  ko.style.transition = "opacity .15s ease";
  ko.style.opacity = isDraft ? "0.62" : "1";
  ko.style.fontStyle = isDraft ? "italic" : "normal";
  b.querySelector("#lcc-debug").textContent = settings.debugSync ? (debugText || "") : "";
  applySettings();
}

function setKoSplit(koEl, stable, draft) {
  // render the Korean line as a locked (solid) prefix + an in-progress (dim italic) suffix.
  koEl.textContent = "";
  if (stable) {
    const s = document.createElement("span");
    s.textContent = draft ? stable + " " : stable;
    s.style.opacity = "1";
    s.style.fontStyle = "normal";
    koEl.appendChild(s);
  }
  if (draft) {
    const d = document.createElement("span");
    d.textContent = draft;
    d.style.opacity = "0.62";
    d.style.fontStyle = "italic";
    koEl.appendChild(d);
  }
}
function setLinesSplit(srcText, koStable, koDraft, debugText) {
  if (!lccShouldRender()) return;
  const b = ensureBox();
  b.style.display = "block";
  b.querySelector("#lcc-src").textContent = srcText || "";
  const ko = b.querySelector("#lcc-ko");
  ko.style.transition = "opacity .15s ease";
  ko.style.opacity = "1";          // per-span opacity now carries the draft dim
  ko.style.fontStyle = "normal";
  setKoSplit(ko, koStable, koDraft);
  b.querySelector("#lcc-debug").textContent = settings.debugSync ? (debugText || "") : "";
  applySettings();
}

// #4 Ghost: hold Alt (Option) to peek the source line while it's hidden. Reveal is temporary — release,
// focus loss, or tab-hide restores the configured visibility. No-op when showSource is already on.
const LCC_PEEK_DEBUG = false;   // 진단 로그 토글. NOTE: Atlas 등 에이전트 브라우저는 Option/Alt을 자체 후킹해 content script까지 keydown이 안 옴 → peek 무효. 일반 Chrome/Edge용.
function lccSetPeek(on) {
  if (lccPeek === on) return;
  lccPeek = on;
  if (LCC_PEEK_DEBUG) console.log("[lcc-peek] set", on, "box=", !!box, "showSource=", settings.showSource, "render=", lccShouldRender());
  applySettings();
}
function lccEditableTarget(t) {
  if (!t || !t.tagName) return false;
  return t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable === true;
}
try {
  if (LCC_PEEK_DEBUG) console.log("[lcc-peek] build loaded; top =", LCC_IS_TOP);
  // Gate on e.altKey (not e.key === "Alt") — the modifier flag is more robust across layouts/IME.
  window.addEventListener("keydown", (e) => {
    if (LCC_PEEK_DEBUG && e.altKey) console.log("[lcc-peek] keydown alt; key =", e.key, "editable =", lccEditableTarget(e.target), "top =", LCC_IS_TOP);
    if (e.altKey && !lccEditableTarget(e.target)) lccSetPeek(true);
  }, true);
  window.addEventListener("keyup", (e) => { if (!e.altKey) lccSetPeek(false); }, true);
  window.addEventListener("blur", () => lccSetPeek(false));
  document.addEventListener("visibilitychange", () => { if (document.hidden) lccSetPeek(false); });
} catch (_) {}

// ---- speaker tagging (diarize lite): prefix the KO line once a second speaker appears ----
const lccSpeakersSeen = new Set();
function lccResetSpeakers() { lccSpeakersSeen.clear(); }   // new session: a past 2-speaker run must not mark a 1-speaker one
const LCC_SPEAKER_MARKS = ["\u2460", "\u2461", "\u2462", "\u2463", "\u2464", "\u2465", "\u2466", "\u2467"];  // ①..⑧
function lccSpeakerPrefix(sp) {
  if (sp == null || !(sp >= 1)) return "";
  lccSpeakersSeen.add(sp);
  if (lccSpeakersSeen.size < 2) return "";   // single-speaker content stays clean
  return (LCC_SPEAKER_MARKS[(sp - 1) % LCC_SPEAKER_MARKS.length]) + " ";
}
function lccApplySpeaker(msg) {
  if (msg && msg.speaker != null && msg.ko) {
    const p = lccSpeakerPrefix(msg.speaker);
    if (p && !msg.ko.startsWith(p)) msg.ko = p + msg.ko;
  }
}

document.addEventListener("fullscreenchange", () => {
  if (!box) return;                          // re-host the caption overlay into the fullscreen subtree
  const visible = box.style.display !== "none";
  box.remove(); box = null;
  if (visible) ensureBox().style.display = "block";
});
