#!/usr/bin/env bash
# Start the caption bridge in CUDA mode (HTTP -> llama.cpp + granite/qwen3 ASR). Run inside WSL2, AFTER the
# two model servers (serve_llama.sh + serve_asr.sh) are up. The Chrome extension on Windows reaches this via
# WSL2's localhost forwarding at ws://127.0.0.1:8765 — no extension change needed.
#
#   bash serve_llama.sh        # terminal 1 — 26B translate (:8080)
#   bash serve_asr.sh          # terminal 2 — granite/qwen3 ASR (:8000)
#   bash run_bridge_cuda.sh    # terminal 3 — bridge (:8765)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${LCC_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "Python venv를 못 찾음: $PY"
  echo "SETUP-windows.md의 '3. WSL2 Python 환경' 참고. 또는 LCC_PYTHON 으로 경로 지정."
  exit 1
fi

# CUDA endpoints + models (LCC_CUDA_*) live here; copy cuda/lcc-cuda.env.example → .env.cuda and edit.
CUDA_ENV="${LCC_CUDA_ENV:-$ROOT/.env.cuda}"
[ -f "$CUDA_ENV" ] && { set -a; . "$CUDA_ENV"; set +a; }

# Repo-local .env (copy .env.example -> .env), then policy A/B flags (.env.policy), same as the Mac runner.
ENV_FILE="${LCC_ENV_FILE:-$ROOT/.env}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
POLICY_ENV="$ROOT/.env.policy"
[ -f "$POLICY_ENV" ] && { set -a; . "$POLICY_ENV"; set +a; }

export LCC_BACKEND=cuda
exec env PYTHONWARNINGS=ignore "$PY" -u "$(dirname "$0")/../server.py"
