import os
import re
import time

from policy import _stream_visible_chars
from text_helpers import _clean


PAGE_TX_BATCH_MIN_TOKENS = max(64, int(os.environ.get("LCC_PAGE_TX_BATCH_MIN_TOKENS", "128")))
PAGE_TX_BATCH_MAX_TOKENS = max(PAGE_TX_BATCH_MIN_TOKENS, int(os.environ.get("LCC_PAGE_TX_BATCH_MAX_TOKENS", "1536")))
PAGE_BLOCK_CONTEXT = os.environ.get("LCC_PAGE_BLOCK_CONTEXT", "1") != "0"   # use the client's surrounding-block text as reference context
PAGE_BLOCK_CTX_MAX = max(80, int(os.environ.get("LCC_PAGE_BLOCK_CTX_MAX", "600")))         # per-block context cap (chars)
PAGE_BLOCK_CTX_TOTAL = max(200, int(os.environ.get("LCC_PAGE_BLOCK_CTX_TOTAL", "1200")))   # total context cap per batch (chars)


# Page DOM microbatch wire format: numbered @@n@@ markers, not JSON. A marker costs ~3 tokens vs a JSON
# object's id-echo + punctuation, and — unlike a JSON array, which only parses once fully closed — markers
# let the bridge stream each segment back the instant the *next* marker appears (the content script paints
# it immediately). Re-alignable by number, so a dropped/merged line degrades to a per-segment miss (caller
# falls back to per-item) instead of corrupting the whole batch.
# Line-START anchored so marker-looking text mid-translation can't split another node, but the segment
# text may follow on the same line OR the next — models vary, and requiring "marker alone on its line"
# would silently drop the whole batch to per-item if the model ever inlines the translation. Collisions
# (a translation line literally starting with @@n@@) are still caught by the strict 1..N sequence check.
_PAGE_MARKER_RE = re.compile(r"(?m)^[ \t]*@@\s*(\d+)\s*@@")


def _page_marker_input(items):
    return "\n\n".join(f"@@{i + 1}@@\n{str(it['text'])}" for i, it in enumerate(items))


def _page_batch_max_tokens(items):
    total_chars = sum(len(str(it.get("text", ""))) for it in (items or []) if isinstance(it, dict))
    n = sum(1 for it in (items or []) if isinstance(it, dict))
    estimate = 96 + n * 24 + int(total_chars * 0.85)
    return max(PAGE_TX_BATCH_MIN_TOKENS, min(PAGE_TX_BATCH_MAX_TOKENS, estimate))


def _page_marker_matches(text: str):
    """Return line-anchored @@n@@ marker matches from a model response. Markers are intentionally
    accepted only when they occupy their own line; marker-looking text inside a translation must not
    split or remap another DOM node."""
    return list(_PAGE_MARKER_RE.finditer(text or ""))


def _page_marker_sequence_ok(marks, n_items: int, *, complete: bool):
    """For DOM replacement, a marker collision is worse than a miss. Require the model's markers to be
    the strict 1..N sequence before trusting parsed output; streaming accepts only a valid prefix."""
    if not marks:
        return False
    idxs = [int(m.group(1)) for m in marks]
    if complete and len(idxs) != n_items:
        return False
    if len(idxs) > n_items:
        return False
    return all(idx == pos + 1 for pos, idx in enumerate(idxs))


def _page_marker_map(text: str, items=None):
    """1-based segment index -> raw segment text, parsed from a marker-formatted model response.
    The complete parser rejects missing/duplicate/out-of-order/extra markers so model errors fall
    back per item instead of corrupting cross-node DOM application."""
    raw = text or ""
    marks = _page_marker_matches(raw)
    if items is not None and not _page_marker_sequence_ok(marks, len(items), complete=True):
        raise ValueError("page batch response markers are missing, duplicated, out of order, or extra")
    out = {}
    for j, m in enumerate(marks):
        idx = int(m.group(1))
        end = marks[j + 1].start() if j + 1 < len(marks) else len(raw)
        out[idx] = raw[m.end():end]
    return out


PAGE_TX_PARTIAL_SOURCE_MAX_CHARS = max(40, int(os.environ.get("LCC_PAGE_TX_PARTIAL_SOURCE_MAX_CHARS", "420")))
PAGE_TX_PARTIAL_MIN_DELTA_CHARS = max(1, int(os.environ.get("LCC_PAGE_TX_PARTIAL_MIN_DELTA_CHARS", "2")))
PAGE_TX_PARTIAL_MIN_INTERVAL_S = max(0.02, float(os.environ.get("LCC_PAGE_TX_PARTIAL_MIN_INTERVAL_MS", "70")) / 1000.0)


def _page_strip_incomplete_marker_tail(segment: str) -> str:
    """During token streaming the model may have started the next marker (e.g. a bare ``\\n@@2``) before it
    completed a line-anchored ``@@2@@``. Don't let that half-marker flicker into the speculative DOM text."""
    return re.sub(r"(?:\r?\n)[ \t]*@{1,2}[ \t]*(?:\d{0,5})[ \t]*(?:@{0,2})[ \t]*$", "", segment or "")


def _page_partial_should_emit(text: str, last: str, now=None, last_t: float = 0.0) -> bool:
    text = _clean(text)
    last = last or ""
    if not text or text == last:
        return False
    visible = _stream_visible_chars(text)
    if visible <= 0:
        return False
    if not last:
        return True
    delta = visible - _stream_visible_chars(last)
    if delta >= PAGE_TX_PARTIAL_MIN_DELTA_CHARS:
        return True
    if now is not None and delta > 0 and (now - float(last_t or 0.0)) >= PAGE_TX_PARTIAL_MIN_INTERVAL_S:
        return True
    return False


def _emit_page_markers(text: str, items, emitted: set, on_segment, on_partial=None, partial_state=None):
    """Stream helper for DOM page batches.

    Final path: emit each COMPLETE segment only while the generated marker stream is a strict
    ``@@1@@, @@2@@, ...`` prefix — a segment is complete once its NEXT marker appears.

    Partial path: when on_partial is given, also emit the still-growing CURRENT segment. These are
    speculative UI only; the final parser stays the source of truth and may still reject the batch."""
    raw = text or ""
    marks = _page_marker_matches(raw)
    if not _page_marker_sequence_ok(marks, len(items), complete=False):
        return
    for j in range(len(marks) - 1):
        idx = j + 1
        if idx in emitted:
            continue
        seg = _clean(raw[marks[j].end():marks[j + 1].start()])
        if not seg:
            continue
        emitted.add(idx)
        if partial_state is not None:
            partial_state.pop(idx, None)
        it = items[idx - 1]
        on_segment(str(it["id"]), str(it["text"]), seg)
    if on_partial is None or not marks:
        return
    idx = len(marks)
    if idx < 1 or idx > len(items) or idx in emitted:
        return
    it = items[idx - 1]
    if len(str(it.get("text", ""))) > PAGE_TX_PARTIAL_SOURCE_MAX_CHARS:
        return
    seg = _clean(_page_strip_incomplete_marker_tail(raw[marks[-1].end():]))
    partial_state = partial_state if partial_state is not None else {}
    st = partial_state.setdefault(idx, {"last": "", "t": 0.0})
    now = time.perf_counter()
    if not _page_partial_should_emit(seg, st.get("last", ""), now, st.get("t", 0.0)):
        return
    st["last"] = seg
    st["t"] = now
    on_partial(str(it["id"]), str(it["text"]), seg)


def _parse_page_batch_result(text: str, items):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json|text)?\s*", "", raw, flags=re.I).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    seg = _page_marker_map(raw, items)
    out, missing = {}, []
    for i, it in enumerate(items):
        t = seg.get(i + 1)
        cleaned = _clean(t) if t is not None else ""
        if not cleaned:
            missing.append(str(it["id"]))
            continue
        out[str(it["id"])] = cleaned
    if missing:
        raise ValueError(f"page batch response missing/empty segments: {', '.join(missing[:4])}")
    return out


def _page_block_context_preamble(items):
    """Marker-free reference context: the distinct surrounding-block texts of the batch's fragments. The model
    uses it for terminology/pronoun/flow when translating segments that were split out of a larger block; the
    @@n@@ parser ignores these lines, so it can't corrupt output. Lives in the user turn (not the system
    prefix) so the page KV prefix stays reusable. Deduped + capped."""
    if not PAGE_BLOCK_CONTEXT:
        return ""
    seen, ctxs, total = set(), [], 0
    for it in items:
        ctx = _clean(str(it.get("ctx", "")))
        if not ctx or ctx == _clean(str(it.get("text", ""))):     # ctx == the segment itself adds nothing
            continue
        ctx = re.sub(r"@@+", "", ctx).strip()                     # never let marker-looking text into context
        key = ctx[:120]
        if not ctx or key in seen:
            continue
        seen.add(key)
        ctxs.append(ctx[:PAGE_BLOCK_CTX_MAX])
        total += len(ctxs[-1])
        if len(ctxs) >= 3 or total >= PAGE_BLOCK_CTX_TOTAL:
            break
    if not ctxs:
        return ""
    return ("[surrounding page text — reference only, DO NOT translate or output these lines]\n"
            + "\n".join(ctxs)
            + "\n[now translate ONLY the @@n@@ segments below]\n\n")
