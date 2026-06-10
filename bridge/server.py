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
mlx on a single dedicated worker thread (_mlx_pool) — inline-in-loop hangs; set_default_device(gpu)
restores the stream. (asyncio.to_thread's default pool could hop threads between calls.)
"""
import asyncio, json, collections, difflib, functools, os, re, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import numpy as np
import websockets
from silero_vad import load_silero_vad, VADIterator
from backend_parakeet import ParakeetAsr

# ASR engine taxonomy (single source of truth). Two families:
#   - "sherpa": Parakeet — runs on sherpa-onnx on a dedicated CPU pool, OFF the MLX device, so it never
#     contends with 26B translation (true ASR∥translate parallelism) and has its own latency/warmup wiring.
#   - "granite"/"qwen3": mlx-audio audio-LLM ASR — in-process on the Apple GPU, so they serialize against
#     26B translation (share _mlx_pool + mlx_lock). Native punctuation/truecasing + multilingual + auto langID.
# _is_sherpa_engine()/_is_mlxa_engine() — NOT scattered name tuples — are THE switch between families.
_SHERPA_ENGINES = ("parakeet",)
_MLXA_ENGINES = ("granite", "qwen3")
# Whisper (large-v3): a dedicated ASR (own decode, no prompt, returns segments) — NOT an audio-LLM,
# so it gets its own family + loader (mlx_whisper on Mac / whisper.cpp q6 on CUDA), mirroring how
# sherpa/parakeet is its own family. The _is_*_engine() helpers stay the single switch (no scattered tuples).
_WHISPER_ENGINES = ("whisper",)
_ASR_ENGINES = _MLXA_ENGINES + _SHERPA_ENGINES + _WHISPER_ENGINES

def _is_sherpa_engine(engine) -> bool:
    return engine in _SHERPA_ENGINES

def _is_mlxa_engine(engine) -> bool:
    return engine in _MLXA_ENGINES

def _is_whisper_engine(engine) -> bool:
    return engine in _WHISPER_ENGINES

def _normalize_asr_engine(value, default="granite"):
    engine = str(value or default or "granite").strip().lower()
    return engine if engine in _ASR_ENGINES else default


# Compute backend (platform): "mlx" = in-process Apple-Silicon MLX (default), "cuda" = OpenAI-compatible
# HTTP client to a remote llama.cpp/vLLM (Windows+NVIDIA via WSL2, or any reachable GPU box). Selected with
# LCC_BACKEND. Only the three GPU leaves (transcribe/translate/ask) differ; everything else is shared. See
# the "Backend seam" block lower in this file.
_BACKENDS = ("mlx", "cuda")

def _normalize_backend(value, default="mlx"):
    b = str(value or default or "mlx").strip().lower()
    b = {"nvidia": "cuda", "gpu": "cuda", "http": "cuda", "apple": "mlx", "metal": "mlx"}.get(b, b)
    return b if b in _BACKENDS else default


def _normalize_latency_mode(value, default="aggressive"):
    mode = str(value or default or "aggressive").strip().lower()
    aliases = {
        "fast": "aggressive",
        "low": "aggressive",
        "low-latency": "aggressive",
        "low_latency": "aggressive",
        "safe": "stable",
        "quality": "stable",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in ("stable", "balanced", "aggressive") else default


# Gemma 4 is broadly multilingual (140+ langs); expose a generous set of widely-used targets. Any name here is
# inserted into the prompt as "into {target}" — few-shot/register anchors exist only for a few (graceful: others
# translate fine without anchors). Keep in sync with the popup targetLang <select>.
_TARGET_LANGS = {
    "Korean", "English", "Japanese", "Chinese", "Spanish", "French", "German", "Portuguese", "Italian",
    "Russian", "Dutch", "Polish", "Turkish", "Vietnamese", "Thai", "Indonesian", "Arabic", "Hindi",
    "Bengali", "Ukrainian", "Czech", "Greek", "Hebrew", "Romanian", "Hungarian", "Swedish", "Danish",
    "Norwegian", "Finnish", "Filipino", "Malay", "Tamil", "Telugu", "Urdu", "Persian", "Swahili",
    "Catalan", "Croatian", "Slovak", "Bulgarian", "Serbian", "Lithuanian", "Slovenian", "Estonian", "Latvian",
}

def _normalize_target_lang(value, default="Korean"):
    raw = str(value or default or "Korean").strip()
    for lang in _TARGET_LANGS:
        if raw.lower() == lang.lower():
            return lang
    return default if default in _TARGET_LANGS else "Korean"

def _translation_context_signature(target, register, hint, glossary_pairs, custom=""):
    # custom is part of the signature (INV-9): change the custom prompt -> cache/epoch invalidates, so a
    # prompt edit never leaves stale translations rendering. Trailing default keeps it backward-compatible.
    return (
        _normalize_target_lang(target),
        str(register or "casual"),
        str(hint or ""),
        tuple(glossary_pairs or ()),
        str(custom or ""),
    )

def _clamp_int(value, default, lo, hi):
    try:
        n = int(value)
    except Exception:
        n = int(default)
    return max(lo, min(hi, n))

def _clamp_float(value, default, lo, hi):
    try:
        n = float(value)
    except Exception:
        n = float(default)
    return max(lo, min(hi, n))

def _config_bool(value, default=False):
    # Strict: a JSON string "false"/"0"/"off" must read False — bool("false") is True, so a malformed or
    # hostile config message could otherwise flip a flag ON. The real extension sends actual booleans.
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off", ""):
        return False
    return default


DOM_TX_MAX_ITEMS = int(os.environ.get("LCC_DOM_TX_MAX_ITEMS", "8"))
DOM_TX_MAX_CHARS = int(os.environ.get("LCC_DOM_TX_MAX_CHARS", "32000"))       # per-item sanity bound; long paragraphs are sentence-chunked (translate_page_long_once) + streamed, not truncated in practice
DOM_TX_MAX_TOTAL_CHARS = int(os.environ.get("LCC_DOM_TX_MAX_TOTAL_CHARS", "36000"))   # whole-batch ceiling (one long item + short ones)
PAGE_LONG_CHARS = max(200, int(os.environ.get("LCC_PAGE_LONG_CHARS", "600")))   # items longer than this take the sentence-chunked, context-preserving path
PAGE_CHUNK_CHARS = max(200, int(os.environ.get("LCC_PAGE_CHUNK_CHARS", "500"))) # target size per chunk in that path
PAGE_TX_BATCH_MIN_TOKENS = max(64, int(os.environ.get("LCC_PAGE_TX_BATCH_MIN_TOKENS", "128")))
PAGE_TX_BATCH_MAX_TOKENS = max(PAGE_TX_BATCH_MIN_TOKENS, int(os.environ.get("LCC_PAGE_TX_BATCH_MAX_TOKENS", "1536")))
PAGE_BLOCK_CONTEXT = os.environ.get("LCC_PAGE_BLOCK_CONTEXT", "1") != "0"   # use the client's surrounding-block text as reference context
PAGE_BLOCK_CTX_MAX = max(80, int(os.environ.get("LCC_PAGE_BLOCK_CTX_MAX", "600")))         # per-block context cap (chars)
PAGE_BLOCK_CTX_TOTAL = max(200, int(os.environ.get("LCC_PAGE_BLOCK_CTX_TOTAL", "1200")))   # total context cap per batch (chars)


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


MLX_IMPORT_ERROR = None
try:
    import mlx.core as mx
    from mlx_lm import load as lm_load, stream_generate as lm_stream
    from mlx_lm.sample_utils import make_sampler
    from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache, can_trim_prompt_cache
except Exception as e:
    MLX_IMPORT_ERROR = e
    mx = None
    lm_load = lm_stream = make_sampler = None
    make_prompt_cache = trim_prompt_cache = can_trim_prompt_cache = None
try:
    from mlx_vlm import load as vlm_load, generate as vlm_generate   # Gemma-4 nano (mid/lite) translator path
except Exception:
    vlm_load = vlm_generate = None

BACKEND = _normalize_backend(os.environ.get("LCC_BACKEND"), "mlx")   # mlx (Apple) | cuda (HTTP to llama.cpp/vLLM)

# --- Translation-model selection: size the translator to AVAILABLE memory, not total ----------------------------
# The translator weights dominate the footprint, so picking by *total* RAM is blind to what's already resident and
# has to assume the worst (over-conservative). Instead size to *free* memory (idle VRAM): on CUDA that's nvidia-smi
# free VRAM; on MLX (unified memory) it's min(Metal working-set budget − active, OS-available). The largest curated
# model that fits with HEADROOM to spare wins — so a busy 32GB box correctly steps down to avoid swap, while an idle
# 24GB box can still run the 26B. Precedence:
#   LCC_LM_MODEL (explicit id)  >  "Auto" = memory-fit over the curated registry (_auto_lm_model).
# Resolution is LAZY (done in load_models/_ensure_asr_loaded, NOT at import) so `import server` in tests never
# probes hardware or prints. Effective on MLX (selects LM_MODEL); on CUDA the GGUF is chosen by cuda/serve_llama.sh
# and the tier here only labels the OpenAI 'model' field + logs which GGUF tier to serve.
# Curated model registry — SINGLE SOURCE OF TRUTH for both roles. install_models.py, the native host
# (models_status), and the popup all read these (no model id duplicated). The old full/mid/lite TIER
# vocabulary is gone: a translator is chosen by id/repo (explicit LCC_LM_MODEL) or by "Auto" = the
# largest whose footprint+headroom fits free memory. nano (e4b/e2b) load via the mlx_vlm auto-fallback
# in load_models() — they are fully loadable, just need mlx_vlm installed. (Gemma 4 / Whisper = OSS.)
LM_MODELS = {   # translation. mlx: repo loaded in-process. cuda: 'repo' = HF GGUF source, 'served' = OpenAI model label.
    "mlx": [
        {"id": "gemma-26b", "label": "Gemma 26B", "repo": "mlx-community/gemma-4-26b-a4b-it-4bit",
         "footprint_gb": _clamp_float(os.environ.get("LCC_LM_NEED_FULL"), 18.0, 4.0, 512.0)},
        {"id": "gemma-e4b", "label": "Gemma E4B",
         "repo": os.environ.get("LCC_LM_MID_MLX", "mlx-community/gemma-4-e4b-it-4bit"),
         "footprint_gb": _clamp_float(os.environ.get("LCC_LM_NEED_MID"), 8.0, 2.0, 512.0)},
        {"id": "gemma-e2b", "label": "Gemma E2B",
         "repo": os.environ.get("LCC_LM_LITE_MLX", "mlx-community/gemma-4-e2b-it-4bit"),
         "footprint_gb": _clamp_float(os.environ.get("LCC_LM_NEED_LITE"), 6.0, 1.0, 512.0)},
    ],
    "cuda": [
        {"id": "gemma-26b", "label": "Gemma 26B", "repo": "google/gemma-4-26B-A4B-it-qat-q4_0-gguf",
         "served": os.environ.get("LCC_LM_FULL_CUDA", "gemma-4-26b-a4b-it-qat-q4_0"),
         "footprint_gb": _clamp_float(os.environ.get("LCC_LM_NEED_FULL"), 18.0, 4.0, 512.0)},
        {"id": "gemma-e4b", "label": "Gemma E4B", "repo": "google/gemma-4-E4B-it-qat-q4_0-gguf",
         "served": os.environ.get("LCC_LM_MID_CUDA", "gemma-4-e4b-it-qat-q4_0"),
         "footprint_gb": _clamp_float(os.environ.get("LCC_LM_NEED_MID"), 8.0, 2.0, 512.0)},
        {"id": "gemma-e2b", "label": "Gemma E2B", "repo": "google/gemma-4-E2B-it-qat-q4_0-gguf",
         "served": os.environ.get("LCC_LM_LITE_CUDA", "gemma-4-e2b-it-qat-q4_0"),
         "footprint_gb": _clamp_float(os.environ.get("LCC_LM_NEED_LITE"), 6.0, 1.0, 512.0)},
    ],
}
# Curated transcription models. engine = loader family (granite/qwen3 audio-LLM, whisper, parakeet); one
# engine may have variants (qwen3 1.7B/0.6B) split by repo — selection carries (engine, repo). needs_quant
# tells install_models which quant target to produce when no prequant exists (whisper only).
ASR_MODELS = {
    "mlx": [
        {"id": "granite", "label": "Granite", "engine": "granite", "repo": "ibm-granite/granite-speech-4.1-2b"},
        {"id": "qwen3-1.7b", "label": "Qwen3-ASR 1.7B", "engine": "qwen3", "repo": "Qwen/Qwen3-ASR-1.7B"},
        {"id": "qwen3-0.6b", "label": "Qwen3-ASR 0.6B", "engine": "qwen3", "repo": "Qwen/Qwen3-ASR-0.6B"},
        {"id": "whisper-large-v3", "label": "Whisper Large v3", "engine": "whisper",
         "repo": os.environ.get("LCC_WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx"), "needs_quant": "mlx-6bit"},
    ],
    "cuda": [
        {"id": "granite", "label": "Granite", "engine": "granite", "repo": "ibm-granite/granite-speech-4.1-2b"},
        {"id": "qwen3-1.7b", "label": "Qwen3-ASR 1.7B", "engine": "qwen3", "repo": "Qwen/Qwen3-ASR-1.7B"},
        {"id": "whisper-large-v3", "label": "Whisper Large v3", "engine": "whisper",
         "repo": os.environ.get("LCC_WHISPER_GGUF_REPO", "ggerganov/whisper.cpp"), "needs_quant": "gguf-q6"},
    ],
}
# HEADROOM = OS/browser slack kept free so a growing browser doesn't push the resident model into swap.
_LM_HEADROOM_GB = _clamp_float(os.environ.get("LCC_LM_HEADROOM_GB"), 4.0, 0.0, 64.0)
_ASR_QWEN3_DEFAULT = "Qwen/Qwen3-ASR-1.7B"   # qwen3 default when unset (tier-based ASR shrink is gone)
WHISPER_REPO = os.environ.get("LCC_WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx")

def lm_models(backend=None):
    """Curated translation models for the backend (single source; popup/install/host read this)."""
    return LM_MODELS.get(_normalize_backend(backend or BACKEND), LM_MODELS["mlx"])

def asr_models(backend=None):
    """Curated transcription models for the backend (single source)."""
    return ASR_MODELS.get(_normalize_backend(backend or BACKEND), ASR_MODELS["mlx"])

def _system_available_gb():
    """OS memory available without swapping (purgeable-inclusive). psutil if present, else vm_stat (macOS)."""
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        pass
    try:   # macOS no-dep fallback: (free + inactive + purgeable + speculative) pages * page size
        import subprocess
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=4).stdout
        page, m = 4096, re.search(r"page size of (\d+) bytes", out)
        if m:
            page = int(m.group(1))
        tot = 0
        for label in ("Pages free", "Pages inactive", "Pages purgeable", "Pages speculative"):
            mm = re.search(rf"{re.escape(label)}:\s+(\d+)\.", out)
            if mm:
                tot += int(mm.group(1))
        return tot * page / 1e9 if tot else None
    except Exception:
        return None

def _free_mem_gb_cuda():
    """Free dedicated VRAM (GB) via nvidia-smi; min across GPUs (we pin one). None if unavailable."""
    import shutil, subprocess
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=4).stdout
        vals = [float(x.strip()) for x in out.splitlines() if x.strip()]
        return min(vals) / 1024.0 if vals else None   # MiB -> GiB
    except Exception:
        return None

def _mlx_device_info():
    try:
        return mx.device_info()            # mlx >= 0.30
    except Exception:
        return mx.metal.device_info()      # older mlx (deprecated path)

def _mlx_active_memory():
    try:
        return mx.get_active_memory()
    except Exception:
        return mx.metal.get_active_memory()

def _free_mem_gb_mlx():
    """Idle unified memory (GB) on Apple Silicon: min(Metal working-set budget − active MLX, OS-available)."""
    budget_free = None
    try:
        info = _mlx_device_info()
        budget = float(info.get("max_recommended_working_set_size", 0))
        if budget > 0:
            budget_free = (budget - float(_mlx_active_memory())) / 1e9
    except Exception:
        pass
    cands = [v for v in (budget_free, _system_available_gb()) if v is not None]
    return min(cands) if cands else None

def _auto_lm_model():
    """Memory-fit auto: the largest curated translator whose footprint + headroom fits free memory; else
    the smallest; else the largest when memory is unprobable. Returns a model dict from the registry.
    Replaces the old tier auto-select (chooses a model, not a tier) — this is what keeps 'Auto' working
    after the tier vocabulary was removed."""
    models = lm_models()
    avail = _free_mem_gb_cuda() if BACKEND == "cuda" else _free_mem_gb_mlx()
    if avail is None:
        print("[bridge] model: free-memory probe failed -> largest (set LCC_LM_MODEL to pin)", flush=True)
        return models[0]
    for m in models:                                     # registry is largest-first
        if avail >= m["footprint_gb"] + _LM_HEADROOM_GB:
            print(f"[bridge] model={m['id']}  (avail≈{avail:.1f}GB ≥ {m['footprint_gb']:.0f}+{_LM_HEADROOM_GB:.0f}GB "
                  f"headroom; pin with LCC_LM_MODEL)", flush=True)
            return m
    print(f"[bridge] model={models[-1]['id']}  (avail≈{avail:.1f}GB below all thresholds)", flush=True)
    return models[-1]

# Lazy-resolved config (empty at import; filled by _finalize_model_config at first warm — see note above).
LM_MODEL = os.environ.get("LCC_LM_MODEL", "")               # "" -> memory-fit auto over the registry
_LM_RESOLVED = False
_LM_IS_VLM = False   # set True at load when the translator is a Gemma-4 nano (multimodal) loaded via mlx_vlm

# --- Aux translator (dual-model concurrency) ------------------------------------------------------------
# A second, SMALL resident translator (E2B) that takes the latency-tolerant-quality / latency-critical-UX
# work off the 26B: caption previews and page-DOM microbatches run on their own pool + device lock and
# OVERLAP the 26B exactly like ASR does (26B decode is bandwidth-bound; a small model slots into the
# compute gap). Finals, long page paragraphs, verify passes, and ask/summary stay on the main model —
# aux is the speed layer, main is the quality layer. MLX only (CUDA serves one GGUF); auto-enabled when
# the main pick is the 26B and free memory still fits the smallest curated model + headroom.
# LCC_AUX_LM: "auto" (default) | "off"/"0" | explicit registry id / HF repo.
AUX_LM = os.environ.get("LCC_AUX_LM", "auto")
AUX_LM_HEADROOM_GB = _clamp_float(os.environ.get("LCC_AUX_LM_HEADROOM_GB"), 2.0, 0.0, 64.0)
aux_lm_model = aux_lm_tok = None
_AUX_LM_IS_VLM = False


def _aux_lm_choice(main_value, avail_gb, setting=None):
    """Resolve the aux translator to load (repo/served string) or None. Pure given inputs; tested in
    test_aux_lm.py. Auto pairs the SMALLEST curated translator under the LARGEST one only — when the
    main pick already had to shrink, there is no spare memory worth betting on."""
    s = (AUX_LM if setting is None else setting).strip()
    if s.lower() in ("", "0", "off", "no", "none", "false"):
        return None
    models = lm_models()
    if s.lower() != "auto":
        choice = _resolve_lm_model(s)                       # explicit id/repo: the user owns the RAM math
        return None if choice == main_value else choice
    if main_value != _lm_select_value(models[0]):
        return None
    small = models[-1]
    if avail_gb is None or avail_gb < small["footprint_gb"] + AUX_LM_HEADROOM_GB:
        return None
    return _lm_select_value(small)


def aux_lm_ready():
    return BACKEND == "mlx" and aux_lm_model is not None


def _aux_runtime():
    return (aux_lm_model, aux_lm_tok, _AUX_LM_IS_VLM)

# mlx-audio audio-LLM ASR engines (granite/qwen3): native punctuation + multilingual, loaded via mlx_audio.
MLXA_REPOS = {
    "granite": os.environ.get("LCC_GRANITE_MODEL", "ibm-granite/granite-speech-4.1-2b"),
    "qwen3":   os.environ.get("LCC_QWEN3_MODEL", ""),       # "" -> 1.7B (full/mid) or 0.6B (lite), set at warm
}

def _lm_select_value(m):
    """The string the runtime loads for a registry entry: the served label on CUDA, else the HF repo."""
    return m["served"] if (BACKEND == "cuda" and "served" in m) else m["repo"]


def _resolve_lm_model(value):
    """Map a curated registry id (e.g. 'gemma-26b') to its repo/served value; pass a raw repo through.
    Lets the popup send a stable id as LCC_LM_MODEL without knowing the backend's repo vs served split."""
    for m in lm_models():
        if value == m["id"]:
            return _lm_select_value(m)
    return value


def _finalize_model_config():
    """Resolve the translator (explicit LCC_LM_MODEL > memory-fit auto) and the ASR repo, once. Lazy
    (called at warm, NOT at import) so tests that `import server` never probe hardware. Idempotent.
    The full/mid/lite tier vocabulary is gone — Auto picks a curated model by footprint (_auto_lm_model)."""
    global LM_MODEL, _LM_RESOLVED
    if _LM_RESOLVED:
        return
    _LM_RESOLVED = True
    if not LM_MODEL:
        LM_MODEL = _lm_select_value(_auto_lm_model())
    else:
        LM_MODEL = _resolve_lm_model(LM_MODEL)
        print(f"[bridge] model={LM_MODEL} (LCC_LM_MODEL)", flush=True)
    if not MLXA_REPOS["qwen3"]:
        MLXA_REPOS["qwen3"] = _ASR_QWEN3_DEFAULT
    print(f"[bridge] translate backend={BACKEND} model={LM_MODEL}", flush=True)
    if BACKEND == "cuda":
        print("[bridge] (cuda) translation GGUF is served by cuda/serve_llama.sh — model label "
              f"{LM_MODEL!r}", flush=True)

# Granite needs an explicit ASR instruction (it also does AST); Qwen3-ASR auto-detects + punctuates with no prompt.
GRANITE_ASR_PROMPT = os.environ.get(
    "LCC_GRANITE_PROMPT", "transcribe the speech with proper punctuation and capitalization.")
ASR_ENGINE = _normalize_asr_engine(os.environ.get("LCC_ASR_ENGINE"), "granite")
PARAKEET_MODEL_DIR = os.environ.get(
    "LCC_PARAKEET_MODEL_DIR",
    os.path.expanduser("~/.local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8"),
)
PARAKEET_THREADS = max(1, int(os.environ.get("LCC_PARAKEET_THREADS", "4")))
PARAKEET_PROVIDER = os.environ.get("LCC_PARAKEET_PROVIDER", "cpu").strip().lower()
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

SR = 16000
WINDOW_SAMPLES = 512                       # Silero VAD window @16k
WINDOW_BYTES = WINDOW_SAMPLES * 2
WINDOW_MS = 1000 * WINDOW_SAMPLES // SR     # 32ms
SEG_SILENCE_MS = 250       # silence that ends one utterance (VADIterator min_silence) — lower = ASR starts sooner
SENT_SILENCE_MS = 1000     # clause/sentence boundary -> translate. KO reverses word order, so kept higher than
                           #  GPT's suggested 750 — committing mid-clause would break the translation order.
SPEECH_PAD_MS = 120        # VADIterator pre/post-roll
PREROLL_WINDOWS = SPEECH_PAD_MS // WINDOW_MS + 4
SOFT_MAX_SEC, HARD_MAX_SEC, MIN_SEC = 4.0, 12, 0.4
SOFT_OVERLAP_MS = 220
LA_ON = os.environ.get("LCC_LA", "0") == "1"   # LocalAgreement: stream the confirmed-prefix as live source (off by default)
LA_STEP_WINDOWS = max(1, round(float(os.environ.get("LCC_LA_STEP_MS", "1000")) / WINDOW_MS))
TWO_PASS_MIN_SEC, TWO_PASS_MAX_SEC = 1.2, 14.0   # accuracy mode: re-transcribe the whole sentence's audio once at commit
PENDING_CAP = 120          # chars: force-translate even without a sentence end (low-latency profile)
PENDING_MAX_AGE_MS = 3000
PREVIEW_DEBOUNCE_MS = 450
PREVIEW_MIN_CHARS = 18
PREVIEW_MIN_DELTA = 12
TRANSLATION_CACHE_MAX = 128
VAD_THRESH = {0: 0.3, 1: 0.4, 2: 0.5, 3: 0.65}   # vadLevel -> Silero speech-probability threshold
SENT_END = re.compile(r"[.!?。！？…][\"'»」』）)]?")   # candidate sentence boundary
MIN_SENT_CHARS = 18        # a split shorter than this is likely an abbreviation (Dr./Mr.), not a sentence
WEAK_TAIL_WORDS = {
    "and", "or", "but", "so", "because", "that", "which", "who", "to", "of", "in", "for",
    "with", "as", "at", "from", "by", "if", "when", "while", "than", "then",
    "i", "we", "you", "they", "he", "she", "it", "a", "an", "the",
    "am", "is", "are", "was", "were", "be", "being", "been",
    "will", "would", "can", "could", "should", "may", "might", "must",
    "do", "does", "did", "have", "has", "had",
}


ASR_MAX_TOKENS = max(32, int(os.environ.get("LCC_ASR_MAX_TOKENS", "64")))   # per-segment generation cap (granite/qwen3)

# Models load lazily via load_models() (called from __main__) so the pure prompt-building helpers
# (_tx_system / _fewshot / translate_once) can be imported by benches/tests without pulling ~50GB of
# weights. The bridge loads everything; a translation-only bench can skip ASR/VAD.
lm_model = lm_tok = silero = _sampler = parakeet_asr = None
mlxa_model = None            # mlx-audio ASR model instance (granite/qwen3); one at a time, reloaded on engine switch
mlxa_loaded_engine = None
whisper_loaded_repo = None   # the whisper repo whose model is warm (mlx_whisper caches the model by path)


def _require_mlx():
    if MLX_IMPORT_ERROR is not None:
        raise RuntimeError(
            "MLX backend unavailable. Install the MLX dependencies for the local live-caption backend."
        ) from MLX_IMPORT_ERROR


def _ensure_asr_loaded(engine: str):
    global parakeet_asr, mlxa_model, mlxa_loaded_engine, whisper_loaded_repo
    _finalize_model_config()   # resolve MLXA_REPOS / qwen3 default before the first load
    engine = _normalize_asr_engine(engine, ASR_ENGINE)
    if engine == "parakeet":
        if not PARAKEET_MODEL_DIR:
            raise RuntimeError("LCC_PARAKEET_MODEL_DIR is required when LCC_ASR_ENGINE=parakeet")
        if parakeet_asr is None:
            print(
                f"[bridge] loading Parakeet ASR ({PARAKEET_PROVIDER}, threads={PARAKEET_THREADS}) from {PARAKEET_MODEL_DIR}…",
                flush=True,
            )
            parakeet_asr = ParakeetAsr(
                PARAKEET_MODEL_DIR,
                num_threads=PARAKEET_THREADS,
                provider=PARAKEET_PROVIDER,
            )
        return engine

    if _is_mlxa_engine(engine):
        _require_mlx()
        if mlxa_model is None or mlxa_loaded_engine != engine:
            from mlx_audio.stt.utils import load_model as _mlxa_load
            repo = MLXA_REPOS[engine]
            print(f"[bridge] loading {repo} ({engine} audio ASR)…", flush=True)
            mlxa_model = _mlxa_load(repo)
            mlxa_loaded_engine = engine
        return engine

    if _is_whisper_engine(engine):
        # Whisper (large-v3) — dedicated ASR via mlx_whisper. The 6bit model is produced/fetched by
        # install_models (prequant-first, else local quantize); here we just ensure mlx_whisper is present
        # and record the repo. mlx_whisper.transcribe() lazily loads + caches the model by path, so the
        # first real transcribe warms it. No prompt (own decode + langID) — INV-7.
        _require_mlx()
        repo = WHISPER_REPO
        if whisper_loaded_repo != repo:
            import mlx_whisper  # noqa: F401  — fail fast if the dep is missing (install ensures it)
            print(f"[bridge] using {repo} (whisper ASR)…", flush=True)
            whisper_loaded_repo = repo
        return engine

    raise RuntimeError(f"unknown ASR engine: {engine}")


def _load_lm_weights(value):
    """Load a translator by repo/served value with the Gemma-4 nano (multimodal) mlx_vlm fallback.
    Returns (model, tok, is_vlm)."""
    try:
        model, tok = lm_load(value)
        return model, tok, False
    except Exception as e:
        # Gemma-4 nano (E4B/E2B) ships as a multimodal checkpoint (language_model.* prefix) the mlx_lm
        # text loader can't read — but it loads via mlx_vlm. Auto-fall back so the small tiers work.
        if "not in model" in str(e) and vlm_load is not None:
            print(f"[bridge] {value} is multimodal (Gemma-4 nano) -> loading via mlx_vlm", flush=True)
            model, tok = vlm_load(value)
            return model, tok, True
        raise


def load_models(asr=True, lm=True, vad=True):
    global ASR_ENGINE, lm_model, lm_tok, silero, _sampler, parakeet_asr, _LM_IS_VLM
    global aux_lm_model, aux_lm_tok, _AUX_LM_IS_VLM
    _finalize_model_config()   # size translator/ASR to available memory (lazy: not at import — tests import server)
    if BACKEND == "cuda":
        # Models live on the remote inference servers (llama.cpp / vLLM); the bridge only needs the endpoints
        # up. Best-effort health-check + log; the first real request surfaces any failure. VAD still loads below.
        import backend_cuda
        backend_cuda.load(asr=asr, lm=lm)
    else:
        needs_mlx = lm or (asr and _is_mlxa_engine(ASR_ENGINE))
        if needs_mlx:
            _require_mlx()
        if needs_mlx and _sampler is None:
            _sampler = make_sampler(temp=0.0)
        if asr:
            try:
                _ensure_asr_loaded(ASR_ENGINE)
            except Exception as e:
                if _is_sherpa_engine(ASR_ENGINE):
                    print(f"[bridge] {ASR_ENGINE} ASR unavailable ({e}); falling back to granite ASR", flush=True)
                    ASR_ENGINE = "granite"
                    _ensure_asr_loaded(ASR_ENGINE)
                else:
                    raise
        if lm and lm_model is None:
            print(f"[bridge] loading translator ({LM_MODEL})…", flush=True)
            lm_model, lm_tok, _LM_IS_VLM = _load_lm_weights(LM_MODEL)
        if lm and aux_lm_model is None:
            # Aux pick happens AFTER the main translator is resident so the free-memory probe reflects it.
            choice = _aux_lm_choice(LM_MODEL, _free_mem_gb_mlx())
            if choice:
                try:
                    print(f"[bridge] loading aux translator ({choice})…", flush=True)
                    aux_lm_model, aux_lm_tok, _AUX_LM_IS_VLM = _load_lm_weights(choice)
                    print("[bridge] aux translator ready — previews + page DOM overlap the main model", flush=True)
                except Exception as e:                      # aux is an enhancement; main path must survive
                    print(f"[bridge] aux translator unavailable ({e}); single-model mode", flush=True)
                    aux_lm_model = aux_lm_tok = None
                    _AUX_LM_IS_VLM = False
    if vad and silero is None:
        print("[bridge] loading Silero VAD…", flush=True)
        silero = load_silero_vad(onnx=True)
# One dedicated worker for MLX keeps stream affinity.
# LOAD-BEARING: translate_once mutates the module-global _tx_cache on this pool's thread, so the KV-reuse
# invariant (_tx_cache_ids == cache.offset) is safe ONLY because there is EXACTLY ONE worker (the cache is
# thread-confined). Do not raise this without making the translator KV cache per-thread/per-connection.
_MLX_POOL_WORKERS = 1
_mlx_pool = ThreadPoolExecutor(max_workers=_MLX_POOL_WORKERS, thread_name_prefix="mlx")
# Parakeet's sherpa-onnx CPU pool — off the MLX device so ASR never contends with 26B translation.
# Single worker keeps per-stream decode affinity.
_sherpa_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sherpa")
# mlx-audio ASR (granite/qwen3) gets its OWN MLX worker+lock so it can OVERLAP 26B translation on the single
# GPU: 26B decode is memory-bandwidth bound (compute idles), so a small ASR forward slots into that gap.
# Measured ~1.16x on a concurrent pair, and it removes the per-sentence "translate-then-transcribe" gap so the
# translation pipeline doesn't stall. ASR holds _ASR_DEVICE_LOCK, translation holds _MLX_DEVICE_LOCK — disjoint,
# so they run concurrently (RAM ~25GB peak on a 64GB box). _tx_cache stays confined to _mlx_pool (no race).
_asr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
_ASR_DEVICE_LOCK = asyncio.Lock()
# Aux translator pool/lock — third concurrent MLX user (after main LM and ASR), same overlap rationale.
# Aux calls always use a FRESH per-call prompt cache (runtime path forces kv_reuse off), so nothing here
# can race the main worker's persistent _tx_cache/_page_tx_cache.
_aux_lm_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="auxlm")
_AUX_LM_DEVICE_LOCK = asyncio.Lock()
# Single MLX device: _MLX_DEVICE_LOCK serializes TRANSLATION (+ ask / warm / engine-load) across connections;
# ASR uses its own lock above so the two overlap. A live engine switch restarts the capture, so warm/engine-load
# on this lock won't race a concurrent transcribe on _ASR_DEVICE_LOCK.
_MLX_DEVICE_LOCK = asyncio.Lock()
# Single active capture connection (diagnostic only): one client is the intended model. If a second
# authenticates while one is active we WARN — we do NOT close it: the extension auto-reconnects, so a forced
# close would cause a reconnect war. Correctness across any concurrent clients is held by _MLX_DEVICE_LOCK;
# full non-degradation would need a per-connection translator cache. See docs/caption-lifecycle.md.
_active_ws = None
_TX_KVREUSE = os.environ.get("LCC_TX_KVREUSE", "1") != "0"   # reuse the translator static-prefix KV across calls
_tx_cache = None            # persistent prompt cache for translate_once (single _mlx_pool worker -> no race)
_tx_cache_ids = []          # token ids currently resident in _tx_cache
_PAGE_TX_KVREUSE = os.environ.get("LCC_PAGE_TX_KVREUSE", "1") != "0"   # separate page-DOM prefix KV; never shares caption cache
_page_tx_cache = None       # persistent prompt cache for translate_page_batch_once (same single _mlx_pool worker)
_page_tx_cache_ids = []     # token ids currently resident in _page_tx_cache
_TX_KV_MAX = int(os.environ.get("LCC_TX_KV_MAX_TOKENS", "4096"))   # cap reuse to a bounded prompt window
_TX_KV_WINDOW = None        # min RotatingKVCache sliding window (Gemma 4); reuse must stay inside it (lazy)
_TX_GEN_MAX = max(1, int(os.environ.get("LCC_TX_GEN_MAX_TOKENS", "64")))   # caption translation cap; ask/summary uses its own chat cap
_TX_WINDOW_MARGIN = max(0, int(os.environ.get("LCC_TX_WINDOW_MARGIN", "8")))   # keep reuse a few tokens clear of the window edge
TX_PROFILE = os.environ.get("LCC_TX_PROFILE", "quality").strip().lower()
TX_FEWSHOT_MAX = max(0, int(os.environ.get("LCC_TX_FEWSHOT_MAX", "0" if TX_PROFILE in ("fast", "compact", "latency") else "3")))
PAGE_TX_FEWSHOT_MAX = max(0, int(os.environ.get("LCC_PAGE_TX_FEWSHOT_MAX", "8")))
TX_COMPACT_PROMPT = TX_PROFILE in ("fast", "compact", "latency")
LATENCY_MODE_DEFAULT = _normalize_latency_mode(os.environ.get("LCC_LATENCY_MODE"), "aggressive")
AGG_SOFT_MAX_SEC = max(MIN_SEC, float(os.environ.get("LCC_AGG_SOFT_MAX_SEC", "4.0")))
BAL_SOFT_MAX_SEC = max(MIN_SEC, float(os.environ.get("LCC_BAL_SOFT_MAX_SEC", "3.5")))
AGG_SENT_SILENCE_MS = max(SEG_SILENCE_MS + WINDOW_MS, int(os.environ.get("LCC_AGG_SENT_SILENCE_MS", "900")))
BAL_SENT_SILENCE_MS = max(SEG_SILENCE_MS + WINDOW_MS, int(os.environ.get("LCC_BAL_SENT_SILENCE_MS", "1100")))
AGG_PENDING_CAP = max(40, int(os.environ.get("LCC_AGG_PENDING_CAP", "120")))
BAL_PENDING_CAP = max(60, int(os.environ.get("LCC_BAL_PENDING_CAP", "100")))
AGG_PENDING_MAX_AGE_MS = max(800, int(os.environ.get("LCC_AGG_PENDING_MAX_AGE_MS", "1800")))
BAL_PENDING_MAX_AGE_MS = max(1000, int(os.environ.get("LCC_BAL_PENDING_MAX_AGE_MS", "2400")))
AGG_PREVIEW_DEBOUNCE_MS = max(80, int(os.environ.get("LCC_AGG_PREVIEW_DEBOUNCE_MS", "180")))
BAL_PREVIEW_DEBOUNCE_MS = max(120, int(os.environ.get("LCC_BAL_PREVIEW_DEBOUNCE_MS", "300")))
SPEC_PREVIEW_MIN_CHARS = max(PREVIEW_MIN_CHARS, int(os.environ.get("LCC_SPEC_PREVIEW_MIN_CHARS", "42")))
SPEC_PREVIEW_MIN_DELTA = max(8, int(os.environ.get("LCC_SPEC_PREVIEW_MIN_DELTA", "18")))
SPEC_PREVIEW_COOLDOWN_MS = max(250, int(os.environ.get("LCC_SPEC_PREVIEW_COOLDOWN_MS", "900")))
PREVIEW_PROMOTE_SIMILARITY = min(1.0, max(0.0, float(os.environ.get("LCC_PREVIEW_PROMOTE_SIMILARITY", "0.985"))))
TX_RECENT_FINAL_MAX = max(0, int(os.environ.get("LCC_TX_RECENT_FINAL_MAX", "2")))
TX_RECENT_PREVIEW_MAX = max(0, int(os.environ.get("LCC_TX_RECENT_PREVIEW_MAX", "0")))
TX_PREVIEW_MAX_TOKENS = max(16, int(os.environ.get("LCC_TX_PREVIEW_MAX_TOKENS", "40")))
TX_FINAL_STREAM_EVERY = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_EVERY", "2")))
TX_FINAL_STREAM_MIN_CHARS = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_MIN_CHARS", "8")))
TX_FINAL_STREAM_MIN_WORDS = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_MIN_WORDS", "2")))
TX_FINAL_STREAM_DELTA_CHARS = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_DELTA_CHARS", "4")))


# --- Latency-mode tuning profile (pure) ---------------------------------------------------------------
# One immutable profile per mode keeps the live scheduler and tests on the same knob surface.
@dataclass(frozen=True)
class LatencyProfile:
    mode: str
    pending_cap: int
    pending_max_age_ms: int
    preview_debounce_ms: int
    preview_min_chars: int
    preview_min_delta: int
    preview_cooldown_ms: int
    final_stream_every: int

    def pending_cap_for_pressure(self, pressure: int = 0) -> int:
        return max(40, self.pending_cap - EVS_CAP_DROP) if pressure >= 1 else self.pending_cap

    def pending_max_age_for_pressure(self, pressure: int = 0) -> int:
        return max(600, self.pending_max_age_ms - EVS_AGE_DROP) if pressure >= 1 else self.pending_max_age_ms

    def stream_every(self, final: bool) -> int:
        return self.final_stream_every if final else 4


def _lat_profile(mode: str) -> LatencyProfile:
    if mode == "aggressive":
        return LatencyProfile(
            mode="aggressive",
            pending_cap=AGG_PENDING_CAP,
            pending_max_age_ms=AGG_PENDING_MAX_AGE_MS,
            preview_debounce_ms=AGG_PREVIEW_DEBOUNCE_MS,
            preview_min_chars=SPEC_PREVIEW_MIN_CHARS,
            preview_min_delta=SPEC_PREVIEW_MIN_DELTA,
            preview_cooldown_ms=SPEC_PREVIEW_COOLDOWN_MS,
            final_stream_every=TX_FINAL_STREAM_EVERY,
        )
    if mode == "balanced":
        return LatencyProfile(
            mode="balanced",
            pending_cap=BAL_PENDING_CAP,
            pending_max_age_ms=BAL_PENDING_MAX_AGE_MS,
            preview_debounce_ms=BAL_PREVIEW_DEBOUNCE_MS,
            preview_min_chars=PREVIEW_MIN_CHARS,
            preview_min_delta=PREVIEW_MIN_DELTA,
            preview_cooldown_ms=2200,
            final_stream_every=4,
        )
    return LatencyProfile(
        mode="stable",
        pending_cap=PENDING_CAP,
        pending_max_age_ms=PENDING_MAX_AGE_MS,
        preview_debounce_ms=PREVIEW_DEBOUNCE_MS,
        preview_min_chars=PREVIEW_MIN_CHARS,
        preview_min_delta=PREVIEW_MIN_DELTA,
        preview_cooldown_ms=2200,
        final_stream_every=4,
    )


def _lat_tx_max_tokens_for(final: bool):
    return _TX_GEN_MAX if final else min(_TX_GEN_MAX, TX_PREVIEW_MAX_TOKENS)


def _lat_tx_stream_every_for(final: bool, mode: str):
    return _lat_profile(mode).stream_every(final)


def _lat_preview_debounce_ms(mode: str):
    return _lat_profile(mode).preview_debounce_ms


# --- EVS (Ear-Voice Span) controller: a load-adaptive latency band ------------------------------------
# Like an interpreter, run a TARGET lag band instead of fixed knobs: under sustained translation backlog
# shift the force-commit thresholds DOWN (commit sooner = shorter span = smaller jobs the client merges to
# catch up), and relax to nominal when idle. Hysteresis (separate enter/exit) stops the level oscillating
# utterance-to-utterance. Default ON (LCC_EVS=0 to disable; A/B with policy_ab.sh). When off,
# _evs_step is a no-op (level 0) so behaviour is byte-identical to the static profile. Tested in
# test_evs_controller.py.
EVS_ON = os.environ.get("LCC_EVS", "1") == "1"
EVS_ENTER_MS = max(1, int(os.environ.get("LCC_EVS_ENTER_MS", "1800")))   # backlog >= this -> pressured
EVS_EXIT_MS = max(0, int(os.environ.get("LCC_EVS_EXIT_MS", "900")))      # backlog <= this -> relax (must be < ENTER)
EVS_CAP_DROP = max(0, int(os.environ.get("LCC_EVS_CAP_DROP", "40")))     # chars shaved off pending_cap when pressured
EVS_AGE_DROP = max(0, int(os.environ.get("LCC_EVS_AGE_DROP", "600")))    # ms shaved off pending_max_age_ms when pressured


def _evs_step(level: int, backlog_ms: int) -> int:
    """Pure hysteresis controller. level 0 = nominal, 1 = pressured. Enter at EVS_ENTER_MS, exit at
    EVS_EXIT_MS (< ENTER) so the level can't flap while backlog hovers near one threshold. No-op when off."""
    if not EVS_ON:
        return 0
    if level <= 0:
        return 1 if backlog_ms >= EVS_ENTER_MS else 0
    return 0 if backlog_ms <= EVS_EXIT_MS else 1


def _lat_pending_cap(mode: str, pressure: int = 0):
    return _lat_profile(mode).pending_cap_for_pressure(pressure)


def _lat_pending_max_age_ms(mode: str, pressure: int = 0):
    return _lat_profile(mode).pending_max_age_for_pressure(pressure)


def _lat_soft_max_sec(engine: str, mode: str):
    if _is_sherpa_engine(engine):
        if mode == "aggressive":
            return AGG_SOFT_MAX_SEC
        if mode == "balanced":
            return BAL_SOFT_MAX_SEC
    return SOFT_MAX_SEC


def _lat_effective_sent_silence_ms(mode: str, raw_ms):
    raw = int(raw_ms)
    if mode == "aggressive":
        return min(raw, AGG_SENT_SILENCE_MS)
    if mode == "balanced":
        return min(raw, BAL_SENT_SILENCE_MS)
    return raw


def _lat_sent_windows_for(mode: str, raw_ms):
    return max(1, (_lat_effective_sent_silence_ms(mode, raw_ms) - SEG_SILENCE_MS) // WINDOW_MS)
# ------------------------------------------------------------------------------------------------------

def warm_mlx_selected(asr=False, lm=False, asr_engine=None):
    engine = _normalize_asr_engine(asr_engine, ASR_ENGINE)
    if not _is_sherpa_engine(engine) or lm:
        _require_mlx()
        mx.set_default_device(mx.gpu)
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
        if aux_lm_ready():
            try:
                translate_once("hello world", runtime=_aux_runtime())
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


def _has_hangul(s: str) -> bool:
    return any("가" <= c <= "힣" for c in s)


def _lcp_words(a, b):
    """Longest common word-prefix length (LocalAgreement n=2)."""
    n = 0
    for x, y in zip(a, b):
        if x == y:
            n += 1
        else:
            break
    return n


def _coalesce_batch(batch):
    """LA partials are UX-only. Drop them when finalizable work (clause/flush/eos) is queued,
    otherwise keep only the latest — so stale partials never delay the real ASR/translation."""
    if any(x[0] in ("clause", "flush", "eos") for x in batch):
        return [x for x in batch if x[0] != "partial"]
    latest, out = None, []
    for x in batch:
        if x[0] == "partial":
            latest = x
        else:
            out.append(x)
    if latest is not None:
        out.append(latest)
    return out


def _next_sentence_cut(text: str) -> int:
    """Index to split off the first COMPLETE sentence, or -1. Skips false boundaries: decimals (5.0)
    and short fragments ending in a dotted abbreviation (Dr./Mr.) via a minimum-length guard."""
    for m in SENT_END.finditer(text):
        i, j = m.start(), m.end()
        if text[i] == "." and i > 0 and text[i - 1].isdigit() and j < len(text) and text[j].isdigit():
            continue                                   # decimal point, not a sentence end
        if len(text[:j].strip()) >= MIN_SENT_CHARS:
            return j
    return -1


def _norm_word(w: str) -> str:
    return re.sub(r"^[^\w가-힣]+|[^\w가-힣]+$", "", w.lower())


def _norm_words(text: str):
    return [w for w in (_norm_word(x) for x in (text or "").split()) if w]


def _short_suffix_duplicate(new: str, prev: str) -> bool:
    nw, pw = _norm_words(new), _norm_words(prev)
    return bool(nw and pw and len(nw) <= 4 and len(pw) > len(nw) and pw[-len(nw):] == nw)


def _append_text_dedupe(prev: str, new: str) -> str:
    prev, new = prev.strip(), new.strip()
    if not prev:
        return new
    if not new:
        return prev
    if new.lower() in prev.lower():
        return prev
    pw, nw = prev.split(), new.split()
    max_k = min(14, len(pw), len(nw))
    for k in range(max_k, 0, -1):
        if [_norm_word(w) for w in pw[-k:]] == [_norm_word(w) for w in nw[:k]]:
            tail = " ".join(nw[k:]).strip()
            return prev if not tail else f"{prev} {tail}"
    return f"{prev} {new}"


def _dedupe_commit_overlap(text: str, tail_words, overlapped: bool) -> str:
    """When a sentence just committed and the next clause's audio OVERLAPPED it (soft/VAD overlap), drop the
    leading words of `text` that duplicate the committed sentence's tail (tail_words = its last normalized
    words). The overlap guard is load-bearing: across a REAL pause a repeat is legitimate ("…it. It is…"), so
    strip only when the audio actually overlapped. _append_text_dedupe handles this WITHIN a unit; this covers
    the cross-commit case the sentence split opens. Tested in test_assembler_decisions.py."""
    if not overlapped or not tail_words or not text:
        return text
    nw = text.split()
    tail = list(tail_words)
    for k in range(min(len(tail), len(nw), 3), 0, -1):
        if [_norm_word(w) for w in nw[:k]] == tail[-k:]:
            return " ".join(nw[k:]).strip()
    return text


def _weak_tail(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if s[-1] in ",;:、，-":
        return True
    words = s.split()
    return bool(words and _norm_word(words[-1]) in WEAK_TAIL_WORDS)


def _caption_read_ms(text: str) -> int:
    return max(1300, min(7000, len(text or "") * 75))


# --- Exact-repeat translation cache (catchphrases) -----------------------------------------------------
# Streams repeat themselves constantly ("Thanks for the sub!", greetings, stingers). The per-connection
# translation_cache keys on the rolling recent-pairs context, so an identical line a minute later almost
# always misses. Short SELF-CONTAINED lines get a second, context-free lookup keyed on normalized words —
# their rendering doesn't meaningfully depend on conversation context, so reusing it is safe and instant.
# Longer lines stay context-keyed only. Cleared with the other caches on any translation-context change.
TX_REPEAT_CACHE_ON = os.environ.get("LCC_TX_REPEAT_CACHE", "1") == "1"
TX_REPEAT_MAX_CHARS = max(10, int(os.environ.get("LCC_TX_REPEAT_MAX_CHARS", "60")))
TX_REPEAT_CACHE_MAX = 256


def _repeat_cache_eligible(source: str) -> bool:
    """True for short lines that read as complete on their own: anything tiny, or up to
    TX_REPEAT_MAX_CHARS when it ends on terminal punctuation. Tested in test_text_helpers.py."""
    s = (source or "").strip()
    if not s or len(s) > TX_REPEAT_MAX_CHARS:
        return False
    return len(s) <= 30 or bool(SENT_END.fullmatch(s[-2:]) or SENT_END.fullmatch(s[-1:]))


def _repeat_key(source: str) -> str:
    return " ".join(_norm_words(source))


def _commit_decision(text, eos_now, finalize_now, age_ms, pending_cap, pending_max_age_ms):
    """Whether the in-progress remainder should be force-committed now, and why (pure; mirrors the inline
    logic in inference_loop). reason is '' when force is False. A weak tail (conjunction/aux/trailing
    comma) defers a pause/age/cap commit but never an eos. Tested in test_assembler_decisions.py."""
    weak = _weak_tail(text)
    too_long = len(text) > pending_cap and not weak
    aged = (age_ms > pending_max_age_ms) and not weak
    force = eos_now or too_long or aged or (finalize_now and not weak)
    if not force:
        return False, ""
    return True, ("eos" if eos_now else ("cap" if too_long else ("age" if aged else "pause")))


def _two_pass_eligible(accuracy_mode, pure, clauses, pcm_len):
    """Accuracy-mode 2-pass re-transcription pays off only for a multi-clause, well-aligned unit whose
    audio is within [TWO_PASS_MIN_SEC, TWO_PASS_MAX_SEC] (pure). Tested in test_assembler_decisions.py."""
    return (accuracy_mode and pure and clauses >= 2
            and int(TWO_PASS_MIN_SEC * SR) * 2 <= pcm_len <= int(TWO_PASS_MAX_SEC * SR) * 2)


NUMGUARD_ON = os.environ.get("LCC_NUMGUARD", "1") == "1"
_SIG_NUM_RE = re.compile(r"\d[\d.,:]*\d")   # >= 2 chars; single digits are intentionally ignored — they
                                            # often become Korean counters ("2 cats" -> "고양이 두 마리").


def _sig_numbers(text: str):
    return [re.sub(r"\D", "", m) for m in _SIG_NUM_RE.findall(text or "")]


# Sino-Korean (일이삼…) and native-Korean (하나둘셋…) spellings, so a number the translator wrote out in
# words ("26" → "스물여섯" / "이십육") isn't falsely flagged as dropped. Scoped to 0–99 (the common case);
# larger numbers keep the digit-only check (rarely spelled out, and full numerals get expensive).
_SINO_ONES = ["", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]
_NATIVE_ONES = ["", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]


def _ko_number_forms(digits: str):
    """Korean spelled-out forms (Sino + native) of an integer 0..99; () for anything outside that range."""
    if not digits or not digits.isdigit():
        return ()
    n = int(digits)
    if n > 99:
        return ()
    if n == 0:
        return ("영", "공")
    tens, ones = divmod(n, 10)
    sino = ((_SINO_ONES[tens] if tens > 1 else "") + "십" if tens else "") + _SINO_ONES[ones]
    native = _NATIVE_TENS[tens] + _NATIVE_ONES[ones]
    return tuple(f for f in {sino, native} if f)


def _missing_numbers(source: str, ko: str):
    """Significant source numbers whose value is absent from the translation — as digits OR spelled out in
    Korean (Sino/native), so a meaning-translated number ("at 20 percent" → "이십 퍼센트") isn't flagged."""
    src = _sig_numbers(source)
    if not src:
        return []
    ko = ko or ""
    kod = re.sub(r"\D", "", ko)
    return [n for n in src
            if n and n not in kod and not any(form in ko for form in _ko_number_forms(n))]


def _guard_numbers(source: str, ko: str):
    """Trust guard: if significant source numbers went missing from the translation, append them verbatim so
    the literal value survives (greedy decoding makes a re-translate pointless). Returns
    (display_ko, number_uncertain). No-op when LCC_NUMGUARD=0. Tested in test_number_guard.py."""
    if not NUMGUARD_ON:
        return ko, False
    missing = _missing_numbers(source, ko)
    if not missing:
        return ko, False
    return f"{ko} ({' '.join(missing)})", True


# --- Post-ASR glossary repair (phonetic/fuzzy) ---------------------------------------------------------
# Granite drops punctuation/casing the moment ANY text hint is appended to its prompt (see transcribe_pcm),
# so glossary terms cannot bias the ASR itself. Repair downstream instead: fuzzy-match each user glossary
# source term against the transcript and rewrite near-misses ("black well" / "Blackwel") to the canonical
# spelling BEFORE translation, where the pinned glossary rendering then applies exactly. Pure; applied in
# handle()'s transcribe() wrapper. Off: LCC_ASR_GLOSSARY_REPAIR=0. Tested in test_glossary_repair.py.
GLOSSARY_REPAIR_ON = os.environ.get("LCC_ASR_GLOSSARY_REPAIR", "1") == "1"
_GR_MIN_TERM_CHARS = 4          # shorter terms ("AI", "Go") are too collision-prone to fuzzy-match
_GR_RATIO = 0.84                # SequenceMatcher floor on normalized strings (exact match short-circuits)
_GR_TOKEN_RE = re.compile(r"\S+")
_GR_EDGE_RE = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def _gr_norm(s: str) -> str:
    """Casefold + strip everything non-alphanumeric, so 'black well' / 'Black-Well' both read 'blackwell'."""
    return re.sub(r"[^0-9a-z가-힣]+", "", (s or "").casefold())


def _repair_glossary_terms(text: str, glossary_pairs):
    """Rewrite fuzzy ASR spellings of glossary source terms to their canonical form. Window sizes n-1..n+1
    around each term's word count catch split ('black well') and merged ('SamAltman') transcriptions; the
    surrounding punctuation of the matched span is preserved. Replacements never overlap, longest-window
    match wins, and an exact normalized match of a DIFFERENT glossary term is never rewritten."""
    if not GLOSSARY_REPAIR_ON or not text or not glossary_pairs:
        return text
    terms = []
    norms = {}
    for src, _tgt in glossary_pairs:
        src = (src or "").strip()
        n = _gr_norm(src)
        toks = [t for t in (_gr_norm(w) for w in src.split()) if t]
        if len(n) >= _GR_MIN_TERM_CHARS and toks:
            terms.append((src, n, toks))
            norms[n] = src
    if not terms:
        return text
    tokens = list(_GR_TOKEN_RE.finditer(text))
    if not tokens:
        return text
    edits = []                                   # (start, end, replacement) on the original string
    taken = [False] * len(tokens)
    def _tok_fuzzy(a, b):
        if min(len(a), len(b)) < 3:                      # tiny tokens must match exactly
            return a == b
        return difflib.SequenceMatcher(None, a, b).ratio() >= _GR_RATIO

    for src, term_norm, term_toks in terms:
        nwords = len(term_toks)
        for width in sorted({w for w in (nwords + 1, nwords, nwords - 1) if 1 <= w <= 4}, reverse=True):
            i = 0
            while i + width <= len(tokens):
                if any(taken[i:i + width]):
                    i += 1
                    continue
                span = text[tokens[i].start():tokens[i + width - 1].end()]
                m = _GR_EDGE_RE.match(span)
                pre, core, post = m.group(1), m.group(2), m.group(3)
                cand = _gr_norm(core)
                if not cand or abs(len(cand) - len(term_norm)) > 3:
                    i += 1
                    continue
                exact = cand == term_norm
                if not exact and cand in norms:          # exactly some OTHER term -> leave it alone
                    i += 1
                    continue
                if not exact:
                    # Fuzzy only at the term's own word count, compared TOKEN BY TOKEN — a whole-span ratio
                    # would let "met SamAltman" absorb the neighboring word. Split/merge windows (n±1)
                    # must match the normalized term exactly.
                    wtoks = [_gr_norm(text[t.start():t.end()]) for t in tokens[i:i + width]]
                    if (width != nwords or len(wtoks) != nwords
                            or not all(_tok_fuzzy(a, b) for a, b in zip(wtoks, term_toks))):
                        i += 1
                        continue
                if core != src:                          # exact-with-different-surface still canonicalizes
                    edits.append((tokens[i].start() + len(pre),
                                  tokens[i + width - 1].end() - len(post), src))
                for k in range(i, i + width):
                    taken[k] = True
                i += width
    if not edits:
        return text
    out = text
    for start, end, rep in sorted(edits, reverse=True):
        out = out[:start] + rep + out[end:]
    return out


# --- Session term memory (auto-glossary) ---------------------------------------------------------------
# recent_pairs only carries the last few finals, so on a long stream the 26B forgets how it rendered a name
# twenty minutes ago and the rendering drifts. Mine recurring proper-noun-ish terms from committed finals
# and pin them into the glossary clause automatically: a term the model kept VERBATIM in the translation
# (e.g. "GPT", "Blackwell" left in Latin) pins as an exact pair; everything else pins term-only ("keep
# consistent" + ASR biasing). User glossary entries always win. Updates are BATCHED (every N finals) because
# the glossary lives in the system prompt — every change invalidates the translator's KV prefix, so the
# ~850ms re-prefill is amortized. Off: LCC_TERM_MEMORY=0. Tested in test_term_memory.py.
TERM_MEMORY_ON = os.environ.get("LCC_TERM_MEMORY", "1") == "1"
TERM_MEMORY_MAX = max(0, int(os.environ.get("LCC_TERM_MEMORY_MAX", "12")))          # auto terms in the clause
TERM_MEMORY_MIN_COUNT = max(1, int(os.environ.get("LCC_TERM_MEMORY_MIN_COUNT", "2")))  # recur before pinning
TERM_MEMORY_UPDATE_EVERY = max(1, int(os.environ.get("LCC_TERM_MEMORY_UPDATE_EVERY", "8")))
TERM_MEMORY_STATS_MAX = 200
# Single capitalized words that are ordinary sentence material, not names. Filters SINGLE-word candidates
# only — multi-word runs ("Sam Altman") and acronyms are kept.
_TERM_STOPWORDS = frozenset(w.casefold() for w in (
    "The This That These Those There Here What When Where Which Who Whose Why How If And But Or So Not "
    "No Yes It Its He She They We You I My Our Your His Her Their Then Now Today Tonight Yesterday "
    "Tomorrow Okay Oh Hey Hello Hi Thanks Thank Well Right Let Look Listen Just Also Even Still Maybe "
    "Please Sorry Is Are Was Were Do Does Did Done Have Has Had Can Could Will Would Should May Might "
    "Must Get Got Go Going Gone Come Coming Welcome Back New One Two Three Four Five First Second Next "
    "Last Good Great Big Small Many Most More Some All Every Each Other Another Because Before After "
    "Over Under Again Anyway Actually Basically Literally Honestly Alright Guys Everyone Everybody"
).split())
_TERM_CAND_RE = re.compile(r"\b(?:[A-Z]{2,}[0-9]*|[A-Z][a-zA-Z0-9]{2,})\b")
_TERM_SENT_LEAD_RE = re.compile(r"[.!?。！？…\"'»」』)\]]\s*$")


def _mine_terms(source: str, ko: str):
    """Proper-noun-ish term candidates from one committed (source, translation) pair.
    Returns [(term, rendering)] where rendering == term when the translation kept the term verbatim
    (locks the Latin form), else "" (term-only: consistency clause + ASR bias). Adjacent capitalized
    words merge into one multi-word term; sentence-initial single words are skipped (too often just
    sentence case), as are stopwords. Latin-script mining only (the dominant EN->KO direction)."""
    source, ko = source or "", ko or ""
    matches = list(_TERM_CAND_RE.finditer(source))
    if not matches:
        return []
    runs, cur = [], []
    for m in matches:
        if cur and source[cur[-1].end():m.start()] == " ":
            cur.append(m)
        else:
            if cur:
                runs.append(cur)
            cur = [m]
    runs.append(cur)
    out, seen = [], set()
    for run in runs:
        while run and run[0].group(0).casefold() in _TERM_STOPWORDS:
            run = run[1:]                                   # "The OpenAI" -> "OpenAI"
        if not run:
            continue
        term = source[run[0].start():run[-1].end()]
        if len(run) == 1:
            w = run[0].group(0)
            if w.casefold() in _TERM_STOPWORDS:
                continue
            acronym = w.isupper()
            lead = source[:run[0].start()].strip()
            initial = not lead or bool(_TERM_SENT_LEAD_RE.search(source[:run[0].start()]))
            if initial and not acronym:                     # sentence case, not evidence of a name
                continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append((term, term if term in ko else ""))
    return out


def _update_term_memory(stats: dict, source: str, ko: str, now: float):
    """Fold one committed final into the running term stats (mutates; bounded). Returns True when a
    term reached pin eligibility or gained a verbatim rendering — i.e. the merged clause may change."""
    notable = False
    for term, rendering in _mine_terms(source, ko):
        rec = stats.get(term)
        if rec is None:
            rec = stats[term] = {"count": 0, "rendering": "", "seen": 0.0}
        rec["count"] += 1
        rec["seen"] = now
        if rec["count"] == TERM_MEMORY_MIN_COUNT:
            notable = True
        if rendering and not rec["rendering"]:
            rec["rendering"] = rendering
            if rec["count"] >= TERM_MEMORY_MIN_COUNT:
                notable = True
    if len(stats) > TERM_MEMORY_STATS_MAX:                  # bound pathological sessions
        for k in sorted(stats, key=lambda t: (stats[t]["count"], stats[t]["seen"]))[:len(stats) - TERM_MEMORY_STATS_MAX]:
            del stats[k]
    return notable


def _merge_auto_glossary(user_pairs, stats: dict, cap: int = None):
    """The auto-pinned (term, rendering) list: recurring terms not already covered by the user glossary,
    most-frequent first, capped. Pure — the caller appends this to the user pairs at prompt-build time."""
    cap = TERM_MEMORY_MAX if cap is None else cap
    if cap <= 0:
        return []
    user_norms = {_gr_norm(s) for s, _ in (user_pairs or ())}
    cands = [(t, rec) for t, rec in stats.items()
             if rec["count"] >= TERM_MEMORY_MIN_COUNT and _gr_norm(t) not in user_norms]
    cands.sort(key=lambda kv: (-kv[1]["count"], -kv[1]["seen"], kv[0]))
    return [(t, rec["rendering"]) for t, rec in cands[:cap]]


# --- Interpretation policy ----------------------------------------------------------------------------
# The "when to wait / commit / (later) compress / repair" surface, borrowed from simultaneous interpreting.
# The pieces feed in at different pipeline points and stay separate pure functions: _evs_step (load -> the
# latency band), _commit_decision (timing -> commit/wait), _two_pass_eligible (clean-up pass), _preview_is_stale
# (preview validity), _guard_numbers (number trust). decide_commit() unifies the commit-time call and carries a
# risk read so a future compression mode can hold the risky bits longer. See docs/caption-lifecycle.md.
_NEG_RE = re.compile(r"n't\b|\b(?:not|never|no|none|without|cannot|nor|neither)\b", re.I)


def _source_risk(text: str) -> str:
    """'high' when the line carries information costly to get wrong — numbers or negation — which viewers
    distrust most when a caption is off; else 'low'. Pure; tested in test_policy.py."""
    return "high" if (_sig_numbers(text) or _NEG_RE.search(text or "")) else "low"


@dataclass
class InterpretDecision:
    action: str   # "commit" | "wait"  (compress / repair / draft reserved for later phases)
    reason: str   # eos / cap / age / pause / "" — why
    risk: str     # "low" | "high" — information risk of the source line


def decide_commit(text, eos_now, finalize_now, age_ms, pending_cap, pending_max_age_ms) -> InterpretDecision:
    """Commit-time policy: commit now or wait, why, and how risky the content is. The commit/wait choice is
    byte-identical to _commit_decision; this adds the risk annotation. Tested in test_policy.py."""
    force, reason = _commit_decision(text, eos_now, finalize_now, age_ms, pending_cap, pending_max_age_ms)
    return InterpretDecision(action="commit" if force else "wait", reason=reason, risk=_source_risk(text))
# ------------------------------------------------------------------------------------------------------


def _preview_is_stale(job, finalized_units, cur_uid, cur_rev, latest_preview_rev):
    """A preview job is stale once its unit finalized, the active unit moved on, or its rev was superseded
    (pure; mirrors the translation scheduler's check). Final jobs are never stale. Tested in
    test_scheduler_staleness.py."""
    return (
        not job["final"] and
        (
            job["unit_id"] in finalized_units or
            cur_uid != job["unit_id"] or
            cur_rev != job["rev"] or
            latest_preview_rev.get(job["unit_id"]) != job["rev"]
        )
    )


def _stream_visible_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _stream_partial_substantial(text: str) -> bool:
    text = _clean(text)
    if not text or text.endswith(("(", "[", "{", ",", "…", "、", "，", "·")):
        return False
    visible = _stream_visible_chars(text)
    if visible >= TX_FINAL_STREAM_MIN_CHARS or len(text.split()) >= TX_FINAL_STREAM_MIN_WORDS:
        return True
    # Short complete captions such as "네." / "맞습니다." are useful, but early fragments
    # like "오늘은" are not. Terminal punctuation is the cheap completeness signal.
    return visible >= 2 and bool(re.search(r"[.!?。！？]$", text))


def _stream_partial_should_emit(text: str, last: str) -> bool:
    text = _clean(text)
    last = _clean(last)
    if not _stream_partial_substantial(text):
        return False
    if not last:
        return True
    if text == last:
        return False
    if len(text.split()) > len(last.split()):
        return True
    return _stream_visible_chars(text) - _stream_visible_chars(last) >= TX_FINAL_STREAM_DELTA_CHARS


# ASR prompts now live with the active mlx-audio backend wiring above.


def transcribe_pcm(pcm: bytes, hint: str = "", asr_engine=None):
    engine = _normalize_asr_engine(asr_engine, ASR_ENGINE)
    if engine == "parakeet":
        if parakeet_asr is None:
            raise RuntimeError("Parakeet ASR is not loaded")
        return parakeet_asr.transcribe_pcm(pcm, hint=hint)

    mx.set_default_device(mx.gpu)
    # 16k mono float32 array straight to the audio model: load_audio()/generate take ndarrays as-is
    # (no resample), so we skip the per-segment /tmp WAV write+decode round-trip.
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    audio *= 1.0 / 32768.0

    if _is_mlxa_engine(engine):                          # mlx-audio audio-LLM (granite / qwen3)
        if mlxa_model is None:
            raise RuntimeError(f"{engine} ASR is not loaded")
        # No ASR-side text hint: qwen3 auto-detects language + punctuates with no prompt; granite's punctuation
        # is fragile — ANY appended hint ("Keywords:"/"Expected names:") suppresses capitalization+punctuation —
        # so keep the clean instruction. (Glossary still biases the 26B translation; only source-side name
        # spelling is dropped, and both models already transcribe names well.)
        gen_kw = {"prompt": GRANITE_ASR_PROMPT} if engine == "granite" else {}
        res = mlxa_model.generate(audio, temperature=0.0, max_tokens=ASR_MAX_TOKENS, **gen_kw)
        raw = getattr(res, "text", None)
    elif _is_whisper_engine(engine):                     # Whisper large-v3 — own decode, no prompt (INV-7)
        import mlx_whisper
        # Each VAD chunk is independent: don't condition on previous text (avoids cross-chunk drift).
        res = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER_REPO,
                                     temperature=0.0, condition_on_previous_text=False, verbose=False)
        raw = res.get("text") if isinstance(res, dict) else getattr(res, "text", None)
    else:
        raise RuntimeError(f"unknown ASR engine: {engine}")
    text = (raw if raw is not None else str(res)).strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    dedup = []
    for l in lines:                       # collapse consecutive echoed lines; keep distinct content
        if not dedup or dedup[-1] != l:
            dedup.append(l)
    text = " ".join(dedup)
    if not text or "[no speech]" in text.lower():
        return None
    return text


_CLEAN_RE = re.compile(r"<\|?channel\|?>.*?<\|?channel\|?>", re.S)
def _clean(s: str) -> str:
    return _CLEAN_RE.sub("", s).strip()


# Register-aware, source-language-aware style anchors (few-shot). Content type changes the right
# tone (a gaming stream vs a conference talk vs a newscast), so each register carries its own anchors;
# and EN->KO vs JA->KO want different example sources, so anchors are keyed by detected source language
# too. Kept to 2-3 lines each so per-call prefill stays cheap. Falls back: register->casual, src->English.
_TX_FEWSHOT = {
    "Korean": {
        "casual": {   # gaming / streaming — 캐주얼 방송 진행자 톤, 해요체
            "English": [
                ("Hey everyone, welcome back to the stream.", "여러분 안녕하세요, 다시 방송으로 돌아왔습니다."),
                ("So basically the whole thing crashed right in the middle of the demo.", "그러니까 결국, 시연 도중에 전체가 그냥 다 뻗어버린 거예요."),
                ("Okay the patch just dropped and they completely reworked ranked.", "자, 방금 패치 떴는데 랭크를 완전히 갈아엎었어요."),
            ],
            "Japanese": [
                ("じゃあ次のステージ行ってみましょうか。", "자, 그럼 다음 스테이지 가볼까요."),
                ("いや今のはマジで運が良かったですね。", "와, 방금 건 진짜 운이 좋았네요."),
            ],
        },
        "lecture": {   # talks / conferences — 정중한 합니다체, 기술용어 정확
            "English": [
                ("Today we're announcing our next-generation GPU architecture.", "오늘 저희는 차세대 GPU 아키텍처를 발표합니다."),
                ("Let me walk you through how the training pipeline actually works.", "학습 파이프라인이 실제로 어떻게 동작하는지 차근차근 설명드리겠습니다."),
                ("This delivers roughly five times the throughput of the previous generation.", "이는 이전 세대 대비 약 5배의 처리량을 제공합니다."),
            ],
            "Japanese": [
                ("本日は新しいアーキテクチャについてご紹介します。", "오늘은 새로운 아키텍처에 대해 소개해 드리겠습니다."),
                ("ここで実際のベンチマーク結果をご覧ください。", "여기서 실제 벤치마크 결과를 보시겠습니다."),
            ],
        },
        "news": {      # news / interview — 중립 보도체
            "English": [
                ("Officials say the new policy will take effect next month.", "당국은 새 정책이 다음 달부터 시행된다고 밝혔습니다."),
                ("The company reported record quarterly earnings on Thursday.", "이 회사는 목요일 분기 사상 최대 실적을 발표했습니다."),
                ("Critics argue the measure does not go far enough.", "비판론자들은 이 조치가 충분하지 않다고 지적합니다."),
            ],
            "Japanese": [
                ("政府は来週、追加の対策を発表する見通しです。", "정부는 다음 주 추가 대책을 발표할 전망입니다."),
                ("専門家はこの傾向が続くと指摘しています。", "전문가들은 이런 추세가 이어질 것이라고 지적합니다."),
            ],
        },
        "chat": {      # casual chat / podcast — 친근한 대화체
            "English": [
                ("Honestly I didn't even think it would work at first.", "솔직히 처음엔 이게 될 거라고 생각도 안 했어요."),
                ("Wait, are you serious right now? That's insane.", "잠깐, 지금 진심이에요? 완전 말도 안 되는데."),
                ("Yeah so we ended up just talking about it for like two hours.", "네, 그래서 결국 그거 가지고 두 시간을 떠들었어요."),
            ],
            "Japanese": [
                ("いやー、それめっちゃ分かるわ。", "아 그거 완전 이해돼요."),
                ("でさ、結局どうなったの?", "그래서, 결국 어떻게 됐어요?"),
            ],
        },
    },
    "Japanese": {
        "casual": {
            "English": [
                ("Hey everyone, welcome back to the stream.", "皆さんこんにちは、配信に戻ってきました。"),
                ("So basically the whole thing crashed right in the middle of the demo.", "それで結局、デモの途中で全部落ちちゃったんですよ。"),
                ("That's a great question — let me actually break it down for you.", "すごくいい質問ですね。ちょっと順を追って説明しますね。"),
            ],
        },
        "lecture": {
            "English": [
                ("Today we're announcing our next-generation GPU architecture.", "本日、次世代のGPUアーキテクチャを発表いたします。"),
                ("This delivers roughly five times the throughput of the previous generation.", "これは前世代の約5倍のスループットを実現します。"),
            ],
        },
        "news": {
            "English": [
                ("Officials say the new policy will take effect next month.", "当局は、新たな政策が来月から施行されると発表しました。"),
                ("The company reported record quarterly earnings on Thursday.", "同社は木曜日、四半期として過去最高の業績を発表しました。"),
            ],
        },
    },
}

_PAGE_TX_FEWSHOT = {
    "Korean": {
        "English": [
            ("Share", "공유"),
            ("Log in", "로그인"),
            ("View more comments", "댓글 더 보기"),
            ("11 hours ago", "11시간 전"),
            ("r/SipsTea", "r/SipsTea"),
            ("Infamous_Question430", "Infamous_Question430"),
            ("People taking zero accountability is an epidemic these days.", "요즘은 책임을 전혀 지지 않는 사람이 너무 많다."),
            ("Woman saves her dogs from another dog in the street", "길거리에서 다른 개로부터 자기 강아지들을 구해낸 여자"),
        ],
        "Japanese": [
            ("コメントをもっと見る", "댓글 더 보기"),
            ("シェア", "공유"),
        ],
    },
    "Japanese": {
        "English": [
            ("Share", "共有"),
            ("Log in", "ログイン"),
            ("View more comments", "コメントをさらに表示"),
            ("11 hours ago", "11時間前"),
            ("r/SipsTea", "r/SipsTea"),
            ("Infamous_Question430", "Infamous_Question430"),
        ],
    },
    "English": {
        "Korean": [
            ("공유", "Share"),
            ("로그인", "Log in"),
            ("댓글 더 보기", "View more comments"),
            ("11시간 전", "11 hours ago"),
        ],
        "Japanese": [
            ("共有", "Share"),
            ("ログイン", "Log in"),
            ("コメントをさらに表示", "View more comments"),
        ],
    },
}

# Per-register tone instruction, appended to the system prompt (target-specific).
_REGISTER_TONE = {
    "Korean": {
        "casual":  "캐주얼한 방송 진행자 말투로, 화자 톤에 맞춰 존댓말(해요체)을 기본으로 자연스럽게 옮겨라. 번역투·영어 어순·직역을 피하고 자연스러운 한국어 종결어미를 써라. ",
        "lecture": "발표·강연 상황의 정중한 존댓말(합니다체)로 옮겨라. 기술용어·고유명사는 정확히, 매끄럽고 명료한 문장으로. 번역투·영어 어순을 피해라. ",
        "news":    "중립적이고 정제된 보도체 존댓말로 옮겨라. 군더더기 없이 사실 위주로, 자연스러운 한국어 보도 문장으로. ",
        "chat":    "친구끼리 편하게 대화하듯 자연스러운 구어체로 옮겨라. 화자 톤에 맞춰 해요체/반말이 섞여도 좋다. 번역투를 피해라. ",
    },
    "Japanese": {
        "casual":  "配信者の自然な口調で、敬体を基本に訳すこと。翻訳調や英語の語順を避け、自然な終助詞で。 ",
        "lecture": "講演・発表の丁寧な敬体（です・ます）で訳すこと。専門用語・固有名詞は正確に、明瞭で滑らかな文に。 ",
        "news":    "中立的で整った報道体で訳すこと。余計な要素を省き、事実中心に自然な日本語で。 ",
        "chat":    "親しい会話のような自然な口語で訳すこと。話者のトーンに合わせて。 ",
    },
}
_REGISTERS = ("casual", "lecture", "news", "chat")

_KANA_RE = re.compile(r"[぀-ヿ]")        # hiragana + katakana -> Japanese source
_LATIN_RE = re.compile(r"[A-Za-z]")
_HANGUL_RE = re.compile(r"[가-힣]")
def _src_lang(text: str) -> str:
    # Ratio-based, not "any hangul -> Korean": an English line with a Korean name (e.g.
    # "I talked to 민준 about the demo") must NOT be treated as Korean (would skip translation).
    h = len(_HANGUL_RE.findall(text or ""))
    k = len(_KANA_RE.findall(text or ""))
    lat = len(_LATIN_RE.findall(text or ""))
    letters = h + k + lat
    if letters <= 0:
        return "English"
    if h >= 4 and h / letters >= 0.45:
        return "Korean"
    if k >= 2 and k / letters >= 0.30:
        return "Japanese"
    return "English"


def _fewshot(target: str, register: str, src_lang: str, profile: str = "caption"):
    if profile == "page":
        by_src = _PAGE_TX_FEWSHOT.get(target, {})
        return by_src.get(src_lang) or by_src.get("English") or []
    by_reg = _TX_FEWSHOT.get(target, {})
    by_src = by_reg.get(register) or by_reg.get("casual") or {}
    return by_src.get(src_lang) or by_src.get("English") or []


def _parse_glossary(raw: str):
    """Parse a user glossary into (source_term, target_rendering) pairs. Accepts 'Blackwell=블랙웰',
    'Blackwell→블랙웰', or a bare 'Blackwell' (term-only: ASR biasing + 'keep consistent')."""
    pairs = []
    for line in (raw or "").splitlines():
        line = line.strip()[:160]
        if not line:
            continue
        sep = "=" if "=" in line else ("→" if "→" in line else None)
        if sep:
            a, b = line.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a:
                pairs.append((a, b))
        else:
            pairs.append((line, ""))
        if len(pairs) >= 40:
            break
    return pairs


def _glossary_clause(pairs) -> str:
    rules = [f"'{s}'→'{t}'" for s, t in pairs if t]
    terms = [s for s, t in pairs if not t]
    out = ""
    if rules:
        out += "Always translate these terms exactly as given: " + "; ".join(rules) + ". "
    if terms:
        out += "Keep these names/terms consistent: " + ", ".join(terms) + ". "
    return out


_FAST_REGISTER_TONE = {
    "casual": "Casual broadcast tone. ",
    "lecture": "Clear lecture/presentation tone. ",
    "news": "Concise news/interview tone. ",
    "chat": "Natural conversation tone. ",
}


def _page_tx_system(target: str, hint: str = "", glossary_pairs=(), custom: str = "") -> str:
    # DOM-preservation structure is ALWAYS kept (INV-10): a custom prompt layers translation STYLE on top
    # of the mandatory structural rules (same-node replacement, handles/URLs/code unchanged, output-only) —
    # it never removes them. So for page, custom augments; the structural guard prose stays verbatim.
    custom = (custom or "").strip()
    if TX_COMPACT_PROMPT:
        s = (f"Translate visible web page text into concise {target}. Replace the same DOM node only. "
             "Keep handles, subreddit names, URLs, code, numbers, timestamps, emoji, and already-target-language text unchanged. "
             "Use label-like wording for UI text. ")
        if custom:
            s += f"Follow these translation instructions: {custom}. "
        s += _glossary_clause(glossary_pairs)
        if hint:
            s += f"Page context / terms: {hint}. "
        return s + f"Output only the replacement text in {target}, or the unchanged source."
    s = (f"You translate visible web page text into {target} for direct DOM replacement. Each user message is the "
         "complete text of one page node or short UI fragment. Output exactly the replacement text for that same "
         "node, with no explanations, prefixes, quotes, markdown, or extra alternatives. Preserve formatting intent, "
         "line breaks when useful, numbers, timestamps, currencies, emoji, handles, subreddit/community names, URLs, "
         "code, IDs, product names, and proper nouns. If the text is already in {target}, a username/handle, a "
         "subreddit/community name, code, a URL, or not meaningful to translate, return it unchanged. For buttons, "
         "menus, labels, counts, and navigation text, use short native UI wording instead of conversational sentences. "
         "Do not add politeness, commentary, inferred context, or sentence endings that are not present in the source. ")
    if custom:
        s += f"Follow these translation instructions: {custom}. "
    s += _glossary_clause(glossary_pairs)
    if hint:
        s += f"Use this page context only to disambiguate names/terms: {hint}. "
    return s + f"Output ONLY the {target} replacement text, nothing else."


def _tx_system(target: str, register: str = "casual", hint: str = "", glossary_pairs=(),
               profile: str = "caption", custom: str = "") -> str:
    # custom (when set) REPLACES the descriptive instruction + register tone (per "서술부만 교체"), but the
    # structural guards stay: glossary clause, hint clause, and the final "Output ONLY the translation" guard
    # (INV-10). Empty custom => byte-identical to the previous prompt (INV-11 / backward-compat).
    if profile == "page":
        return _page_tx_system(target, hint, glossary_pairs, custom)
    custom = (custom or "").strip()
    if TX_COMPACT_PROMPT:
        if custom:
            s = custom + " "
        else:
            s = (f"Translate live speech into natural {target}. Preserve meaning, tone, and names. "
                 f"If the line is incomplete, translate only what is present. ")
            s += _FAST_REGISTER_TONE.get(register, "")
        s += _glossary_clause(glossary_pairs)
        if hint:
            s += f"Consistent names/terms: {hint}. "
        return s + f"Output only {target}."
    if custom:
        s = custom + " "
    else:
        s = (f"You are an expert live interpreter turning a continuous talk/stream into natural, fluent {target}. "
             f"Translate the user's line by MEANING into idiomatic {target} that a native speaker would actually "
             f"say — never word-for-word, never transliterate, no translationese or foreign word order. Match the "
             f"speaker's tone and register, and keep names/terms consistent with the running conversation above. "
             f"The line may be cut off mid-sentence; translate what is there naturally without inventing the rest. ")
        s += _REGISTER_TONE.get(target, {}).get(register, "")
    s += _glossary_clause(glossary_pairs)
    if hint:
        s += f"Render these names/terms consistently: {hint}. "
    return s + f"Output ONLY the {target} translation, nothing else."


def _translate_messages(text, recent_pairs=(), target="Korean", hint="", register="casual", glossary_pairs=(),
                        profile: str = "caption", custom: str = ""):
    """The chat-message list for one clause translation: register-aware system instruction + source-language-
    matched few-shot anchors + the model's recent (source->target) renderings (consistency) + the line itself.
    Shared by the MLX and CUDA backends so both produce byte-identical prompts (same translation regardless of
    runtime — custom is threaded HERE, in the shared builder, not per-backend; INV-11). Each backend applies
    its own chat template (MLX: apply_chat_template; CUDA: server-side)."""
    msgs = [{"role": "system", "content": _tx_system(target, register, hint, glossary_pairs, profile, custom)}]
    fewshot_max = PAGE_TX_FEWSHOT_MAX if profile == "page" else TX_FEWSHOT_MAX
    for ex_src, ex_tgt in _fewshot(target, register, _src_lang(text), profile)[:fewshot_max]:   # source-lang-matched style anchors
        msgs += [{"role": "user", "content": ex_src}, {"role": "assistant", "content": ex_tgt}]
    for s, t in recent_pairs:                                   # the model's own recent renderings -> consistency
        msgs += [{"role": "user", "content": s}, {"role": "assistant", "content": t}]
    msgs.append({"role": "user", "content": text})
    return msgs


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


def _page_marker_system(target: str, hint: str = "", glossary_pairs=(), recent_pairs=(), custom: str = "") -> str:
    s = (
        f"Translate visible web-page text into {target} for direct DOM replacement. The input has numbered "
        "segments — each a marker like @@1@@ on its own line followed by that segment's text. Output the SAME "
        "@@n@@ markers in the SAME order, each on its own line, immediately followed by ONLY that segment's "
        f"{target} replacement text. Keep every @@n@@ marker exactly; translate every segment; never merge, "
        "drop, reorder, or add segments. Preserve handles, subreddit/community names, URLs, code, IDs, numbers, "
        "timestamps, emoji, product names, and already-target-language text unchanged. Use short native wording "
        "for UI labels. Output only the @@n@@ markers and their translations — no JSON, markdown, comments, "
        "quotes, or explanations. Some segments contain inline placeholders like ⟦1⟧ that mark where a "
        "link or fixed element sits — echo every ⟦n⟧ placeholder verbatim, in ascending order, exactly "
        "once each, and translate the text around them naturally; never translate, drop, reorder, or duplicate a "
        "placeholder. "
    )
    if (custom or "").strip():   # custom layers STYLE on top; the @@n@@/placeholder structure above stays (INV-10)
        s += f"Follow these translation instructions: {custom.strip()}. "
    s += _glossary_clause(glossary_pairs)
    if hint:
        s += f"Page context/terms: {hint}. "
    if recent_pairs:
        recent = "; ".join(f"'{src}'->'{tgt}'" for src, tgt in list(recent_pairs)[-4:])
        s += "Recent page renderings for consistency only: " + recent + ". "
    return s


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


def _translate_page_batch_messages(items, recent_pairs=(), target="Korean", hint="", register="casual",
                                   glossary_pairs=(), custom: str = ""):
    """Prompt for page DOM microbatch translation. Output is @@n@@-marked segments so the content script can
    map every replacement back to its text node and the bridge can stream segments as they complete; a missing
    marker falls back to per-item translation. A marker-free block-context preamble (when items carry `ctx`)
    gives the model the surrounding prose for fragments split by inline elements."""
    msgs = [{"role": "system", "content": _page_marker_system(target, hint, glossary_pairs, recent_pairs, custom)}]
    src_lang = _src_lang(" ".join(str(it.get("text", "")) for it in items))
    few = _fewshot(target, register, src_lang, "page")[:min(PAGE_TX_FEWSHOT_MAX, 4)]
    if few:
        ex_in = _page_marker_input([{"text": src} for src, _ in few])
        ex_out = "\n\n".join(f"@@{i + 1}@@\n{tgt}" for i, (_, tgt) in enumerate(few))
        msgs += [{"role": "user", "content": ex_in}, {"role": "assistant", "content": ex_out}]
    msgs.append({"role": "user", "content": _page_block_context_preamble(items) + _page_marker_input(items)})
    return msgs


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


def _ask_messages(mode: str, transcript_text: str, question: str = "", target: str = "Korean"):
    """(messages, max_tokens) for an on-demand summary / Q&A over the running transcript. Shared by both
    backends so the summary/answer is identical across runtimes."""
    if mode == "qa" and question.strip():
        sysmsg = ("You answer a viewer's question about a live talk/stream using ONLY the transcript below. "
                  f"Answer in {target}, concise and concrete. If the transcript doesn't cover it, say so in {target}.")
        user = f"Transcript:\n{transcript_text}\n\nQuestion: {question}"
        max_toks = 320
    else:
        sysmsg = (f"You summarize a live talk/stream from its running transcript. Give a concise {target} summary "
                  f"of the key points so far as short bullet points. Output only the summary, in {target}.")
        user = f"Transcript so far:\n{transcript_text}"
        max_toks = 420
    return [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}], max_toks


def _reset_tx_cache():
    global _tx_cache, _tx_cache_ids
    _tx_cache, _tx_cache_ids = None, []

def _reset_page_tx_cache():
    global _page_tx_cache, _page_tx_cache_ids
    _page_tx_cache, _page_tx_cache_ids = None, []

def _tx_cache_offset(cache):
    """Logical token length the prompt cache is at (all layers agree), or None if unreadable. For sliding
    layers (RotatingKVCache) this is the logical position, NOT resident size; trimmability is separate
    (offset < max_size) and must be checked via can_trim_prompt_cache."""
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
        return list(map(int, lm_tok.encode(prompt)))
    if hasattr(prompt, "tolist"):
        prompt = prompt.tolist()
    return [int(x) for x in prompt]

def _trim_cache_or_reset(cache, reset_fn, n, expected_after):
    """Trim exactly n tokens and VERIFY (count + post-offset). Reset the persistent cache and return False
    on any failure — Gemma 4 sliding layers (RotatingKVCache) go non-trimmable once offset >= sliding_window
    and trim_prompt_cache then silently returns 0, which would desync _tx_cache_ids from the real cache."""
    if n <= 0:
        return True
    if cache is None or not can_trim_prompt_cache(cache):
        reset_fn(); return False
    try:
        got = trim_prompt_cache(cache, n)
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
    mx.set_default_device(mx.gpu)
    model = lm_model if model is None else model
    proc = lm_tok if proc is None else proc
    try:
        prompt = proc.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
    except Exception:
        prompt = proc.tokenizer.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
    res = vlm_generate(model, proc, prompt, max_tokens=int(gen_max), verbose=False)
    text = _clean(getattr(res, "text", None) or (res if isinstance(res, str) else str(res)))
    if on_update is not None and text:
        on_update(text)
    return text


def translate_once(text: str, recent_pairs=(), target: str = "Korean", hint: str = "",
                   register: str = "casual", glossary_pairs=(), on_update=None, kv_reuse=None,
                   max_tokens=None, stream_every=None, profile: str = "caption", custom: str = "",
                   runtime=None):
    """Stateless per-clause translation, primed for quality: a strong register-aware instruction,
    source-language-matched few-shot anchors, a pinned glossary, and the last few (source -> target)
    pairs as conversation context so terminology/tone stay consistent across the stream. Re-callable on
    a growing clause (EN->KO reverses word order, so we re-translate the whole clause). Runs on _mlx_pool
    (single worker -> the module-level _tx_cache has no race). runtime=(model, tok, is_vlm) redirects the
    call to the AUX translator on its own pool — that path always uses a fresh per-call cache (no shared
    KV state, so it is safe off the main worker thread)."""
    global _tx_cache, _tx_cache_ids, _TX_KV_WINDOW
    msgs = _translate_messages(text, recent_pairs, target, hint, register, glossary_pairs, profile, custom)
    model, tok, is_vlm = (lm_model, lm_tok, _LM_IS_VLM) if runtime is None else runtime
    if is_vlm:
        return _vlm_generate_text(msgs, max(1, int(max_tokens or _TX_GEN_MAX)), on_update, model, tok)
    mx.set_default_device(mx.gpu)
    try:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
    prompt = _ensure_ids(prompt)
    gen_max = max(1, int(max_tokens or _TX_GEN_MAX))
    if runtime is None and _TX_KV_WINDOW is None:          # learn the sliding window once (fail-safe on unknown caches)
        _TX_KV_WINDOW = _learn_tx_window(_tx_cache if _tx_cache is not None else make_prompt_cache(lm_model))
    # Reuse the KV of the static prefix (system + few-shot + recent_pairs ~= 95% of the prompt, identical
    # across calls): trim the persistent cache to the longest prefix it still shares with this prompt, then
    # prefill only the divergent tail (~850ms -> ~280ms TTFT). The cache is STATEFUL: the invariant
    # _tx_cache_ids == cache.offset must hold on EVERY path, so every trim is verified and ANY failure
    # (incl. a non-trimmable RotatingKVCache once offset >= sliding_window) resets to a fresh cache; after
    # each call we trim the generated suffix back to prompt-only. Single _mlx_pool worker. Off: LCC_TX_KVREUSE=0.
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
            _tx_cache, _tx_cache_ids = make_prompt_cache(lm_model), []
        common = _lcp_words(_tx_cache_ids, prompt)
        if len(_tx_cache_ids) - common > 0 and not _tx_trim_or_reset(len(_tx_cache_ids) - common, common):
            _tx_cache, _tx_cache_ids, common = make_prompt_cache(lm_model), [], 0
        feed = prompt[common:]
        if not feed:                                           # prompt already resident: rewind one token,
            if common <= 0 or not _tx_trim_or_reset(1, len(prompt) - 1):   # or rebuild if the cache can't rewind
                _tx_cache, _tx_cache_ids, common, feed = make_prompt_cache(lm_model), [], 0, prompt
            else:
                common -= 1; feed = prompt[common:]
        cache = _tx_cache
    else:
        cache = make_prompt_cache(model, max_kv_size=2048)
        feed = prompt
    out, since = [], 0
    try:
        every = max(1, int(stream_every or 4))
        for r in lm_stream(model, tok, feed, max_tokens=gen_max, sampler=_sampler, prompt_cache=cache):
            out.append(r.text)
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
    if reuse and _tx_cache is not None:
        actual = _tx_cache_offset(_tx_cache)
        if actual is None:
            _reset_tx_cache()              # can't verify -> don't keep a cache we can't trust
        elif actual < len(prompt):     # output context itself is suspect -> recompute once on a fresh cache
            _reset_tx_cache()
            print(f"[txkv] invariant breach: offset {actual} < prompt {len(prompt)} -> fresh retry", flush=True)
            return translate_once(
                text, recent_pairs, target, hint, register, glossary_pairs, None, kv_reuse=False,
                max_tokens=max_tokens, stream_every=stream_every, profile=profile, custom=custom,
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

    model, tok, is_vlm = (lm_model, lm_tok, _LM_IS_VLM) if runtime is None else runtime
    if is_vlm:
        raw = _vlm_generate_text(msgs, gen_max, None, model, tok)
        return _finish(raw)
    mx.set_default_device(mx.gpu)
    try:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
    prompt = _ensure_ids(prompt)
    if runtime is None and _TX_KV_WINDOW is None:
        _TX_KV_WINDOW = _learn_tx_window(_page_tx_cache if _page_tx_cache is not None else make_prompt_cache(lm_model))
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
            _page_tx_cache, _page_tx_cache_ids = make_prompt_cache(lm_model), []
        common = _lcp_words(_page_tx_cache_ids, prompt)
        if len(_page_tx_cache_ids) - common > 0 and not _page_tx_trim_or_reset(len(_page_tx_cache_ids) - common, common):
            _page_tx_cache, _page_tx_cache_ids, common = make_prompt_cache(lm_model), [], 0
        feed = prompt[common:]
        if not feed:
            if common <= 0 or not _page_tx_trim_or_reset(1, len(prompt) - 1):
                _page_tx_cache, _page_tx_cache_ids, common, feed = make_prompt_cache(lm_model), [], 0, prompt
            else:
                common -= 1; feed = prompt[common:]
        cache = _page_tx_cache
    else:
        cache = make_prompt_cache(model, max_kv_size=max(2048, min(_TX_KV_MAX, len(prompt) + gen_max + _TX_WINDOW_MARGIN)))
        feed = prompt
    out = []
    since = 0
    try:
        for r in lm_stream(model, tok, feed, max_tokens=gen_max, sampler=_sampler, prompt_cache=cache):
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


# A long paragraph (e.g. an arXiv intro) is one big text node. Translating it whole risks token/window
# truncation; translating each sentence in isolation loses pronouns, terminology, and flow. So we sentence-
# chunk it and translate the chunks SEQUENTIALLY, feeding each chunk the paragraph's already-translated
# chunks as recent_pairs — the model keeps terms/discourse consistent within the paragraph — then join.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？…])\s+|\n[ \t]*\n")


def _split_sentences(text: str):
    return [p.strip() for p in _SENT_SPLIT_RE.split(str(text or "").strip()) if p and p.strip()]


def _chunk_text(text: str, max_chars: int = None):
    """Group sentences into <= max_chars chunks without splitting a sentence (a lone over-long sentence is
    hard-split as a last resort). Returns the chunks in order."""
    max_chars = max_chars or PAGE_CHUNK_CHARS
    chunks, cur = [], ""
    for s in _split_sentences(text):
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= max_chars:
            cur += " " + s
        else:
            chunks.append(cur); cur = s
        while len(cur) > max_chars * 2:                 # a single giant sentence -> hard split (rare)
            chunks.append(cur[:max_chars]); cur = cur[max_chars:].strip()
    if cur:
        chunks.append(cur)
    return chunks or [str(text or "").strip()]


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


def run_ask(mode: str, transcript_text: str, question: str = "", target: str = "Korean", on_partial=None):
    """On-demand summary / Q&A over the running transcript (already-resident translator, fresh KV cache)."""
    msgs, max_toks = _ask_messages(mode, transcript_text, question, target)
    if _LM_IS_VLM:
        return _vlm_generate_text(msgs, max_toks, on_partial)
    mx.set_default_device(mx.gpu)
    try:
        prompt = lm_tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = lm_tok.apply_chat_template(msgs, add_generation_prompt=True)
    cache = make_prompt_cache(lm_model, max_kv_size=8192)   # fresh window; don't pollute the translation KV cache
    out, since = [], 0
    for r in lm_stream(lm_model, lm_tok, prompt, max_tokens=max_toks, sampler=_sampler, prompt_cache=cache):
        out.append(r.text); since += 1
        if on_partial is not None and since >= 4:
            since = 0; on_partial(_clean("".join(out)))
    return _clean("".join(out))


# --- Backend seam -------------------------------------------------------------------------------------
# Everything above (VAD, sentence assembly, scheduler, latency policy, number guard, prompt builders) is
# platform-independent. Only the GPU leaves — transcribe_pcm / translate_once / translate_page_batch_once / run_ask — plus warm and
# ASR-load are runtime-specific. On Apple Silicon they are the MLX functions above (default). With
# LCC_BACKEND=cuda we rebind these SAME module globals to backend_cuda's OpenAI-compatible HTTP client; the
# live loop passes them to executors by name, so it transparently drives a remote llama.cpp/vLLM instead.
# backend_cuda imports the shared prompt builders from THIS module lazily (at call time) — no import cycle.
if BACKEND == "cuda":
    import backend_cuda
    transcribe_pcm = backend_cuda.transcribe_pcm
    translate_once = backend_cuda.translate_once
    translate_page_batch_once = backend_cuda.translate_page_batch_once
    run_ask = backend_cuda.run_ask
    warm_mlx_selected = backend_cuda.warm_selected      # name kept for call-site compatibility; impl is an HTTP ping
    _ensure_asr_loaded = backend_cuda.ensure_asr_loaded
    print(f"[bridge] backend=cuda  chat={backend_cuda.CHAT_URL}  asr={backend_cuda.ASR_URL}", flush=True)
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
    vad = VADIterator(silero, threshold=VAD_THRESH[2], sampling_rate=SR,
                      min_silence_duration_ms=SEG_SILENCE_MS, speech_pad_ms=SPEECH_PAD_MS)
    cur_vad_level = 2                       # applied VAD level; rebuild VAD only when this actually changes
    sent_silence_cfg_ms = SENT_SILENCE_MS
    sent_silence_eff_ms = _lat_effective_sent_silence_ms(LATENCY_MODE_DEFAULT, SENT_SILENCE_MS)
    sent_sil_windows = max(1, (sent_silence_eff_ms - SEG_SILENCE_MS) // WINDOW_MS)   # tunable via config
    recent_pairs = collections.deque(maxlen=5)   # last few (source, target) finals -> consistency context
    dom_recent_pairs = collections.deque(maxlen=3)   # page translation consistency, kept out of caption history
    target_lang, context_hint = _normalize_target_lang("Korean"), ""   # set via {"type":"config"} from the client
    asr_engine = ASR_ENGINE
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
    mlx_lock = _MLX_DEVICE_LOCK   # global: serialize the single MLX device across ALL connections (was per-conn)

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
        return unit.id

    def clear_unit():
        unit.src = ""
        unit.start_ms = unit.end_ms = 0
        unit.id = None
        unit.rev = 0
        unit.pcm.clear(); unit.clauses = 0; unit.pure = True

    async def emit_source(text, unit_id, rev, start_ms, end_ms):
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
        # free-text context + user glossary source terms + auto-pinned terms -> ASR name biasing
        nonlocal asr_hint
        terms = ", ".join(s for s, _ in glossary_pairs)
        auto = ", ".join(s for s, _ in auto_glossary_pairs)
        asr_hint = "; ".join(x for x in (context_hint, terms, auto) if x)[:240]

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
        if aux_lm_ready():
            # Previews run on the aux translator's own pool/lock — they no longer steal the main model,
            # so let them fire even while finals are in flight (bandwidth-shared, UX-only).
            return latency_mode == "aggressive" or ((not in_speech) and work_q.empty())
        if active_tx_job is not None or final_backlog_count() > 0 or pending_final_jobs:
            return False
        if latency_mode == "balanced":
            return (not in_speech) and work_q.empty()
        if _is_sherpa_engine(asr_engine):
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

    async def enqueue_translation(source, final, unit_id, rev, start_ms, end_ms, reason):
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
                "queued_at": time.perf_counter(),
                "latency_mode": latency_mode,
                "epoch": translation_epoch,
            },
        ))
        if final:
            pending_final_jobs[trans_seq] = time.perf_counter()
        else:
            pending_preview_jobs[trans_seq] = time.perf_counter()

    def schedule_preview(source, unit_id, rev, start_ms, end_ms):
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

        async def delayed_preview(c_unit, c_rev, c_source, c_start, c_end):
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
            await enqueue_translation(c_source, False, c_unit, c_rev, c_start, c_end, "preview")

        preview_task = asyncio.create_task(delayed_preview(unit_id, rev, source, start_ms, end_ms))

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
                async with _AUX_LM_DEVICE_LOCK:
                    ko = await loop.run_in_executor(_aux_lm_pool, functools.partial(
                        translate_once, source, recent_ctx, target=target_lang, hint=context_hint,
                        register=register, glossary_pairs=effective_glossary(),
                        max_tokens=tx_max_tokens_for(False), stream_every=tx_stream_every_for(False),
                        profile="caption", custom=custom_prompt, runtime=_aux_runtime()))
            except Exception as e:
                print(f"[trans err aux] {e}", flush=True)
                preview_drop_count += 1
                scheduler_stats["preview_drop_tx_error"] += 1
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
            if not job["final"] and aux_lm_ready():
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
                    if not _is_sherpa_engine(asr_engine):
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
                                                "display_ms": _caption_read_ms(p),
                                            })
                                tpt = asyncio.create_task(_tx_pump())
                                try:
                                    ko = await loop.run_in_executor(
                                        _mlx_pool, translate_once, source, recent_ctx,
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
                                        _mlx_pool, translate_once, source, recent_ctx,
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
        # MLX lock; the in-process MLX audio model (granite/qwen3) runs on its OWN _asr_pool + _ASR_DEVICE_LOCK
        # so it OVERLAPS 26B translation on the single GPU (26B decode is bandwidth-bound; small ASR fills the
        # compute gap). Translation keeps mlx_lock + _mlx_pool; the two locks are disjoint so they run together.
        if _is_sherpa_engine(engine):
            return await loop.run_in_executor(_sherpa_pool, fn, *fn_args)
        async with _ASR_DEVICE_LOCK:
            return await loop.run_in_executor(_asr_pool, fn, *fn_args)

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
                                          unit.start_ms, unit.end_ms)
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
                    await emit_source(
                        commit_src, unit.id, unit.rev, unit.start_ms, unit.end_ms
                    )
                    await enqueue_translation(
                        commit_src, True, unit.id, unit.rev, unit.start_ms, unit.end_ms, "punct"
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
                await emit_source(unit.src, unit.id, unit.rev, unit.start_ms, unit.end_ms)
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
                    await enqueue_translation(
                        final_src, True, unit.id, unit.rev, unit.start_ms, unit.end_ms, reason
                    )
                    commit_carry["tail"], commit_carry["end_ms"] = _norm_words(final_src)[-3:], unit.end_ms
                    clear_unit()
                else:
                    schedule_preview(unit.src, unit.id, unit.rev, unit.start_ms, unit.end_ms)
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
                            latency_mode = _normalize_latency_mode(d.get("latencyMode"), latency_mode)
                            sent_sil_windows = sent_windows_for(sent_silence_cfg_ms)
                        if d.get("asrEngine") is not None:
                            requested_engine = _normalize_asr_engine(d.get("asrEngine"), asr_engine)
                            try:
                                await _on_asr_pool(requested_engine, _ensure_asr_loaded, requested_engine)
                                asr_engine = requested_engine
                            except Exception as e:
                                await send_json({"type": "err", "text": f"ASR 엔진 전환 실패({requested_engine}): {e}"})
                                print(f"[bridge] asr switch failed engine={requested_engine}: {e}", flush=True)
                        if d.get("vadLevel") is not None:
                            vad_level = _clamp_int(d.get("vadLevel"), 2, 0, 3)
                            if vad_level != cur_vad_level:    # rebuild only on real change — a live config push (glossary/slider/lang)
                                cur_vad_level = vad_level      # must not discard the in-progress utterance just by arriving
                                thr = VAD_THRESH.get(vad_level, 0.5)
                                vad = VADIterator(silero, threshold=thr, sampling_rate=SR,
                                                  min_silence_duration_ms=SEG_SILENCE_MS, speech_pad_ms=SPEECH_PAD_MS)
                                # Rebuilding resets VAD state, so flush any in-flight utterance as a soft clause
                                # first — otherwise changing the level mid-speech silently drops it.
                                if in_speech and voiced:
                                    await enqueue_work(("clause", bytes(voiced), speech_start_ms, audio_ms, True))
                                in_speech, voiced = False, bytearray(); preroll.clear()
                        if d.get("sentSilenceMs") is not None:           # 0 is valid, don't truthiness-skip
                            sent_silence_cfg_ms = _clamp_int(d.get("sentSilenceMs"), SENT_SILENCE_MS, 500, 5000)
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
                            accuracy_mode = _config_bool(d.get("accuracyMode"), accuracy_mode)
                        if d.get("termMemory") is not None:
                            term_memory_enabled = _config_bool(d.get("termMemory"), term_memory_enabled)
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
                                    ans = await loop.run_in_executor(_mlx_pool, run_ask, mode, tr, q, target_lang, a_partial)
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
                        verify_requested = _config_bool(d.get("verify"), False)
                        # Aux routing: short microbatches + per-item shorts go to the AUX translator when
                        # resident — page DOM stops contending with captions entirely (no busy deference).
                        # Long paragraphs and verify re-checks stay on the MAIN model (quality layer).
                        use_aux = aux_lm_ready() and not verify_requested
                        dom_lock = _AUX_LM_DEVICE_LOCK if use_aux else mlx_lock
                        dom_pool = _aux_lm_pool if use_aux else _mlx_pool
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
                                            runtime=(_aux_runtime() if use_aux else None),
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
                                        runtime=(_aux_runtime() if use_aux else None),
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
                                            out = await loop.run_in_executor(_mlx_pool, page_long)
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
                    elif d.get("type") == "warm":          # on-demand model warm-up (popup button)
                        t0 = time.perf_counter()
                        if not await wait_aux_translation_slot(1200):
                            await send_json({"type": "warmed", "sec": 0, "deferred": True})
                            print("[warm defer] live caption backlog has priority", flush=True)
                            continue
                        await _on_asr_pool(asr_engine, warm_mlx_selected, True, False, asr_engine)
                        async with mlx_lock:
                            await loop.run_in_executor(_mlx_pool, warm_mlx_selected, False, True, asr_engine)
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
    if BACKEND == "cuda":
        print(f"[bridge] ready (CUDA HTTP backend + 26B translate, latency={LATENCY_MODE_DEFAULT})", flush=True)
    else:
        asr_label = {"parakeet": "Parakeet ASR"}.get(ASR_ENGINE, "MLX ASR")
        print(f"[bridge] ready ({asr_label} + 26B translate, latency={LATENCY_MODE_DEFAULT})", flush=True)
        try:
            import mlx_lm as _mlxlm
            print(f"[bridge] mlx_lm={getattr(_mlxlm, '__version__', '?')} (KV reuse window learned lazily)", flush=True)
        except Exception:
            pass
    try:                                              # warm on the main thread (MLX: establishes streams + compiles; CUDA: pings endpoints)
        warm_mlx_selected(True, True)
        if BACKEND == "mlx":
            _reset_tx_cache()                         # real translation rebuilds it on the _mlx_pool worker thread
            _reset_page_tx_cache()
            if mx is not None:
                try: mx.clear_cache()
                except Exception: pass
        print("[bridge] warmed", flush=True)
    except Exception as e:
        if BACKEND == "mlx":
            _reset_tx_cache()
            _reset_page_tx_cache()
        print("[bridge] warm skip:", e, flush=True)
    asyncio.run(main())
