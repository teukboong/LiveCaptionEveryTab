#!/usr/bin/env bash
# One-shot setup: create a repo-local .venv and install dependencies for your platform.
#   ./setup.sh                 # auto: 'mlx' on Apple Silicon, 'cuda' elsewhere
#   ./setup.sh mlx|cuda|parakeet   # force a specific extra
#   ./setup.sh --models        # also prefetch the default models (~20GB, optional)
# Override the base interpreter with LCC_PYTHON_BASE=python3.13 ./setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

EXTRA=""
MODELS=0
for a in "$@"; do
  case "$a" in
    mlx|cuda|parakeet) EXTRA="$a" ;;
    --models) MODELS=1 ;;
    -h|--help) echo "usage: ./setup.sh [mlx|cuda|parakeet] [--models]"; exit 0 ;;
    *) echo "unknown arg: $a (usage: ./setup.sh [mlx|cuda|parakeet] [--models])"; exit 1 ;;
  esac
done
if [ -z "$EXTRA" ]; then
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then EXTRA="mlx"; else EXTRA="cuda"; fi
fi

PYBIN="${LCC_PYTHON_BASE:-python3}"
if ! command -v "$PYBIN" >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first (e.g. brew install python@3.13)." >&2
  exit 1
fi

echo "[setup] creating .venv with $PYBIN  (extra: $EXTRA)"
"$PYBIN" -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install ".[$EXTRA]"

if [ "$MODELS" = "1" ]; then
  echo "[setup] prefetching default models (~20GB, resumable)…"
  .venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download as d
for r in ["ibm-granite/granite-speech-4.1-2b",
          "Qwen/Qwen3-ASR-1.7B",
          "mlx-community/gemma-4-26b-a4b-it-4bit"]:
    print("downloading", r); d(r)
print("done")
PY
fi

echo
echo "[setup] done. next:"
echo "  1) bash bridge/run_bridge.sh                 # start the bridge (first load ~40s)"
echo "  2) chrome://extensions -> Developer mode -> Load unpacked -> select extension/"
echo "  3) (optional) cp .env.example .env           # pin tier / tweak knobs"
