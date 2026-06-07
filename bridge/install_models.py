#!/usr/bin/env python3
"""Download ONLY the chosen tier's models and wire it as the active translation tier.

Tier- and backend-aware so nothing over-downloads (full is ~14GB+, lite ~4GB — picking by tier saves disk).
Used by BOTH the popup (native host spawns this) and setup.sh (--models), for MLX and CUDA. Single source of
truth for tier->models is bridge/server.py (_LM_TIERS + ASR constants); no model ids are hardcoded for MLX.

  install_models.py <full|mid|lite|auto> [--backend mlx|cuda] [--dry-run]

- auto      : resolve the tier from free memory (server._auto_tier) for the given backend
- MLX       : snapshot_download the tier's translator + ASR repos (HF cache); set LCC_LM_TIER in repo .env
- CUDA      : download the tier's translation GGUF; set LCC_LLAMA_GGUF in ~/.lcc-cuda.env + LCC_LM_TIER
              (ASR GGUF is tier-independent and handled by install_cuda_wsl.sh / switch_asr_gguf.sh)

Writes JSON progress to ~/.lcc-install.json so the popup can poll.
"""
import glob
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))          # .../bridge
ROOT = os.path.dirname(HERE)                                # repo root
STATUS = os.path.join(os.path.expanduser("~"), ".lcc-install.json")
ENV_FILE = os.path.join(ROOT, ".env")
CUDA_ENV = os.environ.get("LCC_CUDA_ENV", os.path.join(os.path.expanduser("~"), ".lcc-cuda.env"))
CUDA_MODEL_ROOT = os.environ.get("LCC_MODEL_ROOT", os.path.join(os.path.expanduser("~"), "models", "live-caption-cuda8"))
TIERS = ("full", "mid", "lite")

# CUDA translation = llama.cpp GGUF (QAT q4_0). ASR GGUF is separate + tier-independent (install_cuda_wsl.sh).
CUDA_GGUF_REPO = {
    "full": "google/gemma-4-26B-A4B-it-qat-q4_0-gguf",
    "mid":  "google/gemma-4-E4B-it-qat-q4_0-gguf",
    "lite": "google/gemma-4-E2B-it-qat-q4_0-gguf",
}


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


def mlx_models(tier):
    """[(label, repo_id), ...] for this tier on MLX, from server.py."""
    s = _server()
    lm = s._LM_TIERS["mlx"][tier]
    qwen = s._ASR_QWEN3_LITE if tier == "lite" else s._ASR_QWEN3_FULL
    if tier == "lite":
        return [("ASR · Qwen3 0.6B", qwen), (f"Translate · {tier}", lm)]
    granite = s.MLXA_REPOS["granite"]
    return [("ASR · Granite", granite), ("ASR · Qwen3 1.7B", qwen), (f"Translate · {tier}", lm)]


def install_mlx(tier, dry):
    models = mlx_models(tier)
    total = len(models)
    write_status(backend="mlx", tier=tier, done=False, ok=True, total=total, index=0,
                 current=models[0][0], pid=os.getpid())
    if not dry:
        from huggingface_hub import snapshot_download
        for i, (label, repo) in enumerate(models):
            write_status(backend="mlx", tier=tier, done=False, ok=True, total=total, index=i,
                         current=f"{label}  ({repo})", pid=os.getpid())
            snapshot_download(repo)
    set_env_kv(ENV_FILE, "LCC_LM_TIER", tier)
    write_status(backend="mlx", tier=tier, done=True, ok=True, total=total, index=total,
                 current="완료" if not dry else "(dry-run)", model=models[-1][1])
    return {"backend": "mlx", "tier": tier, "models": [r for _, r in models], "env": ENV_FILE}


def install_cuda(tier, dry):
    repo = CUDA_GGUF_REPO[tier]
    write_status(backend="cuda", tier=tier, done=False, ok=True, total=1, index=0,
                 current=f"Translate GGUF · {tier}  ({repo})", pid=os.getpid())
    if dry:
        gguf = os.path.join(CUDA_MODEL_ROOT, tier, f"<{tier}>.gguf")
    else:
        from huggingface_hub import snapshot_download
        path = snapshot_download(repo, allow_patterns=["*.gguf"])
        ggufs = sorted(glob.glob(os.path.join(path, "**", "*.gguf"), recursive=True))
        if not ggufs:
            raise RuntimeError(f"no .gguf found in {repo}")
        main = [g for g in ggufs if "mmproj" not in os.path.basename(g).lower()] or ggufs
        gguf = main[0]                                   # the LM gguf (skip the vision mmproj)
    set_env_kv(CUDA_ENV, "LCC_LLAMA_GGUF", gguf)          # lcc_cuda_stack.sh sources ~/.lcc-cuda.env
    set_env_kv(ENV_FILE, "LCC_LM_TIER", tier)
    write_status(backend="cuda", tier=tier, done=True, ok=True, total=1, index=1,
                 current="완료" if not dry else "(dry-run)", model=os.path.basename(gguf), gguf=gguf)
    return {"backend": "cuda", "tier": tier, "gguf": gguf, "cuda_env": CUDA_ENV}


def parse_args():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    backend = None
    if "--backend" in args:
        i = args.index("--backend")
        backend = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    tier = (args[0] if args else "").strip().lower()
    return tier, backend, dry


def main():
    tier, backend, dry = parse_args()
    s = _server()
    backend = (backend or os.environ.get("LCC_BACKEND") or getattr(s, "BACKEND", "mlx") or "mlx").strip().lower()
    backend = "cuda" if backend in ("cuda", "nvidia", "gpu", "http") else "mlx"
    if tier == "auto":
        s.BACKEND = backend
        tier = s._auto_tier()
    if tier not in TIERS:
        write_status(backend=backend, tier=tier, done=True, ok=False, error=f"unknown tier: {tier!r}")
        print(f"unknown tier: {tier!r} (use full|mid|lite|auto)", file=sys.stderr)
        return 2
    try:
        out = install_cuda(tier, dry) if backend == "cuda" else install_mlx(tier, dry)
    except Exception as e:
        write_status(backend=backend, tier=tier, done=True, ok=False, error=str(e))
        traceback.print_exc()
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
