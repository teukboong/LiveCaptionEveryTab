"""Runtime model/backend ownership for the live-caption bridge.

This module owns backend/env normalization, model registries, lazy model loading,
MLX/ASR pools and locks, and the mutable runtime model state. Importing it must
not probe hardware or load model weights; warm/load entrypoints do that lazily.
"""
import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor

from silero_vad import load_silero_vad

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
# LCC_BACKEND. Only the backend leaves (transcribe/translate/ask) differ; everything else is shared. See
# the "Backend seam" block lower in this file.
_BACKENDS = ("mlx", "cuda", "fake")

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
# Models load lazily via load_models() (called from __main__) so the pure prompt-building helpers
# (_tx_system / _fewshot / translate_once) can be imported by benches/tests without pulling ~50GB of
# weights. The bridge loads everything; a translation-only bench can skip ASR/VAD.
lm_model = lm_tok = silero = _sampler = parakeet_asr = None
mlxa_model = None            # mlx-audio ASR model instance (granite/qwen3); one at a time, reloaded on engine switch
mlxa_loaded_engine = None
whisper_loaded_repo = None   # the whisper repo whose model is warm (mlx_whisper caches the model by path)


def _diarize():
    """Lazy diarize-module import: sherpa/numpy stay out of model-free test imports."""
    import diarize
    return diarize


def _require_mlx():
    if MLX_IMPORT_ERROR is not None:
        raise RuntimeError(
            "MLX backend unavailable. Install the MLX dependencies for the local live-caption backend."
        ) from MLX_IMPORT_ERROR


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


def load_models(asr=True, lm=True, vad=True, asr_loader=None):
    global ASR_ENGINE, lm_model, lm_tok, silero, _sampler, parakeet_asr, _LM_IS_VLM
    global aux_lm_model, aux_lm_tok, _AUX_LM_IS_VLM
    if BACKEND == "fake":
        if vad and silero is None:
            import backend_fake
            silero = backend_fake.FAKE_SILERO_SENTINEL
        return
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
            if asr_loader is None:
                raise RuntimeError("ASR loader is required when loading local ASR")
            try:
                asr_loader(ASR_ENGINE)
            except Exception as e:
                if _is_sherpa_engine(ASR_ENGINE):
                    print(f"[bridge] {ASR_ENGINE} ASR unavailable ({e}); falling back to granite ASR", flush=True)
                    ASR_ENGINE = "granite"
                    asr_loader(ASR_ENGINE)
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
