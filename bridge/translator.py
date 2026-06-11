import os

import model_runtime
from text_helpers import _clean, _lcp_words
from page_markers import _emit_page_markers, _page_batch_max_tokens, _parse_page_batch_result
from prompts import _translate_messages, _translate_page_batch_messages, _ask_messages


_TX_KVREUSE = os.environ.get("LCC_TX_KVREUSE", "1") != "0"   # reuse the translator static-prefix KV across calls
_tx_cache = None            # persistent prompt cache for translate_once (single model_runtime._mlx_pool worker -> no race)
_tx_cache_ids = []          # token ids currently resident in _tx_cache
_PAGE_TX_KVREUSE = os.environ.get("LCC_PAGE_TX_KVREUSE", "1") != "0"   # separate page-DOM prefix KV; never shares caption cache
_page_tx_cache = None       # persistent prompt cache for translate_page_batch_once (same single model_runtime._mlx_pool worker)
_page_tx_cache_ids = []     # token ids currently resident in _page_tx_cache
_TX_KV_MAX = int(os.environ.get("LCC_TX_KV_MAX_TOKENS", "4096"))   # cap reuse to a bounded prompt window
_TX_KV_WINDOW = None        # min RotatingKVCache sliding window (Gemma 4); reuse must stay inside it (lazy)
_TX_GEN_MAX = max(1, int(os.environ.get("LCC_TX_GEN_MAX_TOKENS", "64")))   # caption translation cap; ask/summary uses its own chat cap
_TX_WINDOW_MARGIN = max(0, int(os.environ.get("LCC_TX_WINDOW_MARGIN", "8")))   # keep reuse a few tokens clear of the window edge

def _reset_tx_cache():
    global _tx_cache, _tx_cache_ids
    _tx_cache, _tx_cache_ids = None, []

def _reset_page_tx_cache():
    global _page_tx_cache, _page_tx_cache_ids
    _page_tx_cache, _page_tx_cache_ids = None, []

def _tx_cache_offset(cache):
    """Logical token length the prompt cache is at (all layers agree), or None if unreadable. For sliding
    layers (RotatingKVCache) this is the logical position, NOT resident size; trimmability is separate
    (offset < max_size) and must be checked via model_runtime.can_trim_prompt_cache."""
    try:
        offs = [int(c.offset) for c in cache if hasattr(c, "offset")]
        if offs and len(offs) == len(cache) and min(offs) == max(offs):
            return offs[0]
    except Exception:
        pass
    return None

_TX_FULL_CACHE = {"KVCache", "QuantizedKVCache", "ConcatenateKVCache"}   # full-attention: reuse-safe unbounded

def _iter_cache_objs(c):
    if isinstance(c, (list, tuple)):
        for x in c:
            yield from _iter_cache_objs(x)
        return
    inner = getattr(c, "caches", None)
    if isinstance(inner, (list, tuple)):
        for x in inner:
            yield from _iter_cache_objs(x)
        return
    yield c

def _learn_tx_window(cache):
    # smallest sliding window (RotatingKVCache.max_size); _TX_KV_MAX if all full-attention;
    # 0 (= disable reuse) if ANY layer is an unrecognized cache type -> fail safe on future/hybrid models.
    windows, unknown = [], []
    for c in _iter_cache_objs(cache):
        name = type(c).__name__
        if name == "RotatingKVCache":
            m = getattr(c, "max_size", None)
            windows.append(int(m)) if m else unknown.append(name)
        elif name in _TX_FULL_CACHE:
            continue
        else:
            unknown.append(name)
    if unknown:
        print(f"[txkv] reuse disabled - unrecognized cache types {sorted(set(unknown))}", flush=True)
        return 0
    return min(windows) if windows else _TX_KV_MAX

def _ensure_ids(prompt):
    """apply_chat_template normally returns list[int]; coerce str/array so _lcp_words + slicing stay sane."""
    if isinstance(prompt, str):
        return list(map(int, model_runtime.lm_tok.encode(prompt)))
    if hasattr(prompt, "tolist"):
        prompt = prompt.tolist()
    return [int(x) for x in prompt]

def _trim_cache_or_reset(cache, reset_fn, n, expected_after):
    """Trim exactly n tokens and VERIFY (count + post-offset). Reset the persistent cache and return False
    on any failure — Gemma 4 sliding layers (RotatingKVCache) go non-trimmable once offset >= sliding_window
    and model_runtime.trim_prompt_cache then silently returns 0, which would desync _tx_cache_ids from the real cache."""
    if n <= 0:
        return True
    if cache is None or not model_runtime.can_trim_prompt_cache(cache):
        reset_fn(); return False
    try:
        got = model_runtime.trim_prompt_cache(cache, n)
    except Exception:
        reset_fn(); return False
    if got != n or _tx_cache_offset(cache) != expected_after:
        reset_fn(); return False
    return True

def _tx_trim_or_reset(n, expected_after):
    return _trim_cache_or_reset(_tx_cache, _reset_tx_cache, n, expected_after)

def _page_tx_trim_or_reset(n, expected_after):
    return _trim_cache_or_reset(_page_tx_cache, _reset_page_tx_cache, n, expected_after)

def _usable_tx_partial(s):
    # a streamed KO partial good enough to commit as a degraded caption (vs falling back to the source line)
    s = (s or "").strip()
    if len(s) < 8:
        return False
    if s.endswith(("(", "[", "{", ",", "…", "、", "，", "·")):
        return False
    return True

def _vlm_generate_text(msgs, gen_max, on_update=None, model=None, proc=None):
    """mlx_vlm translation path for Gemma-4 nano tiers (mid/lite). Text-only chat -> mlx_vlm.generate. No
    KV-reuse / token streaming (those are mlx_lm-specific) -> a single final update. Defaults to the main
    translator globals; an aux runtime passes its own (model, proc). Same _translate_messages prompt as the
    mlx_lm path, so output is consistent."""
    model_runtime.mx.set_default_device(model_runtime.mx.gpu)
    model = model_runtime.lm_model if model is None else model
    proc = model_runtime.lm_tok if proc is None else proc
    try:
        prompt = proc.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
    except Exception:
        prompt = proc.tokenizer.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
    res = model_runtime.vlm_generate(model, proc, prompt, max_tokens=int(gen_max), verbose=False)
    text = _clean(getattr(res, "text", None) or (res if isinstance(res, str) else str(res)))
    if on_update is not None and text:
        on_update(text)
    return text


def translate_once(text: str, recent_pairs=(), target: str = "Korean", hint: str = "",
                   register: str = "casual", glossary_pairs=(), on_update=None, kv_reuse=None,
                   max_tokens=None, stream_every=None, profile: str = "caption", custom: str = "",
                   runtime=None, meta=None):
    """Stateless per-clause translation, primed for quality: a strong register-aware instruction,
    source-language-matched few-shot anchors, a pinned glossary, and the last few (source -> target)
    pairs as conversation context so terminology/tone stay consistent across the stream. Re-callable on
    a growing clause (EN->KO reverses word order, so we re-translate the whole clause). Runs on model_runtime._mlx_pool
    (single worker -> the module-level _tx_cache has no race). runtime=(model, tok, is_vlm) redirects the
    call to the AUX translator on its own pool — that path always uses a fresh per-call cache (no shared
    KV state, so it is safe off the main worker thread)."""
    global _tx_cache, _tx_cache_ids, _TX_KV_WINDOW
    msgs = _translate_messages(text, recent_pairs, target, hint, register, glossary_pairs, profile, custom)
    model, tok, is_vlm = (model_runtime.lm_model, model_runtime.lm_tok, model_runtime._LM_IS_VLM) if runtime is None else runtime
    if is_vlm:
        if meta is not None:
            meta["truncated"] = None    # vlm path has no finish reason — truncation unknowable
        return _vlm_generate_text(msgs, max(1, int(max_tokens or _TX_GEN_MAX)), on_update, model, tok)
    model_runtime.mx.set_default_device(model_runtime.mx.gpu)
    try:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
    prompt = _ensure_ids(prompt)
    gen_max = max(1, int(max_tokens or _TX_GEN_MAX))
    if runtime is None and _TX_KV_WINDOW is None:          # learn the sliding window once (fail-safe on unknown caches)
        _TX_KV_WINDOW = _learn_tx_window(_tx_cache if _tx_cache is not None else model_runtime.make_prompt_cache(model_runtime.lm_model))
    # Reuse the KV of the static prefix (system + few-shot + recent_pairs ~= 95% of the prompt, identical
    # across calls): trim the persistent cache to the longest prefix it still shares with this prompt, then
    # prefill only the divergent tail (~850ms -> ~280ms TTFT). The cache is STATEFUL: the invariant
    # _tx_cache_ids == cache.offset must hold on EVERY path, so every trim is verified and ANY failure
    # (incl. a non-trimmable RotatingKVCache once offset >= sliding_window) resets to a fresh cache; after
    # each call we trim the generated suffix back to prompt-only. Single model_runtime._mlx_pool worker. Off: LCC_TX_KVREUSE=0.
    # reuse only while the whole call (prompt + generation + margin) stays INSIDE the sliding window: past it
    # the rotating layers go non-trimmable AND a trim+append prefill no longer matches a fresh prefill.
    use_reuse = _TX_KVREUSE if kv_reuse is None else kv_reuse
    reuse = (runtime is None) and use_reuse and (len(prompt) + gen_max + _TX_WINDOW_MARGIN) <= min(_TX_KV_MAX, _TX_KV_WINDOW)
    if reuse:
        if _tx_cache is not None:                              # preflight: cache must still hold exactly _tx_cache_ids
            pre = _tx_cache_offset(_tx_cache)
            if pre is None or pre != len(_tx_cache_ids):
                _reset_tx_cache()
        if _tx_cache is None:
            _tx_cache, _tx_cache_ids = model_runtime.make_prompt_cache(model_runtime.lm_model), []
        common = _lcp_words(_tx_cache_ids, prompt)
        if len(_tx_cache_ids) - common > 0 and not _tx_trim_or_reset(len(_tx_cache_ids) - common, common):
            _tx_cache, _tx_cache_ids, common = model_runtime.make_prompt_cache(model_runtime.lm_model), [], 0
        feed = prompt[common:]
        if not feed:                                           # prompt already resident: rewind one token,
            if common <= 0 or not _tx_trim_or_reset(1, len(prompt) - 1):   # or rebuild if the cache can't rewind
                _tx_cache, _tx_cache_ids, common, feed = model_runtime.make_prompt_cache(model_runtime.lm_model), [], 0, prompt
            else:
                common -= 1; feed = prompt[common:]
        cache = _tx_cache
    else:
        # size the fresh cache to hold the WHOLE call: a fixed cap smaller than prompt+generation makes the
        # rotating cache evict the system prompt mid-prefill (long input_translate drafts) — silent quality collapse
        cache = model_runtime.make_prompt_cache(model, max_kv_size=max(2048, len(prompt) + gen_max + _TX_WINDOW_MARGIN))
        feed = prompt
    out, since, finish = [], 0, None
    try:
        every = max(1, int(stream_every or 4))
        for r in model_runtime.lm_stream(model, tok, feed, max_tokens=gen_max, sampler=model_runtime._sampler, prompt_cache=cache):
            out.append(r.text)
            finish = getattr(r, "finish_reason", None) or finish
            since += 1
            if on_update is not None and since >= every:
                since = 0
                p = _clean("".join(out))
                if p:
                    on_update(p)
    except Exception:
        if reuse:
            _reset_tx_cache()              # cache mutated mid-generation; tracked ids are now stale
        raise
    if meta is not None:
        # hit the token cap without a natural stop -> output is cut mid-sentence; callers must not
        # promote it to a final caption or cache it (fallback: count tokens when finish_reason is absent)
        meta["truncated"] = (finish == "length") if finish is not None else (len(out) >= gen_max)
    if reuse and _tx_cache is not None:
        actual = _tx_cache_offset(_tx_cache)
        if actual is None:
            _reset_tx_cache()              # can't verify -> don't keep a cache we can't trust
        elif actual < len(prompt):     # output context itself is suspect -> recompute once on a fresh cache
            _reset_tx_cache()
            print(f"[txkv] invariant breach: offset {actual} < prompt {len(prompt)} -> fresh retry", flush=True)
            return translate_once(
                text, recent_pairs, target, hint, register, glossary_pairs, None, kv_reuse=False,
                max_tokens=max_tokens, stream_every=stream_every, profile=profile, custom=custom, meta=meta,
            )
        elif actual > len(prompt):
            if _tx_trim_or_reset(actual - len(prompt), len(prompt)):   # drop generated suffix -> prompt-only
                _tx_cache_ids = list(prompt)
            # else: helper already reset the cache; the returned output is still valid
        else:
            _tx_cache_ids = list(prompt)
    return _clean("".join(out))


def translate_page_batch_once(items, recent_pairs=(), target: str = "Korean", hint: str = "",
                              register: str = "casual", glossary_pairs=(), max_tokens=None, kv_reuse=None,
                              on_segment=None, on_partial=None, custom: str = "", runtime=None):
    """Translate a DOM batch in one model call. Uses a page-only prefix KV cache so page DOM work never
    disturbs the live-caption translator cache. Output is @@n@@-marked; when on_segment is given each segment
    is streamed back the instant its following marker appears, and on_partial streams the still-growing
    current segment as speculative UI. Returns {id: target}; raises on a missing segment so callers can fall
    back to per-item translation. runtime=(model, tok, is_vlm) redirects to the AUX translator (fresh
    per-call cache, no shared KV state — safe off the main worker thread)."""
    global _page_tx_cache, _page_tx_cache_ids, _TX_KV_WINDOW
    clean_items = [
        {"id": str(it.get("id", ""))[:80], "text": str(it.get("text", "")).strip(),
         "ctx": str(it.get("ctx", "")).strip()}
        for it in (items or [])
        if isinstance(it, dict) and str(it.get("id", "")).strip() and str(it.get("text", "")).strip()
    ]
    if not clean_items:
        return {}
    msgs = _translate_page_batch_messages(clean_items, recent_pairs, target, hint, register, glossary_pairs, custom)
    gen_max = max(1, int(max_tokens or _page_batch_max_tokens(clean_items)))
    emitted = set()
    partial_state = {}

    def _finish(full_text):
        result = _parse_page_batch_result(full_text, clean_items)   # raises on a missing segment
        if on_segment is not None:
            for i, it in enumerate(clean_items):                    # deliver the trailing/last segment too
                if (i + 1) not in emitted and str(it["id"]) in result:
                    emitted.add(i + 1)
                    on_segment(str(it["id"]), str(it["text"]), result[str(it["id"])])
        return result

    model, tok, is_vlm = (model_runtime.lm_model, model_runtime.lm_tok, model_runtime._LM_IS_VLM) if runtime is None else runtime
    if is_vlm:
        raw = _vlm_generate_text(msgs, gen_max, None, model, tok)
        return _finish(raw)
    model_runtime.mx.set_default_device(model_runtime.mx.gpu)
    try:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
    prompt = _ensure_ids(prompt)
    if runtime is None and _TX_KV_WINDOW is None:
        _TX_KV_WINDOW = _learn_tx_window(_page_tx_cache if _page_tx_cache is not None else model_runtime.make_prompt_cache(model_runtime.lm_model))
    # Streamed DOM segments are applied immediately by the content script; if a persistent KV invariant
    # later forces a fresh retry, there is no safe way to "unsend" already-painted replacements. Use a
    # fresh per-call cache for streaming unless the caller explicitly opts back into reuse.
    use_reuse = (False if on_segment is not None else _PAGE_TX_KVREUSE) if kv_reuse is None else kv_reuse
    reuse = (runtime is None) and use_reuse and (len(prompt) + gen_max + _TX_WINDOW_MARGIN) <= min(_TX_KV_MAX, _TX_KV_WINDOW)
    if reuse:
        if _page_tx_cache is not None:
            pre = _tx_cache_offset(_page_tx_cache)
            if pre is None or pre != len(_page_tx_cache_ids):
                _reset_page_tx_cache()
        if _page_tx_cache is None:
            _page_tx_cache, _page_tx_cache_ids = model_runtime.make_prompt_cache(model_runtime.lm_model), []
        common = _lcp_words(_page_tx_cache_ids, prompt)
        if len(_page_tx_cache_ids) - common > 0 and not _page_tx_trim_or_reset(len(_page_tx_cache_ids) - common, common):
            _page_tx_cache, _page_tx_cache_ids, common = model_runtime.make_prompt_cache(model_runtime.lm_model), [], 0
        feed = prompt[common:]
        if not feed:
            if common <= 0 or not _page_tx_trim_or_reset(1, len(prompt) - 1):
                _page_tx_cache, _page_tx_cache_ids, common, feed = model_runtime.make_prompt_cache(model_runtime.lm_model), [], 0, prompt
            else:
                common -= 1; feed = prompt[common:]
        cache = _page_tx_cache
    else:
        cache = model_runtime.make_prompt_cache(model, max_kv_size=max(2048, len(prompt) + gen_max + _TX_WINDOW_MARGIN))
        feed = prompt
    out = []
    since = 0
    try:
        for r in model_runtime.lm_stream(model, tok, feed, max_tokens=gen_max, sampler=model_runtime._sampler, prompt_cache=cache):
            out.append(r.text)
            if on_segment is not None:
                since += 1
                if since >= 6:                                  # stream completed segments without per-token regex cost
                    since = 0
                    _emit_page_markers("".join(out), clean_items, emitted, on_segment, on_partial, partial_state)
    except Exception:
        if reuse:
            _reset_page_tx_cache()
        raise
    if on_segment is not None:
        _emit_page_markers("".join(out), clean_items, emitted, on_segment, on_partial, partial_state)
    if reuse and _page_tx_cache is not None:
        actual = _tx_cache_offset(_page_tx_cache)
        if actual is None:
            _reset_page_tx_cache()
        elif actual < len(prompt):
            _reset_page_tx_cache()
            print(f"[pagekv] invariant breach: offset {actual} < prompt {len(prompt)} -> fresh retry", flush=True)
            return translate_page_batch_once(
                clean_items, recent_pairs, target, hint, register, glossary_pairs,
                max_tokens=max_tokens, kv_reuse=False, on_segment=on_segment, on_partial=on_partial, custom=custom,
            )
        elif actual > len(prompt):
            if _page_tx_trim_or_reset(actual - len(prompt), len(prompt)):
                _page_tx_cache_ids = list(prompt)
        else:
            _page_tx_cache_ids = list(prompt)
    return _finish("".join(out))



def run_ask(mode: str, transcript_text: str, question: str = "", target: str = "Korean", on_partial=None):
    """On-demand summary / Q&A over the running transcript (already-resident translator, fresh KV cache)."""
    msgs, max_toks = _ask_messages(mode, transcript_text, question, target)
    if model_runtime._LM_IS_VLM:
        return _vlm_generate_text(msgs, max_toks, on_partial)
    model_runtime.mx.set_default_device(model_runtime.mx.gpu)
    try:
        prompt = model_runtime.lm_tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = model_runtime.lm_tok.apply_chat_template(msgs, add_generation_prompt=True)
    cache = model_runtime.make_prompt_cache(model_runtime.lm_model, max_kv_size=8192)   # fresh window; don't pollute the translation KV cache
    out, since = [], 0
    for r in model_runtime.lm_stream(model_runtime.lm_model, model_runtime.lm_tok, prompt, max_tokens=max_toks, sampler=model_runtime._sampler, prompt_cache=cache):
        out.append(r.text); since += 1
        if on_partial is not None and since >= 4:
            since = 0; on_partial(_clean("".join(out)))
    return _clean("".join(out))

