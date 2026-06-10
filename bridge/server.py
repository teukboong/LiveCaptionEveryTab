"""Live-caption bridge v2 (sentence-accumulation): decouple live source from sentence-level KO.

  client -> server : text {"type":"hello","token":..} authenticates the local extension
                     binary PCM16 mono 16kHz frames; text {"type":"eos"} flushes everything
  server -> client : {"type":"source","text":..,"unit_id":..,"rev":..}             # live ASR source
                     {"type":"caption_partial","source":..,"ko":..,"unit_id":..}  # debounced preview
                     {"type":"caption","source":..,"ko":..,"unit_id":..}          # committed final

Why: EN->KO reverses word order, so a fragment can't be translated correctly — you need the whole
clause. So we transcribe each VAD chunk immediately (live source line) but only translate once a
sentence completes (terminal punctuation, OR a long pause = clause boundary, OR a length cap).

Pipeline per VAD chunk: granite/qwen3 (mlx-audio) clean-sentence transcript ([no speech] gate => no hallucination).
Per completed sentence: Gemma-4-26B-A4B MoE -> natural Korean (Korean source skips translation).
mlx on a single dedicated worker thread (model_runtime._mlx_pool) — inline-in-loop hangs; set_default_device(gpu)
restores the stream. (asyncio.to_thread's default pool could hop threads between calls.)
"""
# S3 module map: server keeps websocket orchestration, Unit, backend seam rebinding, warm/startup adapters,
# repeat-cache adapters, and translate_page_long_once; text_helpers owns pure text utilities; policy owns
# scheduler/latency/number-guard policy; prompts owns prompt/message builders; page_markers owns DOM marker
# parsing/streaming; term_memory owns term mining/merge helpers; model_runtime owns lazy model/runtime state;
# asr owns MLX ASR and glossary repair; translator owns KV caches plus translate/ask leaf implementations.
import asyncio, base64, json, collections, difflib, functools, os, re, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import numpy as np
import websockets
from silero_vad import VADIterator
import policy
import model_runtime
import translator
import asr
import text_helpers
import page_markers
import prompts
import term_memory
from policy import (
    SR, WINDOW_BYTES, WINDOW_MS, SEG_SILENCE_MS, SENT_SILENCE_MS, SPEECH_PAD_MS,
    PREROLL_WINDOWS, HARD_MAX_SEC, MIN_SEC, SOFT_OVERLAP_MS, LA_ON, LA_STEP_WINDOWS,
    TWO_PASS_MAX_SEC, PENDING_CAP, AGG_PENDING_CAP, BAL_PENDING_CAP,
    PREVIEW_PROMOTE_SIMILARITY, _lat_profile, _evs_step, _lat_effective_sent_silence_ms,
    _two_pass_eligible, _guard_numbers, _source_risk, decide_commit, _preview_is_stale,
    _stream_partial_should_emit,
)
from text_helpers import (
    _lcp_words, _coalesce_batch, _next_sentence_cut, _norm_words,
    _short_suffix_duplicate, _append_text_dedupe, _dedupe_commit_overlap,
    _repeat_cache_eligible, _repeat_key, _clean, _src_lang, _gr_norm, _chunk_text,
)
from page_markers import (
    PAGE_BLOCK_CTX_MAX, _page_batch_max_tokens, PAGE_TX_PARTIAL_SOURCE_MAX_CHARS,
    _page_partial_should_emit,
)
from prompts import (
    _normalize_target_lang, _translation_context_signature, _REGISTERS, _parse_glossary,
)
from term_memory import (
    TERM_MEMORY_ON, TERM_MEMORY_MIN_COUNT, TERM_MEMORY_UPDATE_EVERY,
    _update_term_memory, _merge_auto_glossary,
)

_TEXT_HELPERS_DYNAMIC_EXPORTS = {
    "MIN_SENT_CHARS", "_append_text_dedupe", "_chunk_text", "_clean", "_dedupe_commit_overlap",
    "_gr_norm", "_next_sentence_cut", "_repeat_cache_eligible", "_repeat_key",
    "_short_suffix_duplicate", "_split_sentences", "_src_lang", "_weak_tail",
}

_POLICY_DYNAMIC_EXPORTS = {
    "EVS_ON", "EVS_ENTER_MS", "EVS_EXIT_MS", "EVS_CAP_DROP", "EVS_AGE_DROP", "NUMGUARD_ON",
    "AGG_SOFT_MAX_SEC", "BAL_SOFT_MAX_SEC", "AGG_SENT_SILENCE_MS", "BAL_SENT_SILENCE_MS",
    "AGG_PENDING_MAX_AGE_MS", "AGG_PREVIEW_DEBOUNCE_MS", "BAL_PREVIEW_DEBOUNCE_MS",
    "SPEC_PREVIEW_MIN_CHARS", "SPEC_PREVIEW_MIN_DELTA", "SPEC_PREVIEW_COOLDOWN_MS",
    "TX_FINAL_STREAM_EVERY", "TX_FINAL_STREAM_MIN_CHARS", "TX_FINAL_STREAM_MIN_WORDS",
    "TX_FINAL_STREAM_DELTA_CHARS", "SR", "WINDOW_MS", "SOFT_MAX_SEC", "TWO_PASS_MIN_SEC",
    "PENDING_CAP", "PENDING_MAX_AGE_MS", "PREVIEW_DEBOUNCE_MS", "AGG_PENDING_CAP",
    "BAL_PENDING_CAP", "BAL_PENDING_MAX_AGE_MS", "PREVIEW_MIN_CHARS", "PREVIEW_MIN_DELTA",
    "_commit_decision", "_two_pass_eligible", "_lat_profile", "_lat_tx_stream_every_for",
    "_lat_preview_debounce_ms", "_lat_pending_cap", "_lat_pending_max_age_ms",
    "_lat_effective_sent_silence_ms", "_evs_step", "_sig_numbers", "_ko_number_forms",
    "_missing_numbers", "_guard_numbers", "_source_risk", "decide_commit", "InterpretDecision",
    "_preview_is_stale", "_stream_visible_chars", "_stream_partial_substantial",
    "_stream_partial_should_emit",
}

_PAGE_MARKERS_DYNAMIC_EXPORTS = {
    "_emit_page_markers", "_page_batch_max_tokens", "_parse_page_batch_result",
}

_PROMPTS_DYNAMIC_EXPORTS = {
    "_TARGET_LANGS", "_REGISTERS", "_ask_messages", "_fewshot", "_normalize_target_lang",
    "_page_tx_system", "_translation_context_signature", "_translate_messages",
    "_translate_page_batch_messages", "_tx_system",
}

_TERM_MEMORY_DYNAMIC_EXPORTS = {
    "TERM_MEMORY_STATS_MAX", "_merge_auto_glossary", "_mine_terms", "_update_term_memory",
}

_MODEL_RUNTIME_DYNAMIC_EXPORTS = {
    "BACKEND", "LM_MODEL", "_LM_RESOLVED", "_LM_IS_VLM", "lm_model", "lm_tok", "_sampler",
    "silero", "aux_lm_model", "aux_lm_tok", "_AUX_LM_IS_VLM", "ASR_ENGINE", "mlxa_model",
    "mlxa_loaded_engine", "parakeet_asr", "whisper_loaded_repo", "mx", "lm_load", "lm_stream",
    "make_sampler", "make_prompt_cache", "trim_prompt_cache", "can_trim_prompt_cache", "vlm_load",
    "vlm_generate", "MLX_IMPORT_ERROR", "lm_models", "asr_models", "_ASR_ENGINES",
    "_is_sherpa_engine", "_is_mlxa_engine", "_is_whisper_engine", "_normalize_asr_engine",
    "_normalize_backend", "_normalize_latency_mode", "_clamp_int", "_clamp_float", "_config_bool",
    "_free_mem_gb_mlx", "_free_mem_gb_cuda", "_system_available_gb", "_mlx_device_info",
    "_mlx_active_memory", "_auto_lm_model", "_finalize_model_config", "_aux_lm_choice",
    "_lm_select_value", "_resolve_lm_model", "AUX_LM", "AUX_LM_HEADROOM_GB", "MLXA_REPOS",
    "LM_MODELS", "ASR_MODELS", "GRANITE_ASR_PROMPT", "WHISPER_REPO", "PARAKEET_MODEL_DIR",
    "PARAKEET_THREADS", "PARAKEET_PROVIDER", "aux_lm_ready", "_aux_runtime", "_diarize",
    "_require_mlx", "_load_lm_weights", "_mlx_pool", "_sherpa_pool",
    "_asr_pool", "_ASR_DEVICE_LOCK", "_aux_lm_pool", "_AUX_LM_DEVICE_LOCK", "_MLX_DEVICE_LOCK",
}

_ASR_DYNAMIC_EXPORTS = {
    "GLOSSARY_REPAIR_ON", "ASR_MAX_TOKENS", "_repair_glossary_terms",
}

_TRANSLATOR_DYNAMIC_EXPORTS = {
    "_TX_KVREUSE", "_tx_cache", "_tx_cache_ids", "_PAGE_TX_KVREUSE", "_page_tx_cache",
    "_page_tx_cache_ids", "_TX_KV_MAX", "_TX_KV_WINDOW", "_TX_GEN_MAX", "_TX_WINDOW_MARGIN",
    "_reset_tx_cache", "_reset_page_tx_cache", "_tx_cache_offset", "_iter_cache_objs",
    "_learn_tx_window", "_ensure_ids", "_trim_cache_or_reset", "_tx_trim_or_reset",
    "_page_tx_trim_or_reset", "_usable_tx_partial", "_vlm_generate_text", "translate_once",
    "translate_page_batch_once", "run_ask",
}


def __getattr__(name):
    if name in _TEXT_HELPERS_DYNAMIC_EXPORTS:
        return getattr(text_helpers, name)
    if name in _POLICY_DYNAMIC_EXPORTS:
        return getattr(policy, name)
    if name in _PAGE_MARKERS_DYNAMIC_EXPORTS:
        return getattr(page_markers, name)
    if name in _PROMPTS_DYNAMIC_EXPORTS:
        return getattr(prompts, name)
    if name in _TERM_MEMORY_DYNAMIC_EXPORTS:
        return getattr(term_memory, name)
    if name in _MODEL_RUNTIME_DYNAMIC_EXPORTS:
        return getattr(model_runtime, name)
    if name in _ASR_DYNAMIC_EXPORTS:
        return getattr(asr, name)
    if name in _TRANSLATOR_DYNAMIC_EXPORTS:
        return getattr(translator, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

DOM_TX_MAX_ITEMS = int(os.environ.get("LCC_DOM_TX_MAX_ITEMS", "8"))
DOM_TX_MAX_CHARS = int(os.environ.get("LCC_DOM_TX_MAX_CHARS", "32000"))       # per-item sanity bound; long paragraphs are sentence-chunked (translate_page_long_once) + streamed, not truncated in practice
DOM_TX_MAX_TOTAL_CHARS = int(os.environ.get("LCC_DOM_TX_MAX_TOTAL_CHARS", "36000"))   # whole-batch ceiling (one long item + short ones)
PAGE_LONG_CHARS = max(200, int(os.environ.get("LCC_PAGE_LONG_CHARS", "600")))   # items longer than this take the sentence-chunked, context-preserving path


def _dom_translate_items(payload, *, max_items=DOM_TX_MAX_ITEMS, max_chars=DOM_TX_MAX_CHARS,
                         max_total_chars=DOM_TX_MAX_TOTAL_CHARS):
    """Normalize untrusted page-translation items from the extension before they reach the model. Each item
    may carry an optional `ctx` (the surrounding semantic block's text) for reference-only context."""
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []
    out, total = [], 0
    for raw in raw_items[:max(1, max_items)]:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id", ""))[:80]
        text = str(raw.get("text", "")).strip()
        if not item_id or not text:
            continue
        text = text[:max_chars]
        if total + len(text) > max_total_chars and out:
            break
        total += len(text)
        item = {"id": item_id, "text": text}
        ctx = str(raw.get("ctx", "")).strip()
        if ctx:
            item["ctx"] = ctx[:PAGE_BLOCK_CTX_MAX]
        out.append(item)
    return out



HOST = os.environ.get("LCC_HOST", "127.0.0.1")   # WSL2→Windows localhost forwarding works on 127.0.0.1; set 0.0.0.0 only if a remote client must reach it
PORT = int(os.environ.get("LCC_PORT", "8765"))
_DEFAULT_WS_TOKEN = "lcc-local-extension-v1"      # also hardcoded in extension/protocol.js — the localhost guard, NOT a real secret
WS_TOKEN = os.environ.get("LCC_WS_TOKEN", _DEFAULT_WS_TOKEN)
# Only THIS project's extension may drive the bridge. The repo manifest pins this id via its embedded "key";
# override (or add dev/custom builds) with LCC_EXTENSION_ID / LCC_ALLOWED_WS_ORIGINS.
_EXTENSION_ID = os.environ.get("LCC_EXTENSION_ID", "ddcflpihicaobncgpmadoipiofpllgnl").strip()
MAX_WS_MSG_BYTES = int(os.environ.get("LCC_MAX_WS_MSG_BYTES", str(256 * 1024)))
MAX_AUDIO_FRAME_BYTES = int(os.environ.get("LCC_MAX_AUDIO_FRAME_BYTES", str(64 * 1024)))
WORK_Q_MAX = max(8, int(os.environ.get("LCC_WORK_Q_MAX", "96")))
TRANS_Q_MAX = max(8, int(os.environ.get("LCC_TRANS_Q_MAX", "64")))

TRANSLATION_CACHE_MAX = 128
VAD_THRESH = {0: 0.3, 1: 0.4, 2: 0.5, 3: 0.65}   # vadLevel -> Silero speech-probability threshold

# Models load lazily via load_models() (called from __main__) so the pure prompt-building helpers
# (_tx_system / _fewshot / translate_once) can be imported by benches/tests without pulling ~50GB of
# weights. The bridge loads everything; a translation-only bench can skip ASR/VAD.
transcribe_pcm = asr.transcribe_pcm
_ensure_asr_loaded = asr._ensure_asr_loaded
_repair_glossary_terms = asr._repair_glossary_terms
translate_once = translator.translate_once
translate_page_batch_once = translator.translate_page_batch_once
run_ask = translator.run_ask
_reset_tx_cache = translator._reset_tx_cache
_reset_page_tx_cache = translator._reset_page_tx_cache
_tx_cache_offset = translator._tx_cache_offset
_usable_tx_partial = translator._usable_tx_partial
_vlm_generate_text = translator._vlm_generate_text


def load_models(asr=True, lm=True, vad=True):
    if model_runtime.BACKEND == "fake":
        if vad and model_runtime.silero is None:
            import backend_fake
            model_runtime.silero = backend_fake.FAKE_SILERO_SENTINEL
        return
    return model_runtime.load_models(asr=asr, lm=lm, vad=vad, asr_loader=_ensure_asr_loaded)


# Single active capture connection (diagnostic only): one client is the intended model. If a second
# authenticates while one is active we WARN — we do NOT close it: the extension auto-reconnects, so a forced
# close would cause a reconnect war. Correctness across any concurrent clients is held by model_runtime._MLX_DEVICE_LOCK;
# full non-degradation would need a per-connection translator cache. See docs/caption-lifecycle.md.
_active_ws = None
LATENCY_MODE_DEFAULT = model_runtime._normalize_latency_mode(os.environ.get("LCC_LATENCY_MODE"), "aggressive")
TX_RECENT_FINAL_MAX = max(0, int(os.environ.get("LCC_TX_RECENT_FINAL_MAX", "2")))
TX_RECENT_PREVIEW_MAX = max(0, int(os.environ.get("LCC_TX_RECENT_PREVIEW_MAX", "0")))
TX_PREVIEW_MAX_TOKENS = max(16, int(os.environ.get("LCC_TX_PREVIEW_MAX_TOKENS", "40")))


def _lat_tx_max_tokens_for(final: bool):
    return translator._TX_GEN_MAX if final else min(translator._TX_GEN_MAX, TX_PREVIEW_MAX_TOKENS)


def _lat_soft_max_sec(engine: str, mode: str):
    if model_runtime._is_sherpa_engine(engine):
        if mode == "aggressive":
            return policy.AGG_SOFT_MAX_SEC
        if mode == "balanced":
            return policy.BAL_SOFT_MAX_SEC
    return policy.SOFT_MAX_SEC


def _lat_sent_windows_for(mode: str, raw_ms):
    return max(1, (policy._lat_effective_sent_silence_ms(mode, raw_ms) - SEG_SILENCE_MS) // WINDOW_MS)
# ------------------------------------------------------------------------------------------------------

def warm_mlx_selected(asr=False, lm=False, asr_engine=None):
    engine = model_runtime._normalize_asr_engine(asr_engine, model_runtime.ASR_ENGINE)
    if not model_runtime._is_sherpa_engine(engine) or lm:
        model_runtime._require_mlx()
        model_runtime.mx.set_default_device(model_runtime.mx.gpu)
    if asr:
        try:
            _ensure_asr_loaded(engine)
            transcribe_pcm((np.zeros(SR, dtype=np.int16)).tobytes(), asr_engine=engine)
        except Exception as e:
            print(f"[warm] {engine} asr:", e, flush=True)
    if lm:
        try:
            translate_once("hello world")
        except Exception as e:
            print("[warm] mlx lm:", e, flush=True)
        if model_runtime.aux_lm_ready():
            try:
                translate_once("hello world", runtime=model_runtime._aux_runtime())
            except Exception as e:
                print("[warm] aux lm:", e, flush=True)

def _request_header(ws, name: str):
    headers = getattr(ws, "request_headers", None)
    if headers is None:
        req = getattr(ws, "request", None)
        headers = getattr(req, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name) or headers.get(name.lower())
    except Exception:
        return None


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True                         # CLI smoke tests usually omit Origin (still token-gated at hello).
    # Restrict to THIS extension's origin — not every chrome-extension:// origin, since any other installed
    # extension could otherwise open the localhost WS and stream the active tab's audio / read transcripts.
    if _EXTENSION_ID and origin == f"chrome-extension://{_EXTENSION_ID}":
        return True
    extra = [o.strip() for o in os.environ.get("LCC_ALLOWED_WS_ORIGINS", "").split(",") if o.strip()]
    return origin in extra


def _caption_read_ms(text: str) -> int:
    return max(1300, min(7000, len(text or "") * 75))


# --- Exact-repeat translation cache (catchphrases) -----------------------------------------------------
# Streams repeat themselves constantly ("Thanks for the sub!", greetings, stingers). The per-connection
# translation_cache keys on the rolling recent-pairs context, so an identical line a minute later almost
# always misses. Short SELF-CONTAINED lines get a second, context-free lookup keyed on normalized words —
# their rendering doesn't meaningfully depend on conversation context, so reusing it is safe and instant.
# Longer lines stay context-keyed only. Cleared with the other caches on any translation-context change.
TX_REPEAT_CACHE_ON = os.environ.get("LCC_TX_REPEAT_CACHE", "1") == "1"
TX_REPEAT_CACHE_MAX = 256




# A long paragraph (e.g. an arXiv intro) is one big text node. Translating it whole risks token/window
# truncation; translating each sentence in isolation loses pronouns, terminology, and flow. So we sentence-
# chunk it and translate the chunks SEQUENTIALLY, feeding each chunk the paragraph's already-translated
# chunks as recent_pairs — the model keeps terms/discourse consistent within the paragraph — then join.
def translate_page_long_once(text, recent_pairs=(), target: str = "Korean", hint: str = "",
                             register: str = "casual", glossary_pairs=(), on_progress=None, custom: str = ""):
    """Translate a long DOM paragraph by sentence-chunking and translating chunks sequentially, each
    conditioned on the paragraph's already-translated chunks (running context). Every model call stays
    small (clean translation, no truncation); the joined result is one string for the node. on_progress, if
    given, is called with the cumulative translation after each chunk so the caller can stream it into the
    node incrementally (and re-arm its timeout) instead of waiting for the whole paragraph."""
    text = str(text or "")
    chunks = _chunk_text(text)

    def _tx(chunk, ctx):
        return _clean(translate_once(chunk, list(ctx), target=target, hint=hint, register=register,
                                     glossary_pairs=glossary_pairs, kv_reuse=False, profile="page",
                                     max_tokens=_page_batch_max_tokens([{"text": chunk}]), custom=custom))

    if len(chunks) <= 1:
        return _tx(text, list(recent_pairs)[-3:])
    ctx = list(recent_pairs)[-3:]
    out = []
    for ch in chunks:
        t = _tx(ch, ctx)
        out.append(t)
        if t and t != ch:
            ctx = (ctx + [(ch[:160], t[:160])])[-4:]    # running paragraph context for the next chunk
        if on_progress is not None:
            try:
                on_progress(" ".join(p for p in out if p))   # cumulative translation so far
            except Exception:
                pass
    return " ".join(p for p in out if p)




# --- Backend seam -------------------------------------------------------------------------------------
# Everything above (VAD, sentence assembly, scheduler, latency policy, number guard, prompt builders) is
# platform-independent. Only the GPU leaves — transcribe_pcm / translate_once / translate_page_batch_once / run_ask — plus warm and
# ASR-load are runtime-specific. On Apple Silicon they are the MLX functions above (default). With
# LCC_BACKEND=cuda we rebind these SAME module globals to backend_cuda's OpenAI-compatible HTTP client; the
# live loop passes them to executors by name, so it transparently drives a remote llama.cpp/vLLM instead.
# backend_cuda imports the shared prompt builders from THIS module lazily (at call time) — no import cycle.
if model_runtime.BACKEND == "cuda":
    import backend_cuda
    transcribe_pcm, translate_once, translate_page_batch_once, run_ask = (
        backend_cuda.transcribe_pcm, backend_cuda.translate_once,
        backend_cuda.translate_page_batch_once, backend_cuda.run_ask)
    warm_mlx_selected, _ensure_asr_loaded = backend_cuda.warm_selected, backend_cuda.ensure_asr_loaded  # warm = HTTP ping
    print(f"[bridge] backend=cuda  chat={backend_cuda.CHAT_URL}  asr={backend_cuda.ASR_URL}", flush=True)
elif model_runtime.BACKEND == "fake":
    import backend_fake
    transcribe_pcm, translate_once, translate_page_batch_once, run_ask = (
        backend_fake.transcribe_pcm, backend_fake.translate_once,
        backend_fake.translate_page_batch_once, backend_fake.run_ask)
    warm_mlx_selected, _ensure_asr_loaded = backend_fake.warm_selected, backend_fake.ensure_asr_loaded
    VADIterator = backend_fake.FakeVADIterator
    print("[bridge] backend=fake (test-only)", flush=True)
elif os.environ.get("LCC_TX_BACKEND") == "cuda":
    import backend_cuda  # EXPERIMENTAL tx-only hybrid: translate/ask via HTTP, ASR stays MLX (see bind_tx_only)
    translate_once, translate_page_batch_once, run_ask, warm_mlx_selected = backend_cuda.bind_tx_only(warm_mlx_selected)
    print(f"[bridge] tx-backend=cuda (translate/ask only; asr stays mlx)  chat={backend_cuda.CHAT_URL}", flush=True)
# ------------------------------------------------------------------------------------------------------


@dataclass
class Unit:
    """Per-connection translation-unit state (one sentence/clause being assembled), grouped out of
    handle()'s nonlocals. Mutated IN PLACE by next_unit()/clear_unit()/inference_loop and never rebound,
    so the nested closures touch attributes without a `nonlocal`. See docs/caption-lifecycle.md."""
    id: int | None = None        # current unit id (monotonic via unit_seq); None between units
    rev: int = 0                 # source revision within the unit
    src: str = ""                # accumulated source text of the current unit
    start_ms: int = 0
    end_ms: int = 0
    pcm: bytearray = field(default_factory=bytearray)   # audio for the optional accuracy-mode 2-pass
    clauses: int = 0             # clauses folded into this unit (2-pass only pays off when >= 2)
    pure: bool = True            # False once a soft/hard cut or split misaligns pcm vs src
    speaker: int | None = None   # diarize-lite label (tagged at commit; None when off/unknown)
    spk_pcm: bytes = b""         # longest clause audio — embedding fallback when the 2-pass buffer was dropped

    def add_clause_audio(self, audio: bytes, soft: bool):
        """Keep only 2-pass-eligible audio. Once the unit is impure or too long, drop the buffer so a
        pathological no-boundary talk cannot grow this bytearray for the rest of the session."""
        self.clauses += 1
        if soft or not self.pure:
            self.pure = False
            self.pcm.clear()
            return
        max_bytes = int(TWO_PASS_MAX_SEC * SR) * 2
        if len(self.pcm) + len(audio) > max_bytes:
            self.pure = False
            self.pcm.clear()
            return
        self.pcm.extend(audio)


async def handle(ws):
    global _active_ws
    origin = _request_header(ws, "Origin")
    if not _origin_allowed(origin):
        print(f"[bridge] rejected origin={origin!r}", flush=True)
        await ws.close(code=1008, reason="origin not allowed")
        return
    peer = getattr(ws, "remote_address", None)
    print(f"[bridge] client connected origin={origin!r} peer={peer!r}", flush=True)
    loop = asyncio.get_running_loop()        # for thread->loop partial-caption handoff
    vad = VADIterator(model_runtime.silero, threshold=VAD_THRESH[2], sampling_rate=SR,
                      min_silence_duration_ms=SEG_SILENCE_MS, speech_pad_ms=SPEECH_PAD_MS)
    cur_vad_level = 2                       # applied VAD level; rebuild VAD only when this actually changes
    sent_silence_cfg_ms = SENT_SILENCE_MS
    sent_silence_eff_ms = _lat_effective_sent_silence_ms(LATENCY_MODE_DEFAULT, SENT_SILENCE_MS)
    sent_sil_windows = max(1, (sent_silence_eff_ms - SEG_SILENCE_MS) // WINDOW_MS)   # tunable via config
    recent_pairs = collections.deque(maxlen=5)   # last few (source, target) finals -> consistency context
    dom_recent_pairs = collections.deque(maxlen=3)   # page translation consistency, kept out of caption history
    target_lang, context_hint = _normalize_target_lang("Korean"), ""   # set via {"type":"config"} from the client
    asr_engine = model_runtime.ASR_ENGINE
    latency_mode = LATENCY_MODE_DEFAULT
    evs_level = 0               # EVS controller pressure (0 nominal / 1 pressured under backlog); see _evs_step
    register = "casual"         # tone preset (casual/lecture/news/chat) -> few-shot + tone instruction
    glossary_pairs = []         # [(source_term, target_rendering)] pinned for consistent translation + ASR biasing
    term_memory_enabled = True  # session term memory (auto-glossary) — config-gated, default on
    session_terms = {}          # term -> {"count","rendering","seen"}; mined from committed finals
    auto_glossary_pairs = []    # _merge_auto_glossary output, appended to the user glossary at prompt time
    auto_seed_raw = ""          # domain-persisted term seeds pushed by the client config (tab memory)
    finals_since_terms = 0      # batch auto-glossary refreshes (each one re-prefills the KV prefix)
    custom_prompt = ""          # user custom translation prompt (advanced/preset) -> replaces the descriptive part in _tx_system; applies to caption + page
    page_register = "casual"    # page DOM translation has its own concise UI/text replacement prompt profile
    page_context_hint = ""      # if unset, page translation falls back to context_hint + page title auto-prime
    page_glossary_pairs = None  # None = inherit glossary_pairs; [] = intentionally no page-specific glossary
    asr_hint = ""               # context_hint + glossary source terms, fed to the ASR prompt (recomputed on config)
    accuracy_mode = False       # 2-pass: re-transcribe the whole sentence's audio at commit (cleaner finals, +~0.7s)
    diarize_enabled = False     # speaker tagging lite (config-gated; model auto-downloads on first enable)
    diarize_loading = False
    spk_clusters = None         # per-connection OnlineSpeakerClusters (labels reset with the session)
    translation_epoch = 0        # bumps when target/register/hints change so old-language jobs cannot render later
    preroll = collections.deque(maxlen=PREROLL_WINDOWS)   # pre-onset audio prepended on speech start
    leftover, voiced, in_speech, sil_windows = b"", bytearray(), False, 0
    repeat_cache = collections.OrderedDict()   # context-free exact-repeat cache (short self-contained lines)
    aux_tasks = set()           # in-flight aux-translator preview tasks (cancelled at teardown)
    unit = Unit()               # per-connection translation-unit state (grouped from the nonlocals)
    unit_seq = trans_seq = 0
    work_q = asyncio.Queue(maxsize=WORK_Q_MAX)    # ("clause", audio, start_ms, end_ms, soft) | ("flush"/"eos", None, None, ms, False) | None
    trans_q = asyncio.PriorityQueue(maxsize=TRANS_Q_MAX)
    pending_final_jobs = {}
    pending_preview_jobs = {}
    latest_preview_rev = {}
    preview_results = {}
    active_tx_job = None
    last_enqueued_final_source = ""
    scheduler_stats = collections.Counter()
    translation_cache = collections.OrderedDict()
    finalized_units = set()
    commit_carry = {"tail": [], "end_ms": -1}   # last committed sentence's tail + end_ms -> dedupe overlap re-transcription
    preview_sent = {}
    preview_task = None
    seg_count = nospeech_count = preview_drop_count = cache_hit_count = 0
    audio_ms = 0
    speech_start_ms = 0
    la_prev, la_stable, la_count, speech_epoch = [], [], 0, 0   # LocalAgreement state + utterance epoch (stale-partial guard)
    t_conn = time.perf_counter()
    authed = False
    mlx_lock = model_runtime._MLX_DEVICE_LOCK   # global: serialize the single MLX device across ALL connections (was per-conn)

    async def send_json(payload):
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def next_unit(start_ms: int):
        nonlocal unit_seq
        unit_seq += 1
        unit.id = unit_seq
        unit.rev = 0
        unit.start_ms = unit.end_ms = max(0, int(start_ms))
        unit.pcm.clear(); unit.clauses = 0; unit.pure = True   # fresh audio buffer for the new unit
        unit.speaker = None
        unit.spk_pcm = b""
        return unit.id

    def clear_unit():
        unit.src = ""
        unit.start_ms = unit.end_ms = 0
        unit.id = None
        unit.rev = 0
        unit.pcm.clear(); unit.clauses = 0; unit.pure = True
        unit.speaker = None
        unit.spk_pcm = b""

    async def emit_source(text, unit_id, rev, start_ms, end_ms, speaker=None):
        if _short_suffix_duplicate(text, last_enqueued_final_source):
            scheduler_stats["source_drop_suffix_dup"] += 1
            return
        await send_json({
            "type": "source",
            "text": text,
            "unit_id": unit_id,
            "rev": rev,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "speaker": speaker,
        })

    def cache_get(key):
        nonlocal cache_hit_count
        if key not in translation_cache:
            return None
        cache_hit_count += 1
        translation_cache.move_to_end(key)
        return translation_cache[key]

    def cache_put(key, value):
        translation_cache[key] = value
        translation_cache.move_to_end(key)
        while len(translation_cache) > TRANSLATION_CACHE_MAX:
            translation_cache.popitem(last=False)

    def repeat_get(source):
        nonlocal cache_hit_count
        if not TX_REPEAT_CACHE_ON or not _repeat_cache_eligible(source):
            return None
        rk = _repeat_key(source)
        if rk not in repeat_cache:
            return None
        cache_hit_count += 1
        scheduler_stats["repeat_cache_hit"] += 1
        repeat_cache.move_to_end(rk)
        return repeat_cache[rk]

    def repeat_put(source, value):
        if not TX_REPEAT_CACHE_ON or not _repeat_cache_eligible(source):
            return
        repeat_cache[_repeat_key(source)] = value
        repeat_cache.move_to_end(_repeat_key(source))
        while len(repeat_cache) > TX_REPEAT_CACHE_MAX:
            repeat_cache.popitem(last=False)

    # --- session term memory: user glossary + auto-pinned recurring terms, one merged view ---
    def effective_glossary():
        return list(glossary_pairs) + list(auto_glossary_pairs)

    def effective_page_glossary():
        base = page_glossary_pairs if page_glossary_pairs is not None else glossary_pairs
        return list(base) + list(auto_glossary_pairs)

    def rebuild_asr_hint():
        nonlocal asr_hint
        asr_hint = asr.build_asr_hint(context_hint, glossary_pairs, auto_glossary_pairs)

    def apply_term_seeds():
        # Domain-persisted seeds (tab memory) arrive as glossary-format lines; they pre-qualify as if
        # already seen TERM_MEMORY_MIN_COUNT times so a returning visitor gets consistency from line one.
        for s_, t_ in _parse_glossary(auto_seed_raw):
            rec = session_terms.get(s_)
            if rec is None:
                rec = session_terms[s_] = {"count": 0, "rendering": "", "seen": 0.0}
            rec["count"] = max(rec["count"], TERM_MEMORY_MIN_COUNT)
            if t_ and not rec["rendering"]:
                rec["rendering"] = t_

    def refresh_auto_glossary():
        """Recompute the auto-pinned list, keeping the PRIOR ORDER of retained terms so the glossary
        clause (and with it the translator's KV prefix) doesn't churn every time counts reshuffle."""
        if not (TERM_MEMORY_ON and term_memory_enabled):
            changed = bool(auto_glossary_pairs)
            auto_glossary_pairs.clear()
            if changed:
                rebuild_asr_hint()
            return changed
        newmap = {_gr_norm(s): (s, t) for s, t in _merge_auto_glossary(glossary_pairs, session_terms)}
        ordered = []
        for s, _t in auto_glossary_pairs:
            k = _gr_norm(s)
            if k in newmap:
                ordered.append(newmap.pop(k))
        ordered.extend(newmap.values())
        if ordered == auto_glossary_pairs:
            return False
        auto_glossary_pairs[:] = ordered
        rebuild_asr_hint()
        return True

    def _preview_promotable(preview_source, final_source):
        p = _clean(preview_source).lower()
        f = _clean(final_source).lower()
        if not p or not f:
            return False
        if len(f) > max(PENDING_CAP, AGG_PENDING_CAP, BAL_PENDING_CAP) + 40:
            return False
        if p == f:
            return True
        if abs(len(p) - len(f)) > 6:
            return False
        return difflib.SequenceMatcher(None, p, f).ratio() >= PREVIEW_PROMOTE_SIMILARITY

    def tx_recent_for(final: bool):
        if latency_mode == "stable":
            limit = recent_pairs.maxlen or len(recent_pairs)
        elif final:
            limit = TX_RECENT_FINAL_MAX
        else:
            limit = TX_RECENT_PREVIEW_MAX
        return list(recent_pairs)[-limit:] if limit > 0 else []

    # Thin adapters over the pure module-level latency profile (tested in test_latency_profile.py); they
    # bind the live latency_mode / asr_engine so call sites stay declarative.
    def latency_profile():
        return _lat_profile(latency_mode)

    def tx_max_tokens_for(final: bool):
        return _lat_tx_max_tokens_for(final)

    def tx_stream_every_for(final: bool):
        return latency_profile().stream_every(final)

    def preview_debounce_ms():
        return latency_profile().preview_debounce_ms

    def pending_cap():
        return latency_profile().pending_cap_for_pressure(evs_level)

    def pending_max_age_ms():
        return latency_profile().pending_max_age_for_pressure(evs_level)

    def soft_max_sec():
        return _lat_soft_max_sec(asr_engine, latency_mode)

    def effective_sent_silence_ms(raw_ms):
        return _lat_effective_sent_silence_ms(latency_mode, raw_ms)

    def sent_windows_for(raw_ms):
        return _lat_sent_windows_for(latency_mode, raw_ms)

    def final_backlog_count():
        return len(pending_final_jobs) + (1 if active_tx_job and active_tx_job.get("final") else 0)

    def final_backlog_age_ms(now=None):
        now = now or time.perf_counter()
        queued = list(pending_final_jobs.values())
        if active_tx_job and active_tx_job.get("final"):
            queued.append(active_tx_job["queued_at"])
        return int((now - min(queued)) * 1000) if queued else 0

    def _queued_work_kind(item):
        return item[0] if isinstance(item, tuple) and item else None

    def _purge_queued_partials():
        # asyncio.Queue has no public remove API; this runs only on the event loop thread and
        # is used as a backpressure valve so UX-only LA partials never block real clauses/eos.
        q = getattr(work_q, "_queue", None)
        if q is None:
            return 0
        kept = collections.deque(x for x in q if _queued_work_kind(x) != "partial")
        dropped = len(q) - len(kept)
        if dropped:
            q.clear(); q.extend(kept)
            scheduler_stats["work_drop_partial_backpressure"] += dropped
        return dropped

    async def enqueue_work(item):
        kind = _queued_work_kind(item)
        if kind == "partial":
            try:
                work_q.put_nowait(item)
            except asyncio.QueueFull:
                scheduler_stats["work_drop_partial_full"] += 1
            return
        if work_q.full():
            _purge_queued_partials()
        await work_q.put(item)

    def preview_startable():
        if latency_mode == "stable":
            return False
        if pending_preview_jobs:
            return False
        if model_runtime.aux_lm_ready():
            # The aux pool freed previews from the main lock, but DISPLAY ORDER still rules: a preview
            # of unit N+1 painting before unit N's still-translating final makes the client rewind to
            # the previous sentence when that final lands ("이전 자막 깜박임"). Keep the backlog gates;
            # aux still wins by never delaying finals in the scheduler loop and by firing during
            # speech without touching the main model.
            if active_tx_job is not None or final_backlog_count() > 0 or pending_final_jobs:
                return False
            return latency_mode == "aggressive" or ((not in_speech) and work_q.empty())
        if active_tx_job is not None or final_backlog_count() > 0 or pending_final_jobs:
            return False
        if latency_mode == "balanced":
            return (not in_speech) and work_q.empty()
        if model_runtime._is_sherpa_engine(asr_engine):
            return True
        return (not in_speech) and work_q.empty()

    def aux_translation_busy():
        return bool(active_tx_job or pending_final_jobs or pending_preview_jobs or in_speech or not work_q.empty())

    async def wait_aux_translation_slot(max_ms=1800):
        """Let auxiliary work run only while real-time caption translation is idle.

        Page DOM translation, summary/QA, and warm-up all share the effective translation device
        or CUDA endpoint, so they yield to committed/preview caption work first.
        """
        deadline = time.perf_counter() + max(0, max_ms) / 1000
        while aux_translation_busy() and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        return not aux_translation_busy()

    def preview_is_stale(job):   # thin adapter over the pure _preview_is_stale (test_scheduler_staleness.py)
        return _preview_is_stale(job, finalized_units, unit.id, unit.rev, latest_preview_rev)

    async def enqueue_translation(source, final, unit_id, rev, start_ms, end_ms, reason, speaker=None):
        nonlocal trans_seq, preview_task, last_enqueued_final_source, preview_drop_count
        source = source.strip()
        if not source:
            return
        if final and _short_suffix_duplicate(source, last_enqueued_final_source):
            scheduler_stats["final_drop_suffix_dup"] += 1
            return
        if final:
            last_enqueued_final_source = source
            finalized_units.add(unit_id)
            if len(finalized_units) > 512:                      # bound long sessions (unit ids are monotonic)
                for u in sorted(finalized_units)[:-256]:
                    finalized_units.discard(u)
            if preview_task and not preview_task.done():
                preview_task.cancel()
        trans_seq += 1
        priority = 0 if final else 5
        if not final:
            latest_preview_rev[unit_id] = rev
            if len(latest_preview_rev) > 512:                  # bound long sessions (unit ids are monotonic)
                for u in sorted(latest_preview_rev)[:-256]:
                    latest_preview_rev.pop(u, None)
            if trans_q.full():
                preview_drop_count += 1
                scheduler_stats["preview_drop_trans_q_full"] += 1
                return
        await trans_q.put((
            priority,
            trans_seq,
            {
                "seq": trans_seq,
                "source": source,
                "final": final,
                "unit_id": unit_id,
                "rev": rev,
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "reason": reason,
                "speaker": speaker,
                "queued_at": time.perf_counter(),
                "latency_mode": latency_mode,
                "epoch": translation_epoch,
            },
        ))
        if final:
            pending_final_jobs[trans_seq] = time.perf_counter()
        else:
            pending_preview_jobs[trans_seq] = time.perf_counter()

    def schedule_preview(source, unit_id, rev, start_ms, end_ms, speaker=None):
        nonlocal preview_task
        source = source.strip()
        if latency_mode == "stable" or unit_id in finalized_units:
            return
        profile = latency_profile()
        min_chars = profile.preview_min_chars
        min_delta = profile.preview_min_delta
        cooldown = profile.preview_cooldown_ms / 1000
        if len(source) < min_chars:
            return
        last_src, last_t = preview_sent.get(unit_id, ("", 0.0))
        if len(source) - len(last_src) < min_delta and time.perf_counter() - last_t < cooldown:
            return
        if preview_task and not preview_task.done():
            preview_task.cancel()

        async def delayed_preview(c_unit, c_rev, c_source, c_start, c_end, c_speaker):
            nonlocal preview_drop_count
            try:
                await asyncio.sleep(preview_debounce_ms() / 1000)
            except asyncio.CancelledError:
                return
            if c_unit in finalized_units or unit.id != c_unit or unit.rev != c_rev:
                preview_drop_count += 1
                return
            if not preview_startable():
                preview_drop_count += 1
                scheduler_stats["preview_drop_busy"] += 1
                return
            preview_sent[c_unit] = (c_source, time.perf_counter())
            if len(preview_sent) > 512:                          # bound long sessions
                for k in list(preview_sent)[:-256]:
                    del preview_sent[k]
            await enqueue_translation(c_source, False, c_unit, c_rev, c_start, c_end, "preview", speaker=c_speaker)

        preview_task = asyncio.create_task(delayed_preview(unit_id, rev, source, start_ms, end_ms, speaker))

    async def aux_preview(job):
        """Preview rendered by the AUX translator as its own task: the scheduler loop moves straight on
        to finals while the small model draws the throwaway preview in parallel (own pool + lock). Aux
        output is NEVER written to the caches and never promotion-eligible — finals stay main-quality."""
        nonlocal preview_drop_count
        source = job["source"]
        ko_src = target_lang == "Korean" and _src_lang(source) == "Korean"
        recent_ctx = tx_recent_for(False)
        key = (target_lang, register, context_hint, tuple(effective_glossary()), tuple(recent_ctx), source)
        engine = "main" if ko_src else "aux"
        ko = source if ko_src else cache_get(key)
        if ko is None:
            ko = repeat_get(source)
        if ko is not None and not ko_src:
            engine = "main"                                  # cached text came from the main model
        if ko is None:
            try:
                async with model_runtime._AUX_LM_DEVICE_LOCK:
                    ko = await loop.run_in_executor(model_runtime._aux_lm_pool, functools.partial(
                        translate_once, source, recent_ctx, target=target_lang, hint=context_hint,
                        register=register, glossary_pairs=effective_glossary(),
                        max_tokens=tx_max_tokens_for(False), stream_every=tx_stream_every_for(False),
                        profile="caption", custom=custom_prompt, runtime=model_runtime._aux_runtime()))
            except Exception as e:
                print(f"[trans err aux] {e}", flush=True)
                preview_drop_count += 1
                scheduler_stats["preview_drop_tx_error"] += 1
                return
        if not _clean(ko or ""):
            # empty render (small models do this on fragments): showing it would blank the previous
            # caption — drop the preview instead, the source line keeps growing on its own
            preview_drop_count += 1
            scheduler_stats["preview_drop_empty"] += 1
            return
        if job.get("epoch") != translation_epoch:
            scheduler_stats["drop_translation_epoch"] += 1
            return
        if preview_is_stale(job):
            preview_drop_count += 1
            scheduler_stats["preview_drop_stale"] += 1
            return
        preview_results[job["unit_id"]] = {"rev": job["rev"], "source": source, "ko": ko,
                                           "at": time.perf_counter(), "engine": engine}
        if len(preview_results) > 256:
            for k in list(preview_results)[:-128]:
                preview_results.pop(k, None)
        scheduler_stats["preview_aux"] += 1
        await send_json({
            "type": "caption_partial", "kind": "preview", "phase": "preview",
            "unit_id": job["unit_id"], "rev": job["rev"], "source": source, "ko": ko,
            "start_ms": job["start_ms"], "end_ms": job["end_ms"], "display_ms": _caption_read_ms(ko),
            "speaker": job.get("speaker"),
            "reason": job["reason"], "risk": _source_risk(source), "scheduler_mode": latency_mode,
            "evs": evs_level, "cache_hit": engine == "main", "preview_promoted": False,
            "degraded": False, "number_uncertain": False, "translation_error": False,
        })

    async def translation_loop():
        nonlocal preview_drop_count, active_tx_job, finals_since_terms
        while True:
            _prio, _seq, job = await trans_q.get()
            if job is None:
                break
            pending_final_jobs.pop(_seq, None)
            pending_preview_jobs.pop(_seq, None)
            if job.get("epoch") != translation_epoch:
                scheduler_stats["drop_translation_epoch"] += 1
                continue
            if preview_is_stale(job):
                preview_drop_count += 1
                scheduler_stats["preview_drop_stale"] += 1
                continue
            if not job["final"] and not preview_startable():
                preview_drop_count += 1
                scheduler_stats["preview_drop_busy"] += 1
                continue
            if not job["final"] and model_runtime.aux_lm_ready():
                t = asyncio.create_task(aux_preview(job))    # don't block finals behind a preview
                aux_tasks.add(t)
                t.add_done_callback(aux_tasks.discard)
                continue
            t0 = time.perf_counter()
            wait_ms = int((t0 - job["queued_at"]) * 1000)
            source = job["source"]
            ko_src = target_lang == "Korean" and _src_lang(source) == "Korean"  # whole-line lang, not any-hangul
            tx_ok = True
            tx_degraded = False
            preview_promoted = False
            if ko_src:
                ko, hit = source, True
            else:
                recent_ctx = tx_recent_for(job["final"])
                key = (
                    target_lang, register, context_hint, tuple(effective_glossary()),
                    tuple(recent_ctx), source,
                )
                ko = None
                hit = False
                if job["final"]:
                    prev = preview_results.get(job["unit_id"])
                    # aux previews are speed-layer output; never promote them into a final
                    if prev and prev.get("engine", "main") != "aux" and _preview_promotable(prev["source"], source):
                        ko = prev["ko"]
                        hit = True
                        preview_promoted = True
                        scheduler_stats["preview_promoted"] += 1
                    elif prev:
                        scheduler_stats["preview_discarded"] += 1
                if ko is None:
                    ko = cache_get(key)
                    hit = ko is not None
                if ko is None:
                    ko = repeat_get(source)              # context-free exact repeat (catchphrases)
                    hit = ko is not None
                if ko is None:
                    # MLX ASR shares the MLX worker with translation. Sherpa ASR (Parakeet) has its own
                    # CPU pool, so do not stall translation behind sherpa speech backlog.
                    if not model_runtime._is_sherpa_engine(asr_engine):
                        guard = 0
                        while not work_q.empty() and guard < 50:
                            await asyncio.sleep(0.02); guard += 1
                    active_tx_job = job
                    try:
                        async with mlx_lock:
                            if job["final"]:
                                tslot = {"ko": ""}; tev = asyncio.Event(); tdone = False
                                def _on_tx(p):
                                    tslot["ko"] = p; loop.call_soon_threadsafe(tev.set)
                                async def _tx_pump():
                                    last = ""; tt = 0.0
                                    while not tdone:
                                        await tev.wait(); tev.clear()
                                        p = tslot["ko"]
                                        if p and p != last and time.perf_counter() - tt >= 0.08:
                                            p = _clean(p)
                                            if not _stream_partial_should_emit(p, last):
                                                scheduler_stats["final_stream_suppressed"] += 1
                                                continue
                                            if job.get("epoch") != translation_epoch:
                                                scheduler_stats["drop_translation_epoch"] += 1
                                                continue
                                            tt = time.perf_counter(); last = p
                                            await send_json({
                                                "type": "caption_partial", "kind": "final_stream", "phase": "final_stream",
                                                "unit_id": job["unit_id"], "rev": job["rev"], "source": source,
                                                "ko": p, "start_ms": job["start_ms"], "end_ms": job["end_ms"],
                                                "display_ms": _caption_read_ms(p), "speaker": job.get("speaker"),
                                            })
                                tpt = asyncio.create_task(_tx_pump())
                                try:
                                    ko = await loop.run_in_executor(
                                        model_runtime._mlx_pool, translate_once, source, recent_ctx,
                                        target_lang, context_hint, register, effective_glossary(), _on_tx,
                                        None, tx_max_tokens_for(True), tx_stream_every_for(True), "caption", custom_prompt)
                                except Exception as e:
                                    print(f"[trans err] {e}", flush=True); tx_ok = False   # never kill the loop
                                    _lp = _clean(tslot.get("ko", ""))
                                    if _usable_tx_partial(_lp):
                                        ko, tx_degraded = _lp, True   # show the last good KO partial, not English source
                                    else:
                                        ko = source
                                finally:
                                    tdone = True; tev.set()
                                    try: await tpt
                                    except Exception: pass
                            else:
                                try:
                                    ko = await loop.run_in_executor(
                                        model_runtime._mlx_pool, translate_once, source, recent_ctx,
                                        target_lang, context_hint, register, effective_glossary(), None,
                                        None, tx_max_tokens_for(job["final"]), tx_stream_every_for(job["final"]), "caption", custom_prompt)
                                except Exception as e:
                                    # preview is UX-only — leave ko unset and drop below; never flash the source
                                    print(f"[trans err] {e}", flush=True); tx_ok = False
                    finally:
                        active_tx_job = None
                    if job.get("epoch") != translation_epoch:
                        scheduler_stats["drop_translation_epoch"] += 1
                        continue
                    if tx_ok and not _clean(ko or ""):
                        tx_ok = False        # empty render: treat like a failed translation
                        ko = source          # a final then shows the source (degraded) instead of a blank line
                    if tx_ok:
                        cache_put(key, ko)   # don't cache the untranslated fallback
                        repeat_put(source, ko)
            if not job["final"] and not tx_ok:
                # preview is UX-only: a failed translation must NOT flash the English source as a caption
                preview_drop_count += 1
                scheduler_stats["preview_drop_tx_error"] += 1
                continue
            if preview_is_stale(job):
                preview_drop_count += 1
                scheduler_stats["preview_drop_stale"] += 1
                continue
            if not job["final"] and tx_ok:
                preview_results[job["unit_id"]] = {
                    "rev": job["rev"],
                    "source": source,
                    "ko": ko,
                    "at": time.perf_counter(),
                }
                if len(preview_results) > 256:
                    for k in list(preview_results)[:-128]:
                        preview_results.pop(k, None)
            disp_ko, num_uncertain = _guard_numbers(source, ko) if job["final"] else (ko, False)
            mtype = "caption" if job["final"] else "caption_partial"
            await send_json({
                "type": mtype,
                "kind": "commit" if job["final"] else "preview",
                "unit_id": job["unit_id"],
                "rev": job["rev"],
                "source": source,
                "ko": disp_ko,
                "start_ms": job["start_ms"],
                "end_ms": job["end_ms"],
                "display_ms": _caption_read_ms(disp_ko),
                "speaker": job.get("speaker"),
                "translation_wait_ms": wait_ms,
                "translation_ms": int((time.perf_counter() - t0) * 1000),
                "translation_queue_depth": len(pending_final_jobs),
                "translation_backlog_ms": final_backlog_age_ms(),
                "cache_hit": hit,
                "reason": job["reason"],
                "risk": _source_risk(source),
                "scheduler_mode": latency_mode,
                "evs": evs_level,
                "preview_promoted": preview_promoted,
                "phase": "degraded_stream" if tx_degraded else ("final" if job["final"] else "preview"),
                "degraded": tx_degraded,
                "number_uncertain": num_uncertain,
                "translation_error": not tx_ok,
            })
            if job["final"]:
                if not ko_src and tx_ok:                       # skip Korean->Korean + don't poison context on tx failure
                    recent_pairs.append((source[:160], ko[:160]))
                    if TERM_MEMORY_ON and term_memory_enabled:
                        notable = _update_term_memory(session_terms, source, ko, time.perf_counter())
                        finals_since_terms += 1
                        # refresh on a NOTABLE change (new pin / new verbatim rendering) or every N finals;
                        # each refresh that changes the clause re-prefills the translator KV prefix once.
                        if notable or finals_since_terms >= TERM_MEMORY_UPDATE_EVERY:
                            finals_since_terms = 0
                            if refresh_auto_glossary():
                                await send_json({"type": "term_memory",
                                                 "terms": [[s, t] for s, t in auto_glossary_pairs]})
                print(
                    f"[cap {time.perf_counter()-t0:.1f}s wait={wait_ms}ms cache={hit} "
                    f"q={len(pending_final_jobs)} backlog={final_backlog_age_ms()}ms "
                    f"mode={latency_mode} promoted={preview_promoted} reason={job['reason']}] {source}  ->  {ko}",
                    flush=True,
                )
                preview_results.pop(job["unit_id"], None)

    async def _on_asr_pool(engine, fn, *fn_args):
        # Route an ASR-pool job to the right executor: sherpa (Parakeet) runs on the dedicated CPU pool with no
        # MLX lock; the in-process MLX audio model (granite/qwen3) runs on its OWN model_runtime._asr_pool + model_runtime._ASR_DEVICE_LOCK
        # so it OVERLAPS 26B translation on the single GPU (26B decode is bandwidth-bound; small ASR fills the
        # compute gap). Translation keeps mlx_lock + model_runtime._mlx_pool; the two locks are disjoint so they run together.
        if model_runtime._is_sherpa_engine(engine):
            return await loop.run_in_executor(model_runtime._sherpa_pool, fn, *fn_args)
        async with model_runtime._ASR_DEVICE_LOCK:
            return await loop.run_in_executor(model_runtime._asr_pool, fn, *fn_args)

    async def transcribe(audio):
        if len(audio) < int(MIN_SEC * SR) * 2:
            return None
        try:
            out = await _on_asr_pool(asr_engine, transcribe_pcm, audio, asr_hint, asr_engine)
            # Glossary spelling repair runs on every ASR product (clauses, LA partials, 2-pass) so the
            # canonical term reaches translation no matter which path produced the text.
            if out and glossary_pairs:
                out = _repair_glossary_terms(out, glossary_pairs)
            return out
        except Exception as e:
            # A transient ASR failure (OOM, a malformed frame, a model hiccup) must never kill
            # inference_loop — it has no other guard, and a dead loop silently freezes all captions
            # and then wedges the WS reader on a full work_q. Treat it as no-speech and carry on,
            # mirroring translation_loop's "never kill the loop" stance.
            print(f"[asr err] {e}", flush=True)
            return None

    async def tag_unit_speaker():
        """Commit-time speaker tagging: the unit's WHOLE clean audio (the accuracy-mode 2-pass buffer,
        usually a full sentence) beats the old first-clause fragment by a wide margin; impure/overlong
        units fall back to their longest clause. One CPU embedding per unit; never blocks a commit on
        failure."""
        if not (diarize_enabled and spk_clusters is not None) or unit.speaker is not None:
            return
        min_bytes = int(model_runtime._diarize().SPK_MIN_SEC * SR) * 2
        spk_src = bytes(unit.pcm) if (unit.pure and len(unit.pcm) >= min_bytes) else unit.spk_pcm
        if len(spk_src) < min_bytes:
            return
        try:
            emb = await loop.run_in_executor(model_runtime._sherpa_pool, model_runtime._diarize().embed, spk_src)
            if emb:
                unit.speaker = spk_clusters.add(emb)
        except Exception as e:
            print(f"[diarize err] {e}", flush=True)

    async def inference_loop():
        # ASR stays separate from translation: this loop creates fast source atoms and translation units,
        # while translation_loop decides which final/preview jobs deserve the 26B.
        nonlocal seg_count, nospeech_count, la_prev, la_stable, evs_level
        while True:
            item = await work_q.get()
            if item is None:
                break
            batch = [item]
            stop_after_batch = False
            while not work_q.empty():
                nxt = work_q.get_nowait()
                if nxt is None:
                    stop_after_batch = True
                    break
                batch.append(nxt)
            batch = _coalesce_batch(batch)                     # drop stale LA partials when finalizable work is queued
            finalize_now = eos_now = False
            boundary_ms = unit.end_ms
            for kind, audio, start_ms, end_ms, _soft, *_rest in batch:
                if kind == "partial":                          # LocalAgreement: stream confirmed words as live source (LCC_LA=1)
                    if _rest and _rest[0] != speech_epoch:      # stale partial from a previous utterance
                        continue
                    hyp = await transcribe(audio)
                    if _rest and _rest[0] != speech_epoch:      # epoch changed during transcribe -> result is stale
                        continue
                    hw = (hyp or "").split()
                    k = _lcp_words(la_prev, hw); la_prev = hw
                    ns = hw[:k]                                 # words agreed by two consecutive hypotheses -> stable
                    if len(ns) > len(la_stable):
                        la_stable = ns
                        if unit.id is None:
                            next_unit(start_ms)
                        unit.rev += 1                        # source revision bumps as confirmed prefix grows
                        unit.end_ms = max(unit.end_ms, int(end_ms))
                        await emit_source(" ".join(la_stable), unit.id, unit.rev,
                                          unit.start_ms, unit.end_ms, unit.speaker)
                    continue
                if kind == "clause":
                    src = await transcribe(audio)
                    seg_count += 1
                    if src:
                        if unit.id is None:
                            next_unit(start_ms)
                        old = unit.src
                        if not unit.src and commit_carry["tail"]:   # fresh unit right after a commit: a soft/VAD
                            src = _dedupe_commit_overlap(            # overlap can re-transcribe the boundary word
                                src, commit_carry["tail"], start_ms <= commit_carry["end_ms"])
                        unit.src = _append_text_dedupe(unit.src, src)
                        if unit.src != old:
                            unit.rev += 1
                        unit.add_clause_audio(audio, _soft)          # bounded audio for optional 2-pass at commit
                        unit.end_ms = max(unit.end_ms, int(end_ms))
                        if (diarize_enabled or diarize_loading) and len(audio) > len(unit.spk_pcm):
                            unit.spk_pcm = audio        # embedding fallback for impure/overlong units
                    else:
                        nospeech_count += 1
                else:                                  # "flush" (long pause) or "eos"
                    finalize_now = True
                    eos_now = eos_now or kind == "eos"
                    boundary_ms = max(boundary_ms, int(end_ms or boundary_ms))
            # commit COMPLETE sentences one at a time (terminal punctuation), so fast speech doesn't pile
            # multiple sentences into one 160-char block. The client paces their display.
            first_split = True
            while True:
                cut = _next_sentence_cut(unit.src)
                if cut < 0:
                    break
                sent, unit.src = unit.src[:cut].strip(), unit.src[cut:].strip()
                if sent:
                    commit_src = sent
                    # accuracy mode: re-transcribe the whole sentence's audio, but ONLY when this sentence
                    # IS the entire current unit (first split, nothing after) so unit.pcm aligns with it.
                    # Multi-sentence batches can't be isolated per-sentence -> they stay 1-pass.
                    if (first_split and not unit.src
                            and _two_pass_eligible(accuracy_mode, unit.pure, unit.clauses, len(unit.pcm))):
                        clean = await transcribe(bytes(unit.pcm))
                        if clean:
                            commit_src = clean
                    await tag_unit_speaker()
                    await emit_source(
                        commit_src, unit.id, unit.rev, unit.start_ms, unit.end_ms, unit.speaker
                    )
                    await enqueue_translation(
                        commit_src, True, unit.id, unit.rev, unit.start_ms, unit.end_ms, "punct",
                        speaker=unit.speaker,
                    )
                    commit_carry["tail"], commit_carry["end_ms"] = _norm_words(commit_src)[-3:], unit.end_ms
                first_split = False
                if unit.src:
                    next_unit(unit.end_ms)
                    unit.rev = 1
                    unit.pure = False    # remainder's audio was consumed by the committed sentence -> no clean 2-pass
                else:
                    clear_unit()
            # the in-progress remainder: show it growing, or commit on a real pause / eos / length cap
            evs_level = _evs_step(evs_level, final_backlog_age_ms())   # EVS: shift the commit band under sustained backlog
            if unit.src:
                await emit_source(unit.src, unit.id, unit.rev, unit.start_ms, unit.end_ms, unit.speaker)
                age_ms = max(0, int((boundary_ms or unit.end_ms) - unit.start_ms))
                decision = decide_commit(
                    unit.src, eos_now, finalize_now, age_ms, pending_cap(), pending_max_age_ms())
                force_commit, reason = decision.action == "commit", decision.reason
                if force_commit:
                    final_src = unit.src
                    # accuracy mode: a multi-clause sentence ending on a natural pause/eos was stitched from
                    # independently-transcribed VAD chunks (boundary words can be split/garbled). Re-transcribe
                    # the whole sentence's audio once for a clean final. Skipped when unit.pure is False
                    # (overlap/split made unit.pcm misaligned) or the buffer is too short/long.
                    if _two_pass_eligible(accuracy_mode, unit.pure, unit.clauses, len(unit.pcm)):
                        clean = await transcribe(bytes(unit.pcm))
                        if clean:
                            final_src = clean
                    await tag_unit_speaker()
                    await enqueue_translation(
                        final_src, True, unit.id, unit.rev, unit.start_ms, unit.end_ms, reason,
                        speaker=unit.speaker,
                    )
                    commit_carry["tail"], commit_carry["end_ms"] = _norm_words(final_src)[-3:], unit.end_ms
                    clear_unit()
                else:
                    schedule_preview(unit.src, unit.id, unit.rev, unit.start_ms, unit.end_ms, unit.speaker)
            if stop_after_batch:
                break

    inf_task = asyncio.create_task(inference_loop())
    trans_task = asyncio.create_task(translation_loop())

    try:
        async for msg in ws:
            if isinstance(msg, str):
                if len(msg.encode("utf-8")) > MAX_WS_MSG_BYTES:     # control msgs (e.g. ask transcript) ~ MAX_WS_MSG_BYTES, not the audio-frame cap
                    await ws.close(code=1009, reason="control message too large")
                    break
                try:
                    d = json.loads(msg)
                    if d.get("type") == "hello":
                        if str(d.get("token", "")) != WS_TOKEN:
                            print(f"[bridge] bad token origin={origin!r}", flush=True)
                            await ws.close(code=1008, reason="bad token")
                            break
                        authed = True
                        await send_json({"type": "hello", "ok": True})
                        print(f"[bridge] client authed origin={origin!r} peer={peer!r}", flush=True)
                        if _active_ws is not None and _active_ws is not ws:   # single-client model: evict the prior
                            # A reload/multi-tab/zombie leaves a stale ws; ping_interval is off (heavy inference
                            # starves keepalive), so it would otherwise linger ~244s sharing the ONE MLX device +
                            # KV cache (degraded latency). Close it now so the new client runs alone.
                            print("[bridge] superseding prior client — single MLX device; closing the stale connection.", flush=True)
                            _stale_ws = _active_ws
                            asyncio.create_task(_stale_ws.close(code=1001, reason="superseded by a new client"))
                        _active_ws = ws
                    elif not authed:
                        await ws.close(code=1008, reason="hello required")
                        break
                    elif d.get("type") == "config":
                        prev_tx_sig = _translation_context_signature(target_lang, register, context_hint, glossary_pairs, custom_prompt)
                        prev_page_glossary = page_glossary_pairs if page_glossary_pairs is not None else glossary_pairs
                        prev_page_sig = _translation_context_signature(
                            target_lang, page_register, page_context_hint or context_hint, prev_page_glossary, custom_prompt)
                        if d.get("latencyMode") is not None:
                            latency_mode = model_runtime._normalize_latency_mode(d.get("latencyMode"), latency_mode)
                            sent_sil_windows = sent_windows_for(sent_silence_cfg_ms)
                        if d.get("asrEngine") is not None:
                            requested_engine = model_runtime._normalize_asr_engine(d.get("asrEngine"), asr_engine)
                            try:
                                await _on_asr_pool(requested_engine, _ensure_asr_loaded, requested_engine)
                                asr_engine = requested_engine
                            except Exception as e:
                                await send_json({"type": "err", "text": f"ASR 엔진 전환 실패({requested_engine}): {e}"})
                                print(f"[bridge] asr switch failed engine={requested_engine}: {e}", flush=True)
                        if d.get("vadLevel") is not None:
                            vad_level = model_runtime._clamp_int(d.get("vadLevel"), 2, 0, 3)
                            if vad_level != cur_vad_level:    # rebuild only on real change — a live config push (glossary/slider/lang)
                                cur_vad_level = vad_level      # must not discard the in-progress utterance just by arriving
                                thr = VAD_THRESH.get(vad_level, 0.5)
                                vad = VADIterator(model_runtime.silero, threshold=thr, sampling_rate=SR,
                                                  min_silence_duration_ms=SEG_SILENCE_MS, speech_pad_ms=SPEECH_PAD_MS)
                                # Rebuilding resets VAD state, so flush any in-flight utterance as a soft clause
                                # first — otherwise changing the level mid-speech silently drops it.
                                if in_speech and voiced:
                                    await enqueue_work(("clause", bytes(voiced), speech_start_ms, audio_ms, True))
                                in_speech, voiced = False, bytearray(); preroll.clear()
                        if d.get("sentSilenceMs") is not None:           # 0 is valid, don't truthiness-skip
                            sent_silence_cfg_ms = model_runtime._clamp_int(d.get("sentSilenceMs"), SENT_SILENCE_MS, 500, 5000)
                            sent_sil_windows = sent_windows_for(sent_silence_cfg_ms)
                        if d.get("targetLang"):
                            target_lang = _normalize_target_lang(d.get("targetLang"), target_lang)
                        if d.get("contextHint") is not None:
                            context_hint = str(d["contextHint"])[:200]
                        if d.get("register"):
                            register = str(d["register"]) if str(d["register"]) in _REGISTERS else "casual"
                        if d.get("glossary") is not None:
                            glossary_pairs = _parse_glossary(str(d["glossary"]))
                        if d.get("customPrompt") is not None:
                            custom_prompt = str(d["customPrompt"])[:4000]   # user custom translation prompt (advanced/preset)
                        if d.get("pageContextHint") is not None:
                            page_context_hint = str(d["pageContextHint"])[:240]
                        if d.get("pageRegister"):
                            page_register = str(d["pageRegister"]) if str(d["pageRegister"]) in _REGISTERS else "casual"
                        if "pageGlossary" in d:
                            raw_page_glossary = str(d.get("pageGlossary") or "")
                            page_glossary_pairs = _parse_glossary(raw_page_glossary) if raw_page_glossary.strip() else None
                        if d.get("accuracyMode") is not None:
                            accuracy_mode = model_runtime._config_bool(d.get("accuracyMode"), accuracy_mode)
                        if d.get("diarize") is not None:
                            _want_diarize = model_runtime._config_bool(d.get("diarize"), diarize_enabled)
                            if not _want_diarize:
                                diarize_enabled = False
                            elif spk_clusters is not None:
                                diarize_enabled = True
                            elif not diarize_loading:
                                diarize_loading = True

                                async def _enable_diarize():
                                    nonlocal diarize_enabled, diarize_loading, spk_clusters
                                    try:
                                        await loop.run_in_executor(model_runtime._sherpa_pool, model_runtime._diarize().ensure_extractor)
                                        spk_clusters = model_runtime._diarize().OnlineSpeakerClusters()
                                        diarize_enabled = True
                                        await send_json({"type": "notice", "text": "화자 구분 켜짐"})
                                    except Exception as e:
                                        await send_json({"type": "err", "text": f"화자 구분 사용 불가: {e}"})
                                        print(f"[diarize] enable failed: {e}", flush=True)
                                    finally:
                                        diarize_loading = False

                                asyncio.create_task(_enable_diarize())
                        if d.get("termMemory") is not None:
                            term_memory_enabled = model_runtime._config_bool(d.get("termMemory"), term_memory_enabled)
                        if d.get("autoGlossary") is not None:     # domain-persisted term seeds (tab memory)
                            auto_seed_raw = str(d.get("autoGlossary") or "")[:4000]
                            if TERM_MEMORY_ON and term_memory_enabled:
                                apply_term_seeds()
                        refresh_auto_glossary()                   # also folds seeds in / clears when disabled
                        rebuild_asr_hint()   # free-text context + glossary terms + auto-pinned terms -> ASR name biasing
                        new_tx_sig = _translation_context_signature(target_lang, register, context_hint, glossary_pairs, custom_prompt)
                        new_page_glossary = page_glossary_pairs if page_glossary_pairs is not None else glossary_pairs
                        new_page_sig = _translation_context_signature(
                            target_lang, page_register, page_context_hint or context_hint, new_page_glossary, custom_prompt)
                        if new_tx_sig != prev_tx_sig:
                            translation_epoch += 1
                            recent_pairs.clear()
                            translation_cache.clear()
                            repeat_cache.clear()
                            preview_results.clear()
                            latest_preview_rev.clear()
                            pending_preview_jobs.clear()
                            pending_final_jobs.clear()
                            # mined renderings belonged to the OLD translation context -> re-mine; the
                            # domain seeds (user-blessed by persistence) re-apply immediately.
                            session_terms.clear()
                            auto_glossary_pairs.clear()
                            finals_since_terms = 0
                            if TERM_MEMORY_ON and term_memory_enabled:
                                apply_term_seeds()
                                refresh_auto_glossary()
                            rebuild_asr_hint()
                            print(f"[cfg] translation context reset epoch={translation_epoch} target={target_lang}", flush=True)
                        if new_page_sig != prev_page_sig:
                            dom_recent_pairs.clear()
                            print(f"[cfg] page translation context reset target={target_lang}", flush=True)
                        print(f"[cfg] vad={d.get('vadLevel')} sentSil={sent_silence_cfg_ms}/{effective_sent_silence_ms(sent_silence_cfg_ms)} target={target_lang} "
                              f"reg={register} asr={asr_engine} latency={latency_mode} acc={accuracy_mode} gloss={len(glossary_pairs)} "
                              f"pageReg={page_register} pageGloss={len(new_page_glossary)} hint={asr_hint[:30]!r}", flush=True)
                    elif d.get("type") == "eos":
                        if in_speech and voiced:
                            await enqueue_work(("clause", bytes(voiced), speech_start_ms, audio_ms, False))
                        voiced, in_speech = bytearray(), False
                        await enqueue_work(("eos", None, None, audio_ms, False))   # finalize current sentence
                        try: vad.reset_states()
                        except Exception: pass
                    elif d.get("type") == "ask":           # on-demand summary / Q&A over the transcript
                        tr = str(d.get("transcript", ""))[-8000:]    # recent window (v1: long talks summarize the tail)
                        q = str(d.get("question", ""))[:500]
                        mode = "qa" if (d.get("mode") == "qa" and q.strip()) else "summary"
                        if not tr.strip():
                            await send_json({"type": "answer", "text": "(아직 자막 기록이 없어요)"})
                        elif not await wait_aux_translation_slot(2200):
                            await send_json({"type": "answer", "text": "지금은 자막 번역을 우선 처리 중이라 요약/질문은 잠시 뒤 다시 눌러줘."})
                            print("[ask defer] live caption backlog has priority", flush=True)
                        else:
                            a_slot = {"t": ""}; a_ev = asyncio.Event(); a_done = False
                            def a_partial(p):
                                a_slot["t"] = p; loop.call_soon_threadsafe(a_ev.set)
                            async def a_pump():
                                while not a_done:
                                    await a_ev.wait(); a_ev.clear()
                                    if a_slot["t"]:
                                        try: await send_json({"type": "answer_partial", "text": a_slot["t"]})
                                        except Exception: pass
                            t0 = time.perf_counter()
                            print(f"[ask] mode={mode} q={q[:40]!r} tr={len(tr)}c", flush=True)
                            async with mlx_lock:
                                apt = asyncio.create_task(a_pump())
                                try:
                                    ans = await loop.run_in_executor(model_runtime._mlx_pool, run_ask, mode, tr, q, target_lang, a_partial)
                                finally:
                                    a_done = True; a_ev.set()
                                    await asyncio.gather(apt, return_exceptions=True)
                            await send_json({"type": "answer", "text": ans})
                            print(f"[ask {time.perf_counter()-t0:.1f}s] -> {ans[:60]}", flush=True)
                    elif d.get("type") == "dom_translate_batch":
                        request_id = str(d.get("request_id", ""))[:100]
                        items = _dom_translate_items(d)
                        if not request_id:
                            await send_json({"type": "dom_translate_err", "request_id": "", "text": "missing request_id"})
                            continue
                        if not items:
                            await send_json({"type": "dom_translate_done", "request_id": request_id, "count": 0})
                            continue
                        page_glossary = effective_page_glossary()   # user page/caption glossary + auto-pinned terms
                        page_hint = page_context_hint or context_hint
                        # short items ride the marker microbatch; long paragraphs take the sentence-chunked,
                        # context-preserving path (translated separately so the batch call never goes huge).
                        # Policy-R items carry inline ⟦n⟧ placeholders; the long path sentence-chunks, which would
                        # split a placeholder across chunks — keep them on the marker-batch (short) path regardless of length.
                        short = [it for it in items if len(it["text"]) <= PAGE_LONG_CHARS or "⟦" in it["text"]]
                        longs = [it for it in items if len(it["text"]) > PAGE_LONG_CHARS and "⟦" not in it["text"]]
                        partial_requested = bool(d.get("partial"))
                        verify_requested = model_runtime._config_bool(d.get("verify"), False)
                        # Aux routing: short microbatches + per-item shorts go to the AUX translator when
                        # resident — page DOM stops contending with captions entirely (no busy deference).
                        # Long paragraphs and verify re-checks stay on the MAIN model (quality layer).
                        use_aux = model_runtime.aux_lm_ready() and not verify_requested
                        dom_lock = model_runtime._AUX_LM_DEVICE_LOCK if use_aux else mlx_lock
                        dom_pool = model_runtime._aux_lm_pool if use_aux else model_runtime._mlx_pool
                        dom_engine = "aux" if use_aux else "main"
                        print(f"[dom-tx] request={request_id} items={len(items)} short={len(short)} long={len(longs)} "
                              f"partial={int(partial_requested)} engine={dom_engine}", flush=True)
                        sent = [0]
                        sent_ids = set()
                        last_partial = {}
                        deferred = False

                        async def _send_seg(item_id, source, target, engine=None):
                            out = _clean(target)
                            if out and out != source:
                                dom_recent_pairs.append((source[:160], out[:160]))
                            sent[0] += 1
                            await send_json({
                                "type": "dom_translate_result", "request_id": request_id,
                                "item_id": item_id, "source": source, "target": out,
                                "engine": engine or dom_engine,
                            })

                        async def _send_partial(item_id, source, target):   # speculative UI only; never cached
                            out = _clean(target)
                            if not out or last_partial.get(item_id) == out:
                                return
                            last_partial[item_id] = out
                            await send_json({
                                "type": "dom_translate_partial", "request_id": request_id,
                                "item_id": item_id, "source": source, "target": out,
                            })

                        if len(short) > 1:
                            if not use_aux and not await wait_aux_translation_slot(1200):
                                await send_json({"type": "dom_translate_busy", "request_id": request_id, "retry_ms": 1800})
                                print("[dom-tx defer] live caption backlog has priority", flush=True)
                                deferred = True
                            else:
                                seg_q = asyncio.Queue()
                                _SEG_DONE = object()

                                def _on_segment(item_id, source, target, _q=seg_q):
                                    loop.call_soon_threadsafe(_q.put_nowait, ("final", item_id, source, target))

                                def _on_partial(item_id, source, target, _q=seg_q):
                                    if partial_requested:
                                        loop.call_soon_threadsafe(_q.put_nowait, ("partial", item_id, source, target))

                                def _worker(_q=seg_q, _done=_SEG_DONE):
                                    try:
                                        return translate_page_batch_once(
                                            [dict(item) for item in short],
                                            list(dom_recent_pairs),
                                            target=target_lang, hint=page_hint, register=page_register,
                                            glossary_pairs=list(page_glossary), on_segment=_on_segment,
                                            on_partial=(_on_partial if partial_requested else None),
                                            custom=custom_prompt,
                                            runtime=(model_runtime._aux_runtime() if use_aux else None),
                                        )
                                    finally:
                                        loop.call_soon_threadsafe(_q.put_nowait, _done)

                                batch_t0 = time.perf_counter()
                                batch_out = {}
                                async with dom_lock:                 # GPU busy for the whole batch; segments stream out as they land
                                    fut = loop.run_in_executor(dom_pool, _worker)
                                    while True:
                                        seg = await seg_q.get()
                                        if seg is _SEG_DONE:
                                            break
                                        seg_kind, seg_id, seg_src, seg_tgt = seg
                                        if seg_id in sent_ids:
                                            continue
                                        if seg_kind == "partial":
                                            await _send_partial(seg_id, seg_src, seg_tgt)
                                            continue
                                        sent_ids.add(seg_id)
                                        await _send_seg(seg_id, seg_src, seg_tgt)
                                    try:
                                        batch_out = await fut
                                        print(f"[dom-tx batch ok] request={request_id} items={len(items)} "
                                              f"sent={len(sent_ids)} ms={int((time.perf_counter() - batch_t0) * 1000)}", flush=True)
                                    except Exception as e:
                                        batch_out = {}
                                        print(f"[dom-tx batch fallback] {e}", flush=True)
                                for item in short:                   # parsed but not streamed (VLM dict / partial stream)
                                    if item["id"] in sent_ids:
                                        continue
                                    if item["id"] in batch_out:
                                        sent_ids.add(item["id"])
                                        await _send_seg(item["id"], item["text"], batch_out[item["id"]])
                        if not deferred:
                            for item in short:                       # per-item fallback: single short item, or batch misses
                                if item["id"] in sent_ids:
                                    continue
                                if not use_aux and not await wait_aux_translation_slot(1200):
                                    await send_json({"type": "dom_translate_busy", "request_id": request_id, "retry_ms": 1800})
                                    print("[dom-tx defer] live caption backlog has priority", flush=True)
                                    deferred = True
                                    break
                                source = item["text"]
                                try:
                                    partial_q = asyncio.Queue()
                                    _PARTIAL_DONE = object()
                                    partial_task = None
                                    want_partial = partial_requested and len(source) <= PAGE_TX_PARTIAL_SOURCE_MAX_CHARS

                                    def _single_partial(p, _q=partial_q, _on=want_partial):
                                        if _on:
                                            loop.call_soon_threadsafe(_q.put_nowait, p)

                                    async def _single_partial_pump(_q=partial_q, _done=_PARTIAL_DONE, _item=item, _src=source):
                                        last, last_t = "", 0.0
                                        while True:
                                            p = await _q.get()
                                            if p is _done:
                                                break
                                            now = time.perf_counter()
                                            outp = _clean(p)
                                            if not _page_partial_should_emit(outp, last, now, last_t):
                                                continue
                                            last, last_t = outp, now
                                            if _item["id"] not in sent_ids:
                                                await _send_partial(_item["id"], _src, outp)

                                    page_tx = functools.partial(
                                        translate_once, source, list(dom_recent_pairs),
                                        target=target_lang, hint=page_hint, register=page_register,
                                        glossary_pairs=list(page_glossary),
                                        on_update=(_single_partial if want_partial else None),
                                        kv_reuse=False, profile="page", stream_every=3,
                                        max_tokens=_page_batch_max_tokens([dict(item)]),   # _TX_GEN_MAX(64) would truncate
                                        custom=custom_prompt,
                                        runtime=(model_runtime._aux_runtime() if use_aux else None),
                                    )
                                    if want_partial:
                                        partial_task = asyncio.create_task(_single_partial_pump())
                                    try:
                                        async with dom_lock:
                                            out = await loop.run_in_executor(dom_pool, page_tx)
                                    finally:
                                        if partial_task is not None:
                                            partial_q.put_nowait(_PARTIAL_DONE)
                                            await asyncio.gather(partial_task, return_exceptions=True)
                                    sent_ids.add(item["id"])
                                    await _send_seg(item["id"], source, out)
                                except Exception as e:
                                    print(f"[dom-tx err] {e}", flush=True)
                                    await send_json({
                                        "type": "dom_translate_err", "request_id": request_id,
                                        "item_id": item["id"], "source": source, "text": str(e)[:240],
                                    })
                        if not deferred:
                            for item in longs:                       # long paragraphs: sentence-chunked, context-preserving
                                if item["id"] in sent_ids:
                                    continue
                                if not await wait_aux_translation_slot(1200):
                                    await send_json({"type": "dom_translate_busy", "request_id": request_id, "retry_ms": 1800})
                                    print("[dom-tx defer] live caption backlog has priority", flush=True)
                                    break
                                source = item["text"]
                                lp_q = asyncio.Queue()
                                _LP_DONE = object()

                                def _lp_progress(cum, _q=lp_q):                 # called from the model thread per chunk
                                    if partial_requested:
                                        loop.call_soon_threadsafe(_q.put_nowait, cum)

                                async def _lp_pump(_q=lp_q, _done=_LP_DONE, _id=item["id"], _src=source):
                                    last = ""
                                    while True:                                  # stream the cumulative paragraph as it grows
                                        cum = await _q.get()
                                        if cum is _done:
                                            break
                                        if _id in sent_ids or not cum or cum == last:
                                            continue
                                        last = cum
                                        await _send_partial(_id, _src, cum)
                                try:
                                    page_long = functools.partial(
                                        translate_page_long_once, source, list(dom_recent_pairs),
                                        target=target_lang, hint=page_hint, register=page_register,
                                        glossary_pairs=list(page_glossary),
                                        on_progress=(_lp_progress if partial_requested else None),
                                        custom=custom_prompt,
                                    )
                                    lp_task = asyncio.create_task(_lp_pump()) if partial_requested else None
                                    t0 = time.perf_counter()
                                    try:
                                        async with mlx_lock:
                                            out = await loop.run_in_executor(model_runtime._mlx_pool, page_long)
                                    finally:
                                        if lp_task is not None:
                                            lp_q.put_nowait(_LP_DONE)
                                            await asyncio.gather(lp_task, return_exceptions=True)
                                    sent_ids.add(item["id"])
                                    await _send_seg(item["id"], source, out, "main")   # long paragraphs always render on the main model
                                    print(f"[dom-tx long ok] request={request_id} chars={len(source)} ms={int((time.perf_counter()-t0)*1000)}", flush=True)
                                except Exception as e:
                                    print(f"[dom-tx long err] {e}", flush=True)
                                    await send_json({
                                        "type": "dom_translate_err", "request_id": request_id,
                                        "item_id": item["id"], "source": source, "text": str(e)[:240],
                                    })
                        await send_json({"type": "dom_translate_done", "request_id": request_id, "count": sent[0]})
                    elif d.get("type") == "input_translate":
                        # Write-back: translate the user's DRAFT (their language) into the page's language for
                        # posting. One-shot + user-initiated + published-by-the-user, so it always renders on
                        # the MAIN model (quality layer) — it yields briefly to caption backlog, then proceeds.
                        request_id = str(d.get("request_id", ""))[:100]
                        wb_text = str(d.get("text", ""))[:4000].strip()
                        wb_target = _normalize_target_lang(d.get("target_lang"), "English")
                        if not request_id:
                            continue
                        if not wb_text:
                            await send_json({"type": "input_translate_err", "request_id": request_id, "text": "empty draft"})
                            continue
                        await wait_aux_translation_slot(1500)   # best-effort yield; user is waiting -> proceed regardless
                        t0 = time.perf_counter()
                        try:
                            write_tx = functools.partial(
                                translate_once, wb_text, [], target=wb_target, hint="", register="chat",
                                glossary_pairs=effective_glossary(), kv_reuse=False, profile="write",
                                max_tokens=min(2048, max(96, len(wb_text))), custom="")
                            async with mlx_lock:
                                wb_out = await loop.run_in_executor(model_runtime._mlx_pool, write_tx)
                            await send_json({"type": "input_translate_result", "request_id": request_id,
                                             "source": wb_text, "text": wb_out})
                            print(f"[input-tx {time.perf_counter()-t0:.1f}s] {len(wb_text)}c -> {wb_target}", flush=True)
                        except Exception as e:
                            print(f"[input-tx err] {e}", flush=True)
                            await send_json({"type": "input_translate_err", "request_id": request_id, "text": str(e)[:240]})
                    elif d.get("type") == "ocr_translate":
                        # Image OCR translation: the extension ships a cropped JPEG of a hovered image;
                        # Apple Vision (ANE, CPU pool) reads the lines and they ride the page-translation
                        # path (aux when resident, else main with a brief caption yield). macOS only.
                        request_id = str(d.get("request_id", ""))[:100]
                        img_b64 = str(d.get("image_b64", ""))
                        if not request_id:
                            continue
                        if not img_b64 or len(img_b64) > 240_000:
                            await send_json({"type": "ocr_translate_err", "request_id": request_id,
                                             "text": "이미지가 없거나 너무 큽니다"})
                            continue
                        t0 = time.perf_counter()
                        try:
                            ocr_img = base64.b64decode(img_b64)

                            def _ocr(_img=ocr_img):
                                import ocr_mac
                                # group Vision's per-line fragments into reading blocks: fewer marker
                                # segments (the small model keeps the format) + sentence-level context
                                return ocr_mac.group_lines(ocr_mac.recognize(_img))

                            ocr_lines = await loop.run_in_executor(model_runtime._sherpa_pool, _ocr)
                            ocr_lines = [l for l in ocr_lines if re.search(r"[^\W\d_]", l["text"])]
                        except Exception as e:
                            print(f"[ocr err] {e}", flush=True)
                            await send_json({"type": "ocr_translate_err", "request_id": request_id, "text": str(e)[:240]})
                            continue
                        if not ocr_lines:
                            await send_json({"type": "ocr_translate_result", "request_id": request_id, "blocks": []})
                            continue
                        ocr_glossary = effective_page_glossary()
                        ocr_hint = page_context_hint or context_hint
                        # OCR renders on the MAIN translator: the overlay is one-shot (no idle re-check can
                        # patch it later like page DOM), so quality wins over hover latency here.
                        use_aux_ocr = False
                        await wait_aux_translation_slot(1500)   # user-initiated: yield briefly, then proceed
                        ocr_lock = mlx_lock
                        ocr_pool = model_runtime._mlx_pool
                        try:
                            ocr_out = {}
                            for chunk_at in range(0, len(ocr_lines), DOM_TX_MAX_ITEMS):   # marker batches stay small
                                chunk = ocr_lines[chunk_at:chunk_at + DOM_TX_MAX_ITEMS]
                                items = [{"id": str(chunk_at + i + 1), "text": l["text"]} for i, l in enumerate(chunk)]
                                ocr_tx = functools.partial(
                                    translate_page_batch_once, items, [],
                                    target=target_lang, hint=ocr_hint, register=page_register,
                                    glossary_pairs=list(ocr_glossary), custom=custom_prompt,
                                    runtime=(model_runtime._aux_runtime() if use_aux_ocr else None))
                                try:
                                    async with ocr_lock:
                                        ocr_out.update(await loop.run_in_executor(ocr_pool, ocr_tx))
                                except Exception as e:
                                    # broken markers etc. — same recovery as the DOM path: per item
                                    print(f"[ocr batch fallback] {e}", flush=True)
                                    for it in items:
                                        try:
                                            ocr_tx1 = functools.partial(
                                                translate_once, it["text"], [],
                                                target=target_lang, hint=ocr_hint, register=page_register,
                                                glossary_pairs=list(ocr_glossary), kv_reuse=False,
                                                profile="page", max_tokens=_page_batch_max_tokens([dict(it)]),
                                                custom=custom_prompt,
                                                runtime=(model_runtime._aux_runtime() if use_aux_ocr else None))
                                            async with ocr_lock:
                                                ocr_out[it["id"]] = await loop.run_in_executor(ocr_pool, ocr_tx1)
                                        except Exception as e1:
                                            print(f"[ocr item err] {e1}", flush=True)
                            blocks = []
                            for i, l in enumerate(ocr_lines):
                                tgt = _clean(str(ocr_out.get(str(i + 1), "")))
                                blocks.append({"box": l["box"], "source": l["text"], "target": tgt or l["text"],
                                               "line_h": l.get("line_h", l["box"][3])})
                            await send_json({"type": "ocr_translate_result", "request_id": request_id, "blocks": blocks})
                            print(f"[ocr {time.perf_counter()-t0:.1f}s] lines={len(blocks)} engine={'aux' if use_aux_ocr else 'main'}", flush=True)
                        except Exception as e:
                            print(f"[ocr tx err] {e}", flush=True)
                            await send_json({"type": "ocr_translate_err", "request_id": request_id, "text": str(e)[:240]})
                    elif d.get("type") == "warm":          # on-demand model warm-up (popup button)
                        t0 = time.perf_counter()
                        if not await wait_aux_translation_slot(1200):
                            await send_json({"type": "warmed", "sec": 0, "deferred": True})
                            print("[warm defer] live caption backlog has priority", flush=True)
                            continue
                        await _on_asr_pool(asr_engine, warm_mlx_selected, True, False, asr_engine)
                        async with mlx_lock:
                            await loop.run_in_executor(model_runtime._mlx_pool, warm_mlx_selected, False, True, asr_engine)
                        sec = round(time.perf_counter() - t0, 1)
                        await send_json({"type": "warmed", "sec": sec})
                        print(f"[warm] {sec}s", flush=True)
                except Exception as e:
                    print(f"[bridge] bad control msg: {e}", flush=True)
                continue
            if not authed:
                await ws.close(code=1008, reason="hello required")
                break
            if len(msg) > MAX_AUDIO_FRAME_BYTES:
                await ws.close(code=1009, reason="audio frame too large")
                break
            data = leftover + msg
            nwin = len(data) // WINDOW_BYTES
            for i in range(nwin):
                win_start_ms = audio_ms
                win_end_ms = audio_ms + WINDOW_MS
                wb = data[i * WINDOW_BYTES:(i + 1) * WINDOW_BYTES]
                wf = np.frombuffer(wb, dtype=np.int16).astype(np.float32) / 32768.0
                ev = vad(wf)
                if in_speech:
                    voiced.extend(wb)
                    if LA_ON:                                       # LocalAgreement: periodic partial transcribe of the growing clause
                        la_count += 1
                        if la_count >= LA_STEP_WINDOWS and len(voiced) >= int(MIN_SEC * SR) * 2:
                            la_count = 0
                            await enqueue_work(("partial", bytes(voiced), speech_start_ms, win_end_ms, False, speech_epoch))
                    if len(voiced) >= soft_max_sec() * SR * 2:        # soft-cut a pauseless monologue
                        await enqueue_work(("clause", bytes(voiced), speech_start_ms, win_end_ms, True))
                        keep = min(len(voiced), int(SOFT_OVERLAP_MS * SR / 1000) * 2)
                        overlap = bytes(voiced[-keep:]) if keep else b""
                        voiced = bytearray(overlap)
                        speech_start_ms = max(0, win_end_ms - int(1000 * len(overlap) / (SR * 2)))
                    if len(voiced) >= HARD_MAX_SEC * SR * 2:
                        # keep a small tail overlap so a word straddling the hard cut isn't lost; soft=True
                        # so the accuracy-mode 2-pass skips this unit (the overlap would double-transcribe).
                        await enqueue_work(("clause", bytes(voiced), speech_start_ms, win_end_ms, True))
                        keep = min(len(voiced), int(SOFT_OVERLAP_MS * SR / 1000) * 2)
                        overlap = bytes(voiced[-keep:]) if keep else b""
                        voiced = bytearray(overlap)
                        speech_start_ms = max(0, win_end_ms - int(1000 * len(overlap) / (SR * 2)))
                else:
                    preroll.append(wb)
                    sil_windows += 1
                    if sil_windows == sent_sil_windows:        # long pause -> sentence boundary (fires once)
                        await enqueue_work(("flush", None, None, win_end_ms, False))
                if ev:
                    if "start" in ev:
                        in_speech, sil_windows = True, 0
                        speech_epoch += 1                          # new utterance epoch (guards stale partials)
                        la_prev, la_stable, la_count = [], [], 0   # new utterance -> reset LocalAgreement
                        speech_start_ms = max(0, win_start_ms - len(preroll) * WINDOW_MS)
                        voiced = bytearray(b"".join(preroll)); preroll.clear()
                    elif "end" in ev:
                        in_speech, sil_windows = False, 0
                        await enqueue_work(("clause", bytes(voiced), speech_start_ms, win_end_ms, False)); voiced = bytearray()
                audio_ms = win_end_ms
            leftover = data[nwin * WINDOW_BYTES:]

    finally:
        try:
            if in_speech and voiced:
                await enqueue_work(("clause", bytes(voiced), speech_start_ms, audio_ms, False))
            await enqueue_work(("eos", None, None, audio_ms, False))       # finalize trailing sentence
            await work_q.put(None)                # stop the inference loop
            await inf_task
        finally:
            if preview_task and not preview_task.done():
                preview_task.cancel()
                await asyncio.gather(preview_task, return_exceptions=True)
            trans_seq += 1
            await trans_q.put((99, trans_seq, None))
            await asyncio.gather(trans_task, return_exceptions=True)
            for t in list(aux_tasks):
                t.cancel()
            await asyncio.gather(*aux_tasks, return_exceptions=True)
            el = time.perf_counter() - t_conn
            rate = (100 * nospeech_count / seg_count) if seg_count else 0
            print(f"[bridge] client disconnected — {seg_count} segs, {nospeech_count} no-speech "
                  f"({rate:.0f}%), preview_drops={preview_drop_count}, cache_hits={cache_hit_count}, {el:.0f}s", flush=True)
            if _active_ws is ws:        # release single-active registration (best-effort; only drives the WARN above)
                _active_ws = None


def _port_in_use(host: str, port: int) -> bool:
    """True if something already accepts on host:port — i.e. another bridge is running. Probed BEFORE
    load_models so a duplicate launch exits without loading models (the 2-bridge smell: two 26B copies
    share the GPU/RAM + port -> degraded latency + flickering captions)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


async def main():
    # ping_interval=None: heavy inference can starve the loop and trip keepalive -> drop. Disable it.
    async with websockets.serve(handle, HOST, PORT, max_size=MAX_WS_MSG_BYTES, ping_interval=None):
        print(f"[bridge] ready  ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()


def _is_loopback_host(host: str) -> bool:
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1")


if __name__ == "__main__":
    # Fail-close: binding a non-loopback host (e.g. 0.0.0.0) with the built-in token would let anyone on the
    # LAN who read the public source stream the user's tab audio / read transcripts. Require an explicit
    # acknowledgement (a private LCC_WS_TOKEN, or LCC_ALLOW_INSECURE_BIND=1) before exposing beyond localhost.
    if not _is_loopback_host(HOST) and WS_TOKEN == _DEFAULT_WS_TOKEN \
            and os.environ.get("LCC_ALLOW_INSECURE_BIND", "").strip() not in ("1", "true", "yes"):
        print(f"[bridge] refusing to bind non-loopback host {HOST!r} with the built-in default token — anyone "
              f"on your LAN could stream this tab's audio. Bind 127.0.0.1 (default), or set a private "
              f"LCC_WS_TOKEN, or LCC_ALLOW_INSECURE_BIND=1 to accept the risk. Exiting.", flush=True)
        raise SystemExit(2)
    if _port_in_use(HOST, PORT):
        print(f"[bridge] {HOST}:{PORT} already in use — another bridge is running; refusing to start a "
              f"second (they would share the MLX device → slow + flickering). Exiting.", flush=True)
        raise SystemExit(1)
    load_models(asr=True, lm=True, vad=True)
    if model_runtime.BACKEND == "cuda":
        print(f"[bridge] ready (CUDA HTTP backend + 26B translate, latency={LATENCY_MODE_DEFAULT})", flush=True)
    else:
        asr_label = {"parakeet": "Parakeet ASR"}.get(model_runtime.ASR_ENGINE, "MLX ASR")
        print(f"[bridge] ready ({asr_label} + 26B translate, latency={LATENCY_MODE_DEFAULT})", flush=True)
        try:
            import mlx_lm as _mlxlm
            print(f"[bridge] mlx_lm={getattr(_mlxlm, '__version__', '?')} (KV reuse window learned lazily)", flush=True)
        except Exception:
            pass
    try:                                              # warm on the main thread (MLX: establishes streams + compiles; CUDA: pings endpoints)
        warm_mlx_selected(True, True)
        if model_runtime.BACKEND == "mlx":
            _reset_tx_cache()                         # real translation rebuilds it on the model_runtime._mlx_pool worker thread
            _reset_page_tx_cache()
            if model_runtime.mx is not None:
                try: model_runtime.mx.clear_cache()
                except Exception: pass
        print("[bridge] warmed", flush=True)
    except Exception as e:
        if model_runtime.BACKEND == "mlx":
            _reset_tx_cache()
            _reset_page_tx_cache()
        print("[bridge] warm skip:", e, flush=True)
    asyncio.run(main())
