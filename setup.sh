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

# Native-messaging host: lets the popup start/stop the bridge and install model tiers without a terminal.
# Chrome's sandbox can't install this itself (it's the bootstrap), so it's the one filesystem step done here.
# Non-fatal: if no Chromium browser is installed yet, install it and re-run setup (or run install-host.sh later).
if [ "$(uname -s)" = "Darwin" ]; then
  echo "[setup] installing native-messaging host (enables the popup's bridge/model buttons)…"
  bash extension/native-host/install-host.sh || echo "[setup] host install skipped — install Chrome/Edge/Brave, then re-run ./setup.sh"
elif [ "$EXTRA" = "cuda" ] && [ -f extension/native-host/install-host-windows-wsl.sh ]; then
  echo "[setup] installing native-messaging host (WSL)…"
  bash extension/native-host/install-host-windows-wsl.sh || echo "[setup] host install skipped (see SETUP-windows.md)"
fi

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
echo "[setup] done. next (no more terminal needed):"
echo "  1) chrome://extensions -> Developer mode -> Load unpacked -> select extension/   (then reload it)"
echo "  2) open the popup -> '브릿지 켜기' to start the bridge, and Full/Mid/Lite to fetch models"
echo "     (terminal alternative: bash bridge/run_bridge.sh)"
echo "  3) (optional) cp .env.example .env   # pin a tier / tweak knobs"
