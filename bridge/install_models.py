#!/usr/bin/env python3
"""Download ONE chosen model for a role (transcription / translation) and wire it as active.

Replaces the old tier downloader: selection is by **role + model** (a curated id OR a raw HF repo for
custom input), not full/mid/lite. The curated registry is owned by bridge/server.py (LM_MODELS /
ASR_MODELS) — no model id is hardcoded here.

  install_models.py --role asr|lm --model <id-or-hf-repo> [--backend mlx|cuda] [--dry-run]

- lm  (translation): download the model; on success pin LCC_LM_MODEL in the repo .env.
- asr (transcription): download the engine's model. **Whisper** needs a 6bit (mlx) / q6 gguf (cuda)
  artifact — prefer a ready quantized download, else quantize locally (mlx: in-process; cuda: the
  cuda/quantize_whisper_q6.sh helper) — and pin LCC_WHISPER_MODEL so the bridge loads it.
- Runtime loader deps are ensured per model: whisper -> mlx-whisper, Gemma nano (e4b/e2b) -> mlx_vlm.

Writes JSON progress to ~/.lcc-install.json so the popup can poll. Exposes is_installed(role, model,
backend) for the native host's models_status. Custom (non-registry) models download as-is — no
auto-quantization is guaranteed for them.
"""
import glob
import json
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))          # .../bridge
ROOT = os.path.dirname(HERE)                                # repo root
STATUS = os.path.join(os.path.expanduser("~"), ".lcc-install.json")
ENV_FILE = os.path.join(ROOT, ".env")
CUDA_ENV = os.environ.get("LCC_CUDA_ENV", os.path.join(os.path.expanduser("~"), ".lcc-cuda.env"))
CUDA_MODEL_ROOT = os.environ.get("LCC_MODEL_ROOT", os.path.join(os.path.expanduser("~"), "models", "live-caption-cuda8"))
# Where locally-quantized whisper artifacts live (deterministic so is_installed() can find them again).
WHISPER_QUANT_ROOT = os.environ.get(
    "LCC_WHISPER_QUANT_ROOT", os.path.join(os.path.expanduser("~"), ".cache", "live-caption", "whisper-mlx-6bit"))
ROLES = ("asr", "lm")


def write_status(**kw):
    kw.setdefault("ts", int(time.time()))
    tmp = STATUS + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(kw, f)
        os.replace(tmp, STATUS)   # atomic so the poller never reads a half-written file
    except Exception:
        pass


def set_env_kv(path, key, val):
    """Replace/append KEY=val in an env file, preserving other lines."""
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = [ln for ln in f.read().splitlines() if not ln.strip().startswith(key + "=")]
    lines.append(f"{key}={val}")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def _server():
    sys.path.insert(0, HERE)
    import server as s
    return s


# --- registry lookup (single source of truth is server.py) ---------------------------------------------
def _registry(role, backend):
    s = _server()
    return s.asr_models(backend) if role == "asr" else s.lm_models(backend)


def find_entry(role, model, backend):
    """The curated registry entry whose id (or repo) matches `model`, else None (custom raw repo)."""
    model = str(model or "").strip()
    for m in _registry(role, backend):
        if model == m.get("id") or model == m.get("repo") or model == m.get("served"):
            return m
    return None


def _download_repo(repo, **kw):
    from huggingface_hub import snapshot_download
    return snapshot_download(repo, **kw)


_WEIGHT_EXTS = (".safetensors", ".gguf", ".bin", ".npz", ".onnx")


def _hf_cached(repo):
    """True if `repo` has a COMPLETE snapshot in the local HF cache (no network). snapshot_download
    creates snapshots/<rev> immediately and links files in as each finishes — a download interrupted
    at 0% still leaves a non-empty snapshot dir, so mere presence must not count as installed."""
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:
        HF_HUB_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    root = os.path.join(HF_HUB_CACHE, "models--" + str(repo).replace("/", "--"))
    snaps = os.path.join(root, "snapshots")
    if not os.path.isdir(snaps):
        return False
    if glob.glob(os.path.join(root, "blobs", "*.incomplete")):
        return False                       # a file of this repo is mid-download (or its download was killed)
    for rev in os.scandir(snaps):
        if not rev.is_dir():
            continue
        entries = [f for f in glob.glob(os.path.join(rev.path, "**", "*"), recursive=True)
                   if os.path.islink(f) or os.path.isfile(f)]
        if not entries or any(not os.path.exists(f) for f in entries):   # dangling symlink = interrupted
            continue
        # config/tokenizer files land first — only a present weights file means the model is usable
        if any(f.lower().endswith(_WEIGHT_EXTS) for f in entries):
            return True
    return False


def _cuda_whisper_gguf():
    return os.path.join(CUDA_MODEL_ROOT, "asr-gguf-q6", "whisper", "whisper-large-v3-q6_k.gguf")


def _looks_quantized_mlx(repo):
    r = str(repo).lower()
    return any(k in r for k in ("6bit", "q6", "8bit", "4bit", "3bit", "int8", "int4"))


def _ensure_dep(modname, pip_name, dry):
    """Best-effort: make sure a loader dependency is importable; pip-install it if missing."""
    try:
        __import__(modname)
        return True
    except Exception:
        if dry:
            return False
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", pip_name], check=True)
            __import__(modname)
            return True
        except Exception as e:
            raise RuntimeError(f"required dependency {pip_name} is missing and could not be installed: {e}")


# --- whisper quantization (Mac: mlx 6bit) --------------------------------------------------------------
def _whisper_quant_dir(src_repo, bits=6):
    safe = str(src_repo).replace("/", "--")
    return os.path.join(WHISPER_QUANT_ROOT, f"{safe}-{bits}bit")


def _quantize_whisper_mlx(src_repo, out_dir, bits=6, group_size=64):
    """Quantize a whisper checkpoint to `bits`-bit in mlx_whisper's on-disk format (config.json with a
    'quantization' key + weights.safetensors). The bridge loads this dir via mlx_whisper (load_model
    re-applies the quantization). Local-quantize FALLBACK — used only when no prequant is available."""
    import mlx.core as mx
    import mlx.nn as nn
    from mlx.utils import tree_flatten
    from mlx_whisper.load_models import load_model
    src = _download_repo(src_repo)
    with open(os.path.join(src, "config.json")) as f:
        config = json.load(f)
    config.pop("model_type", None)
    config.pop("quantization", None)
    model = load_model(src_repo, dtype=mx.float16)
    nn.quantize(model, group_size=group_size, bits=bits)
    os.makedirs(out_dir, exist_ok=True)
    mx.save_safetensors(os.path.join(out_dir, "weights.safetensors"), dict(tree_flatten(model.parameters())))
    config["quantization"] = {"group_size": group_size, "bits": bits}
    with open(os.path.join(out_dir, "config.json"), "w") as f:
        json.dump(config, f)
    return out_dir


def _resolve_whisper_mlx(src_repo, dry):
    """Return (path_or_repo the bridge should load, did_quantize). Prefer a ready quantized download;
    else quantize the source locally to 6bit."""
    if _looks_quantized_mlx(src_repo):                       # already a 6bit/quant repo -> just fetch it
        if not dry:
            _download_repo(src_repo)
        return src_repo, False
    out_dir = _whisper_quant_dir(src_repo, 6)
    if os.path.isfile(os.path.join(out_dir, "config.json")):  # we already quantized it before
        return out_dir, False
    if dry:
        return out_dir, True
    _ensure_dep("mlx_whisper", "mlx-whisper", dry)
    _quantize_whisper_mlx(src_repo, out_dir, bits=6)
    return out_dir, True


# --- installed-state (for the native host's models_status) ---------------------------------------------
def is_installed(role, model, backend):
    """True if the chosen model's artifact is already present locally (no download)."""
    backend = "cuda" if str(backend).lower() in ("cuda", "nvidia", "gpu", "http") else "mlx"
    entry = find_entry(role, model, backend)
    repo = (entry or {}).get("repo") or str(model)
    if entry and entry.get("tx_http"):
        # external llama.cpp diffusion server: installed = its launcher + a local GGUF in its models dir
        dg = os.environ.get("LCC_DG_DIR", os.path.expanduser("~/llama.cpp-diffusion"))
        return os.access(os.path.join(dg, "run-diffusion-server.sh"), os.X_OK) and bool(
            glob.glob(os.path.join(dg, "models", "*.gguf")))
    if backend == "cuda":
        # cuda whisper is the only artifact under CUDA_MODEL_ROOT (a fixed q6 gguf — mirror install_asr);
        # everything else lands in the HF cache. A root-wide *.gguf glob marked EVERY cuda model installed
        # the moment any one gguf existed.
        if entry and entry.get("engine") == "whisper":
            return os.path.isfile(_cuda_whisper_gguf())
        return _hf_cached(repo)
    if entry and entry.get("engine") == "whisper":
        return _looks_quantized_mlx(repo) and _hf_cached(repo) or os.path.isfile(
            os.path.join(_whisper_quant_dir(repo, 6), "config.json"))
    return _hf_cached(repo)


# --- install: translation (lm) -------------------------------------------------------------------------
def install_lm(model, backend, dry):
    entry = find_entry("lm", model, backend)
    repo = (entry or {}).get("repo") or str(model)
    label = (entry or {}).get("label") or model
    if entry and entry.get("tx_http"):
        # the external diffusion server needs a llama.cpp build, not just weights — manual setup
        raise RuntimeError(f"{label}: 자동 설치 미지원 — llama.cpp diffusion 브랜치 빌드 + GGUF를 "
                           f"LCC_DG_DIR(기본 ~/llama.cpp-diffusion)에 준비하세요")
    write_status(backend=backend, role="lm", model=model, done=False, ok=True, total=1, index=0,
                 current=f"Translate · {label}  ({repo})", pid=os.getpid())
    if backend == "cuda":
        if dry:
            gguf = os.path.join(CUDA_MODEL_ROOT, "lm", f"<{model}>.gguf")
        else:
            path = _download_repo(repo, allow_patterns=["*.gguf"])
            ggufs = sorted(glob.glob(os.path.join(path, "**", "*.gguf"), recursive=True))
            if not ggufs:
                raise RuntimeError(f"no .gguf found in {repo}")
            main = [g for g in ggufs if "mmproj" not in os.path.basename(g).lower()] or ggufs
            gguf = main[0]
        served = (entry or {}).get("served") or str(model)
        if not dry:
            set_env_kv(CUDA_ENV, "LCC_LLAMA_GGUF", gguf)
            set_env_kv(ENV_FILE, "LCC_LM_MODEL", served)
        write_status(backend=backend, role="lm", model=model, done=True, ok=True, total=1, index=1,
                     current="완료" if not dry else "(dry-run)", gguf=gguf)
        return {"backend": backend, "role": "lm", "model": served, "gguf": gguf, "cuda_env": CUDA_ENV}
    # mlx
    if not dry:
        # Gemma nano (e4b/e2b) translators load via mlx_vlm — make sure that loader dep is present.
        if "e4b" in repo.lower() or "e2b" in repo.lower():
            _ensure_dep("mlx_vlm", "mlx-vlm", dry)
        _download_repo(repo)
        set_env_kv(ENV_FILE, "LCC_LM_MODEL", repo)
    write_status(backend=backend, role="lm", model=model, done=True, ok=True, total=1, index=1,
                 current="완료" if not dry else "(dry-run)", model_repo=repo)
    return {"backend": backend, "role": "lm", "model": repo, "env": ENV_FILE}


# --- install: transcription (asr) ----------------------------------------------------------------------
def install_asr(model, backend, dry):
    entry = find_entry("asr", model, backend)
    repo = (entry or {}).get("repo") or str(model)
    engine = (entry or {}).get("engine")
    label = (entry or {}).get("label") or model
    write_status(backend=backend, role="asr", model=model, done=False, ok=True, total=1, index=0,
                 current=f"ASR · {label}  ({repo})", pid=os.getpid())
    if engine == "whisper":
        if backend == "cuda":
            # q6 gguf: prefer a ready q6 download; else the whisper.cpp quantize helper (cuda/).
            quant = os.path.join(HERE, "cuda", "quantize_whisper_q6.sh")
            target = _cuda_whisper_gguf()
            if not dry:
                if not os.path.isfile(target) and os.path.isfile(quant):
                    subprocess.run(["bash", quant, repo, target], check=True)
                set_env_kv(CUDA_ENV, "LCC_CUDA_ASR_WHISPER_GGUF", target)
            write_status(backend=backend, role="asr", model=model, done=True, ok=True, total=1, index=1,
                         current="완료" if not dry else "(dry-run)", gguf=target)
            return {"backend": backend, "role": "asr", "engine": "whisper", "gguf": target}
        # mlx: prefer prequant 6bit, else quantize locally
        path, did_quant = _resolve_whisper_mlx(repo, dry)
        if not dry:
            set_env_kv(ENV_FILE, "LCC_WHISPER_MODEL", path)
        write_status(backend=backend, role="asr", model=model, done=True, ok=True, total=1, index=1,
                     current="완료" if not dry else "(dry-run)", model_repo=path, quantized=did_quant)
        return {"backend": backend, "role": "asr", "engine": "whisper", "model": path, "quantized": did_quant}
    # granite / qwen3 (and custom): download as-is (already shipped at a usable precision)
    if backend == "cuda":
        # CUDA granite/qwen3 ASR is served by transformers/asr_server.py or switch_asr_gguf.sh; just fetch HF repo.
        if not dry:
            _download_repo(repo)
        write_status(backend=backend, role="asr", model=model, done=True, ok=True, total=1, index=1,
                     current="완료" if not dry else "(dry-run)", model_repo=repo)
        return {"backend": backend, "role": "asr", "engine": engine, "model": repo}
    if not dry:
        _download_repo(repo)
    write_status(backend=backend, role="asr", model=model, done=True, ok=True, total=1, index=1,
                 current="완료" if not dry else "(dry-run)", model_repo=repo)
    return {"backend": backend, "role": "asr", "engine": engine, "model": repo}


def parse_args(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    def take(flag):
        if flag in args:
            i = args.index(flag)
            val = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
            return val
        return None

    role = (take("--role") or "").strip().lower()
    model = (take("--model") or "").strip()
    backend = take("--backend")
    return role, model, backend, dry


def main():
    role, model, backend, dry = parse_args()
    s = _server()
    backend = (backend or os.environ.get("LCC_BACKEND") or getattr(s, "BACKEND", "mlx") or "mlx").strip().lower()
    backend = "cuda" if backend in ("cuda", "nvidia", "gpu", "http") else "mlx"
    if role not in ROLES:
        write_status(backend=backend, role=role, done=True, ok=False, error=f"unknown role: {role!r} (use asr|lm)")
        print(f"unknown role: {role!r} (use asr|lm)", file=sys.stderr)
        return 2
    if not model:
        write_status(backend=backend, role=role, done=True, ok=False, error="missing --model")
        print("missing --model", file=sys.stderr)
        return 2
    try:
        out = install_asr(model, backend, dry) if role == "asr" else install_lm(model, backend, dry)
    except Exception as e:
        write_status(backend=backend, role=role, model=model, done=True, ok=False, error=str(e))
        traceback.print_exc()
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
