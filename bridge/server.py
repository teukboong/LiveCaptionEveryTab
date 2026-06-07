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
import asyncio, json, collections, difflib, os, re, time
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
_ASR_ENGINES = _MLXA_ENGINES + _SHERPA_ENGINES

def _is_sherpa_engine(engine) -> bool:
    return engine in _SHERPA_ENGINES

def _is_mlxa_engine(engine) -> bool:
    return engine in _MLXA_ENGINES

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

def _translation_context_signature(target, register, hint, glossary_pairs):
    return (
        _normalize_target_lang(target),
        str(register or "casual"),
        str(hint or ""),
        tuple(glossary_pairs or ()),
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
DOM_TX_MAX_CHARS = int(os.environ.get("LCC_DOM_TX_MAX_CHARS", "900"))
DOM_TX_MAX_TOTAL_CHARS = int(os.environ.get("LCC_DOM_TX_MAX_TOTAL_CHARS", "4000"))


def _dom_translate_items(payload, *, max_items=DOM_TX_MAX_ITEMS, max_chars=DOM_TX_MAX_CHARS,
                         max_total_chars=DOM_TX_MAX_TOTAL_CHARS):
    """Normalize untrusted page-translation items from the extension before they reach the model."""
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
        out.append({"id": item_id, "text": text})
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

# --- Translation-model tiering: size the translator to AVAILABLE memory, not total -------------------------------
# The translator weights dominate the footprint, so picking by *total* RAM is blind to what's already resident and
# has to assume the worst (over-conservative). Instead size to *free* memory (idle VRAM): on CUDA that's nvidia-smi
# free VRAM; on MLX (unified memory) it's min(Metal working-set budget − active, OS-available). The largest tier
# that fits with HEADROOM to spare wins — so a busy 32GB box correctly steps down to avoid swap, while an idle
# 24GB box can still run full. Precedence:
#   LCC_LM_MODEL (explicit id)  >  LCC_LM_TIER (full|mid|lite)  >  auto-detect by free memory.
# Resolution is LAZY (done in load_models/_ensure_asr_loaded, NOT at import) so `import server` in tests never
# probes hardware or prints. Effective on MLX (selects LM_MODEL); on CUDA the GGUF is chosen by cuda/serve_llama.sh
# and the tier here only labels the OpenAI 'model' field + logs which GGUF tier to serve.
_LM_TIERS = {
    "mlx": {   # Gemma 4, Apache-2.0. full=mlx_lm. mid/lite are Gemma-4 nano (multimodal) → need mlx_vlm loader (pending).
        "full": "mlx-community/gemma-4-26b-a4b-it-4bit",                              # ~14GB, mlx_lm
        "mid":  os.environ.get("LCC_LM_MID_MLX",  "mlx-community/gemma-4-e4b-it-4bit"),   # ~5GB  (nano; mlx_vlm)
        "lite": os.environ.get("LCC_LM_LITE_MLX", "mlx-community/gemma-4-e2b-it-4bit"),   # ~3.2GB (nano; mlx_vlm)
    },
    "cuda": {  # llama.cpp serves the .gguf chosen at launch; this is just the OpenAI 'model' label
        "full": os.environ.get("LCC_LM_FULL_CUDA", "gemma-4-26b-a4b-it-qat-q4_0"),
        "mid":  os.environ.get("LCC_LM_MID_CUDA",  "gemma-4-e4b-it-qat-q4_0"),
        "lite": os.environ.get("LCC_LM_LITE_CUDA", "gemma-4-e2b-it-qat-q4_0"),
    },
}
# Resident footprint per tier (GB) = translator + ASR + KV/activation slack. Conservative-ish; tune via env.
# HEADROOM = OS/browser slack kept free so a growing browser doesn't push the resident model into swap.
_LM_TIER_NEED = {   # resident GB (translator + ASR + KV). measured translator-only: 26B ~14 / e4b 5.9 / e2b 4.3
    "full": _clamp_float(os.environ.get("LCC_LM_NEED_FULL"), 18.0, 4.0, 512.0),
    "mid":  _clamp_float(os.environ.get("LCC_LM_NEED_MID"),   8.0, 2.0, 512.0),
    "lite": _clamp_float(os.environ.get("LCC_LM_NEED_LITE"),  6.0, 1.0, 512.0),
}
_LM_TIER_HEADROOM_GB = _clamp_float(os.environ.get("LCC_LM_HEADROOM_GB"), 4.0, 0.0, 64.0)
_LM_TIER_ORDER = ("full", "mid", "lite")
_ASR_QWEN3_FULL = "Qwen/Qwen3-ASR-1.7B"
_ASR_QWEN3_LITE = "Qwen/Qwen3-ASR-0.6B"   # lite tier shrinks ASR too so the whole stack fits small machines

def _normalize_tier(value):
    t = str(value or "").strip().lower()
    aliases = {"large": "full", "big": "full", "max": "full", "medium": "mid", "small": "lite", "min": "lite"}
    t = aliases.get(t, t)
    return t if t in _LM_TIER_ORDER else ""

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

def _auto_tier():
    """Largest tier whose footprint + headroom fits in free memory; falls back to lite, or full if unprobable."""
    avail = _free_mem_gb_cuda() if BACKEND == "cuda" else _free_mem_gb_mlx()
    if avail is None:
        print("[bridge] tier: free-memory probe failed -> 'full' (set LCC_LM_TIER to pin)", flush=True)
        return "full"
    for t in _LM_TIER_ORDER:
        if avail >= _LM_TIER_NEED[t] + _LM_TIER_HEADROOM_GB:
            print(f"[bridge] tier={t}  (avail≈{avail:.1f}GB ≥ {_LM_TIER_NEED[t]:.0f}+{_LM_TIER_HEADROOM_GB:.0f}GB headroom; "
                  f"pin with LCC_LM_TIER)", flush=True)
            return t
    print(f"[bridge] tier=lite  (avail≈{avail:.1f}GB below mid threshold)", flush=True)
    return "lite"

# Lazy-resolved config (empty at import; filled by _finalize_model_config at first warm — see note above).
LM_TIER = _normalize_tier(os.environ.get("LCC_LM_TIER"))     # "" until resolved
LM_MODEL = os.environ.get("LCC_LM_MODEL", "")               # "" -> derived from tier
_LM_RESOLVED = False
_LM_IS_VLM = False   # set True at load when the translator is a Gemma-4 nano (multimodal) loaded via mlx_vlm

# mlx-audio audio-LLM ASR engines (granite/qwen3): native punctuation + multilingual, loaded via mlx_audio.
MLXA_REPOS = {
    "granite": os.environ.get("LCC_GRANITE_MODEL", "ibm-granite/granite-speech-4.1-2b"),
    "qwen3":   os.environ.get("LCC_QWEN3_MODEL", ""),       # "" -> 1.7B (full/mid) or 0.6B (lite), set at warm
}

def _finalize_model_config():
    """Resolve translator tier+model and the ASR repo from available memory, once. Lazy (called at warm, not at
    import) so tests that `import server` never probe hardware. Idempotent."""
    global LM_TIER, LM_MODEL, _LM_RESOLVED
    if _LM_RESOLVED:
        return
    _LM_RESOLVED = True
    if not LM_TIER:
        LM_TIER = _auto_tier()
    else:
        print(f"[bridge] tier={LM_TIER} (LCC_LM_TIER)", flush=True)
    if not LM_MODEL:
        LM_MODEL = _LM_TIERS[BACKEND][LM_TIER]
    if not MLXA_REPOS["qwen3"]:
        MLXA_REPOS["qwen3"] = _ASR_QWEN3_LITE if LM_TIER == "lite" else _ASR_QWEN3_FULL
    print(f"[bridge] translate backend={BACKEND} tier={LM_TIER} model={LM_MODEL}", flush=True)
    if BACKEND == "cuda":
        print(f"[bridge] (cuda) translation GGUF is chosen by cuda/serve_llama.sh — serve the '{LM_TIER}' tier "
              f"({_LM_TIERS['cuda'][LM_TIER]})", flush=True)

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
WS_TOKEN = os.environ.get("LCC_WS_TOKEN", "lcc-local-extension-v1")
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


def _require_mlx():
    if MLX_IMPORT_ERROR is not None:
        raise RuntimeError(
            "MLX backend unavailable. Install the MLX dependencies for the local live-caption backend."
        ) from MLX_IMPORT_ERROR


def _ensure_asr_loaded(engine: str):
    global parakeet_asr, mlxa_model, mlxa_loaded_engine
    _finalize_model_config()   # resolve MLXA_REPOS (lite tier shrinks ASR) before the first load
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

    raise RuntimeError(f"unknown ASR engine: {engine}")


def load_models(asr=True, lm=True, vad=True):
    global ASR_ENGINE, lm_model, lm_tok, silero, _sampler, parakeet_asr, _LM_IS_VLM
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
            print(f"[bridge] loading translator ({LM_TIER}: {LM_MODEL})…", flush=True)
            try:
                lm_model, lm_tok = lm_load(LM_MODEL)
                _LM_IS_VLM = False
            except Exception as e:
                # Gemma-4 nano (E4B/E2B, mid/lite) ships as a multimodal checkpoint (language_model.* prefix) the
                # mlx_lm text loader can't read — but it loads via mlx_vlm. Auto-fall back so the small tiers work.
                if "not in model" in str(e) and vlm_load is not None:
                    print(f"[bridge] {LM_MODEL} is multimodal (Gemma-4 nano) -> loading via mlx_vlm", flush=True)
                    lm_model, lm_tok = vlm_load(LM_MODEL)
                    _LM_IS_VLM = True
                else:
                    raise
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
_TX_KV_MAX = int(os.environ.get("LCC_TX_KV_MAX_TOKENS", "4096"))   # cap reuse to a bounded prompt window
_TX_KV_WINDOW = None        # min RotatingKVCache sliding window (Gemma 4); reuse must stay inside it (lazy)
_TX_GEN_MAX = max(1, int(os.environ.get("LCC_TX_GEN_MAX_TOKENS", "64")))   # caption translation cap; ask/summary uses its own chat cap
_TX_WINDOW_MARGIN = max(0, int(os.environ.get("LCC_TX_WINDOW_MARGIN", "8")))   # keep reuse a few tokens clear of the window edge
TX_PROFILE = os.environ.get("LCC_TX_PROFILE", "quality").strip().lower()
TX_FEWSHOT_MAX = max(0, int(os.environ.get("LCC_TX_FEWSHOT_MAX", "0" if TX_PROFILE in ("fast", "compact", "latency") else "3")))
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
        return True                         # CLI smoke tests usually omit Origin.
    if origin.startswith("chrome-extension://"):
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
    # "I talked to 김수영 about the demo") must NOT be treated as Korean (would skip translation).
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


def _fewshot(target: str, register: str, src_lang: str):
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


def _tx_system(target: str, register: str = "casual", hint: str = "", glossary_pairs=()) -> str:
    if TX_COMPACT_PROMPT:
        s = (f"Translate live speech into natural {target}. Preserve meaning, tone, and names. "
             f"If the line is incomplete, translate only what is present. ")
        s += _FAST_REGISTER_TONE.get(register, "")
        s += _glossary_clause(glossary_pairs)
        if hint:
            s += f"Consistent names/terms: {hint}. "
        return s + f"Output only {target}."
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


def _translate_messages(text, recent_pairs=(), target="Korean", hint="", register="casual", glossary_pairs=()):
    """The chat-message list for one clause translation: register-aware system instruction + source-language-
    matched few-shot anchors + the model's recent (source->target) renderings (consistency) + the line itself.
    Shared by the MLX and CUDA backends so both produce byte-identical prompts (same translation regardless of
    runtime). Each backend applies its own chat template (MLX: apply_chat_template; CUDA: server-side)."""
    msgs = [{"role": "system", "content": _tx_system(target, register, hint, glossary_pairs)}]
    for ex_src, ex_tgt in _fewshot(target, register, _src_lang(text))[:TX_FEWSHOT_MAX]:   # source-lang-matched style anchors
        msgs += [{"role": "user", "content": ex_src}, {"role": "assistant", "content": ex_tgt}]
    for s, t in recent_pairs:                                   # the model's own recent renderings -> consistency
        msgs += [{"role": "user", "content": s}, {"role": "assistant", "content": t}]
    msgs.append({"role": "user", "content": text})
    return msgs


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

def _tx_trim_or_reset(n, expected_after):
    """Trim exactly n tokens and VERIFY (count + post-offset). Reset the persistent cache and return False
    on any failure — Gemma 4 sliding layers (RotatingKVCache) go non-trimmable once offset >= sliding_window
    and trim_prompt_cache then silently returns 0, which would desync _tx_cache_ids from the real cache."""
    if n <= 0:
        return True
    if _tx_cache is None or not can_trim_prompt_cache(_tx_cache):
        _reset_tx_cache(); return False
    try:
        got = trim_prompt_cache(_tx_cache, n)
    except Exception:
        _reset_tx_cache(); return False
    if got != n or _tx_cache_offset(_tx_cache) != expected_after:
        _reset_tx_cache(); return False
    return True

def _usable_tx_partial(s):
    # a streamed KO partial good enough to commit as a degraded caption (vs falling back to the source line)
    s = (s or "").strip()
    if len(s) < 8:
        return False
    if s.endswith(("(", "[", "{", ",", "…", "、", "，", "·")):
        return False
    return True

def _vlm_generate_text(msgs, gen_max, on_update=None):
    """mlx_vlm translation path for Gemma-4 nano tiers (mid/lite). Text-only chat -> mlx_vlm.generate. No
    KV-reuse / token streaming (those are mlx_lm-specific) -> a single final update. lm_model is the vlm model,
    lm_tok the processor. Same _translate_messages prompt as the mlx_lm path, so output is consistent."""
    mx.set_default_device(mx.gpu)
    proc = lm_tok
    try:
        prompt = proc.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
    except Exception:
        prompt = proc.tokenizer.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
    res = vlm_generate(lm_model, proc, prompt, max_tokens=int(gen_max), verbose=False)
    text = _clean(getattr(res, "text", None) or (res if isinstance(res, str) else str(res)))
    if on_update is not None and text:
        on_update(text)
    return text


def translate_once(text: str, recent_pairs=(), target: str = "Korean", hint: str = "",
                   register: str = "casual", glossary_pairs=(), on_update=None, kv_reuse=None,
                   max_tokens=None, stream_every=None):
    """Stateless per-clause translation, primed for quality: a strong register-aware instruction,
    source-language-matched few-shot anchors, a pinned glossary, and the last few (source -> target)
    pairs as conversation context so terminology/tone stay consistent across the stream. Re-callable on
    a growing clause (EN->KO reverses word order, so we re-translate the whole clause). Runs on _mlx_pool
    (single worker -> the module-level _tx_cache has no race)."""
    global _tx_cache, _tx_cache_ids, _TX_KV_WINDOW
    msgs = _translate_messages(text, recent_pairs, target, hint, register, glossary_pairs)
    if _LM_IS_VLM:
        return _vlm_generate_text(msgs, max(1, int(max_tokens or _TX_GEN_MAX)), on_update)
    mx.set_default_device(mx.gpu)
    try:
        prompt = lm_tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = lm_tok.apply_chat_template(msgs, add_generation_prompt=True)
    prompt = _ensure_ids(prompt)
    gen_max = max(1, int(max_tokens or _TX_GEN_MAX))
    if _TX_KV_WINDOW is None:                              # learn the sliding window once (fail-safe on unknown caches)
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
    reuse = use_reuse and (len(prompt) + gen_max + _TX_WINDOW_MARGIN) <= min(_TX_KV_MAX, _TX_KV_WINDOW)
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
        cache = make_prompt_cache(lm_model, max_kv_size=2048)
        feed = prompt
    out, since = [], 0
    try:
        every = max(1, int(stream_every or 4))
        for r in lm_stream(lm_model, lm_tok, feed, max_tokens=gen_max, sampler=_sampler, prompt_cache=cache):
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
                max_tokens=max_tokens, stream_every=stream_every,
            )
        elif actual > len(prompt):
            if _tx_trim_or_reset(actual - len(prompt), len(prompt)):   # drop generated suffix -> prompt-only
                _tx_cache_ids = list(prompt)
            # else: helper already reset the cache; the returned output is still valid
        else:
            _tx_cache_ids = list(prompt)
    return _clean("".join(out))


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
# platform-independent. Only the three GPU leaves — transcribe_pcm / translate_once / run_ask — plus warm and
# ASR-load are runtime-specific. On Apple Silicon they are the MLX functions above (default). With
# LCC_BACKEND=cuda we rebind these SAME module globals to backend_cuda's OpenAI-compatible HTTP client; the
# live loop passes them to executors by name, so it transparently drives a remote llama.cpp/vLLM instead.
# backend_cuda imports the shared prompt builders from THIS module lazily (at call time) — no import cycle.
if BACKEND == "cuda":
    import backend_cuda
    transcribe_pcm = backend_cuda.transcribe_pcm
    translate_once = backend_cuda.translate_once
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
    asr_hint = ""               # context_hint + glossary source terms, fed to the ASR prompt (recomputed on config)
    accuracy_mode = False       # 2-pass: re-transcribe the whole sentence's audio at commit (cleaner finals, +~0.7s)
    translation_epoch = 0        # bumps when target/register/hints change so old-language jobs cannot render later
    preroll = collections.deque(maxlen=PREROLL_WINDOWS)   # pre-onset audio prepended on speech start
    leftover, voiced, in_speech, sil_windows = b"", bytearray(), False, 0
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
        if active_tx_job is not None or final_backlog_count() > 0 or pending_final_jobs:
            return False
        if pending_preview_jobs:
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

    async def translation_loop():
        nonlocal preview_drop_count, active_tx_job
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
                    target_lang, register, context_hint, tuple(glossary_pairs),
                    tuple(recent_ctx), source,
                )
                ko = None
                hit = False
                if job["final"]:
                    prev = preview_results.get(job["unit_id"])
                    if prev and _preview_promotable(prev["source"], source):
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
                                        target_lang, context_hint, register, list(glossary_pairs), _on_tx,
                                        None, tx_max_tokens_for(True), tx_stream_every_for(True))
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
                                        target_lang, context_hint, register, list(glossary_pairs), None,
                                        None, tx_max_tokens_for(job["final"]), tx_stream_every_for(job["final"]))
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
        return await _on_asr_pool(asr_engine, transcribe_pcm, audio, asr_hint, asr_engine)

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
                        if _active_ws is not None and _active_ws is not ws:   # single-client is the intended model
                            print("[bridge] WARN: a 2nd client connected while one is active — they share ONE MLX "
                                  "device + translator KV cache; expect degraded latency.", flush=True)
                        _active_ws = ws
                    elif not authed:
                        await ws.close(code=1008, reason="hello required")
                        break
                    elif d.get("type") == "config":
                        prev_tx_sig = _translation_context_signature(target_lang, register, context_hint, glossary_pairs)
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
                        if d.get("accuracyMode") is not None:
                            accuracy_mode = _config_bool(d.get("accuracyMode"), accuracy_mode)
                        # ASR biasing hint = free-text context + glossary source terms (helps the model spell names)
                        _gloss_terms = ", ".join(s for s, _ in glossary_pairs)
                        asr_hint = "; ".join(x for x in (context_hint, _gloss_terms) if x)[:240]
                        new_tx_sig = _translation_context_signature(target_lang, register, context_hint, glossary_pairs)
                        if new_tx_sig != prev_tx_sig:
                            translation_epoch += 1
                            recent_pairs.clear()
                            translation_cache.clear()
                            preview_results.clear()
                            pending_preview_jobs.clear()
                            pending_final_jobs.clear()
                            print(f"[cfg] translation context reset epoch={translation_epoch} target={target_lang}", flush=True)
                        print(f"[cfg] vad={d.get('vadLevel')} sentSil={sent_silence_cfg_ms}/{effective_sent_silence_ms(sent_silence_cfg_ms)} target={target_lang} "
                              f"reg={register} asr={asr_engine} latency={latency_mode} acc={accuracy_mode} gloss={len(glossary_pairs)} "
                              f"hint={asr_hint[:30]!r}", flush=True)
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
                        sent = 0
                        print(f"[dom-tx] request={request_id} items={len(items)}", flush=True)
                        for item in items:
                            if not await wait_aux_translation_slot(1200):
                                await send_json({"type": "dom_translate_busy", "request_id": request_id, "retry_ms": 1800})
                                print("[dom-tx defer] live caption backlog has priority", flush=True)
                                break
                            source = item["text"]
                            try:
                                async with mlx_lock:
                                    out = await loop.run_in_executor(
                                        _mlx_pool, translate_once, source, list(dom_recent_pairs),
                                        target_lang, context_hint, register, glossary_pairs
                                    )
                                out = _clean(out)
                                sent += 1
                                if out and out != source:
                                    dom_recent_pairs.append((source[:160], out[:160]))
                                await send_json({
                                    "type": "dom_translate_result",
                                    "request_id": request_id,
                                    "item_id": item["id"],
                                    "source": source,
                                    "target": out,
                                })
                            except Exception as e:
                                print(f"[dom-tx err] {e}", flush=True)
                                await send_json({
                                    "type": "dom_translate_err",
                                    "request_id": request_id,
                                    "item_id": item["id"],
                                    "source": source,
                                    "text": str(e)[:240],
                                })
                        await send_json({"type": "dom_translate_done", "request_id": request_id, "count": sent})
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
            el = time.perf_counter() - t_conn
            rate = (100 * nospeech_count / seg_count) if seg_count else 0
            print(f"[bridge] client disconnected — {seg_count} segs, {nospeech_count} no-speech "
                  f"({rate:.0f}%), preview_drops={preview_drop_count}, cache_hits={cache_hit_count}, {el:.0f}s", flush=True)
            if _active_ws is ws:        # release single-active registration (best-effort; only drives the WARN above)
                _active_ws = None


async def main():
    # ping_interval=None: heavy inference can starve the loop and trip keepalive -> drop. Disable it.
    async with websockets.serve(handle, HOST, PORT, max_size=MAX_WS_MSG_BYTES, ping_interval=None):
        print(f"[bridge] ready  ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
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
            if mx is not None:
                try: mx.clear_cache()
                except Exception: pass
        print("[bridge] warmed", flush=True)
    except Exception as e:
        if BACKEND == "mlx":
            _reset_tx_cache()
        print("[bridge] warm skip:", e, flush=True)
    asyncio.run(main())
