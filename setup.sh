#!/usr/bin/env bash
# One-shot setup: create a repo-local .venv and install dependencies for your platform.
#   ./setup.sh                          # auto backend (mlx on Apple Silicon, cuda elsewhere)
#   ./setup.sh mlx|cuda|parakeet        # force a backend extra
#   ./setup.sh --models                 # also fetch a memory-fit translation model + Granite ASR (disk-frugal)
#   ./setup.sh --models --tier lite     # fetch a smaller translation model instead (full|mid|lite -> 26B|E4B|E2B)
# Override the base interpreter with LCC_PYTHON_BASE=python3.13 ./setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

USAGE="usage: ./setup.sh [mlx|cuda|parakeet] [--models] [--tier full|mid|lite|auto] [--python-check]"
EXTRA=""
MODELS=0
TIER="auto"
PYTHON_CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    mlx|cuda|parakeet) EXTRA="$1" ;;
    --models) MODELS=1 ;;
    --tier) TIER="${2:-auto}"; shift ;;
    --tier=*) TIER="${1#--tier=}" ;;
    --python-check) PYTHON_CHECK=1 ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
    *) echo "unknown arg: $1 ($USAGE)"; exit 1 ;;
  esac
  shift
done
if [ -z "$EXTRA" ]; then
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then EXTRA="mlx"; else EXTRA="cuda"; fi
fi

python_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

python_version() {
  "$1" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
}

pick_python_base() {
  if [ -n "${LCC_PYTHON_BASE:-}" ]; then
    local forced
    forced="$(command -v "$LCC_PYTHON_BASE" 2>/dev/null || true)"
    if [ -z "$forced" ]; then
      echo "LCC_PYTHON_BASE를 찾지 못함: $LCC_PYTHON_BASE" >&2
      return 1
    fi
    if ! python_ok "$forced"; then
      echo "LCC_PYTHON_BASE는 Python 3.10+이어야 함: $forced ($(python_version "$forced" 2>/dev/null || echo unknown))" >&2
      return 1
    fi
    echo "$forced"
    return 0
  fi

  local candidates resolved seen
  candidates="python3.13 python3.12 python3.11 python3.10 python3.14 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3"
  seen=":"
  for cand in $candidates; do
    resolved="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$resolved" ] || continue
    case "$seen" in *":$resolved:"*) continue ;; esac
    seen="${seen}${resolved}:"
    if python_ok "$resolved"; then
      echo "$resolved"
      return 0
    fi
  done
  echo "Python 3.10+를 찾지 못했습니다. macOS라면 brew install python@3.13 후 다시 실행하세요." >&2
  return 1
}

PYBIN="$(pick_python_base)"
if [ "$PYTHON_CHECK" = "1" ]; then
  echo "$PYBIN ($(python_version "$PYBIN"))"
  exit 0
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
  # Pick the translation model: an explicit --tier maps to its model for back-compat, else memory-fit auto.
  case "$TIER" in
    full|large|max) LMMODEL="gemma-26b" ;;
    mid|medium)     LMMODEL="gemma-e4b" ;;
    lite|small|min) LMMODEL="gemma-e2b" ;;
    *) LMMODEL="$(.venv/bin/python -c "import sys;sys.path.insert(0,'bridge');import server as s;s.BACKEND=s._normalize_backend('$EXTRA');print(s._auto_lm_model()['id'])" 2>/dev/null || echo gemma-26b)" ;;
  esac
  echo "[setup] fetching translation model '$LMMODEL' + Granite ASR ($EXTRA) — resumable…"
  .venv/bin/python bridge/install_models.py --role lm --model "$LMMODEL" --backend "$EXTRA"
  .venv/bin/python bridge/install_models.py --role asr --model granite --backend "$EXTRA"
fi

echo
echo "[setup] done. next (no more terminal needed):"
echo "  1) chrome://extensions -> Developer mode -> Load unpacked -> select extension/   (then reload it)"
echo "  2) open the popup -> '브릿지 켜기' to start the bridge; pick/download models from the 모델 dropdowns"
echo "     (terminal alternative: bash bridge/run_bridge.sh)"
echo "  3) (optional) cp .env.example .env   # pin a tier / tweak knobs"
