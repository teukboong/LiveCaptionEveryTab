#!/bin/bash
set -eo pipefail
# Start the local caption bridge (ASR + Gemma-4 translate).
# Default interpreter is the repo-local venv (.venv); override with LCC_PYTHON=/path/to/python ./run_bridge.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${LCC_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "Python venv를 못 찾음: $PY"
  echo "먼저 ./setup.sh 를 실행하거나, LCC_PYTHON 으로 인터프리터 경로를 지정하세요."
  exit 1
fi
# Optional config: repo-local .env (copy .env.example -> .env). Holds LCC_* overrides (tier, latency, etc.).
ENV_FILE="${LCC_ENV_FILE:-$ROOT/.env}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
# Experimental interpretation-policy A/B flags written by policy_ab.sh land in .env.policy (sourced last).
POLICY_ENV="$ROOT/.env.policy"
[ -f "$POLICY_ENV" ] && { set -a; . "$POLICY_ENV"; set +a; }

# EXPERIMENTAL diffusion translator lifecycle (LCC_TX_BACKEND=cuda; see server.py's tx-only seam).
# The external server holds the model resident (~17GB) — exactly the kind of thing you forget about,
# so the bridge owns it: diffusion mode auto-starts it and waits for /health; normal mode stops a
# leftover instance BEFORE the memory-tier check, or the translator silently downgrades for lack of RAM.
DG_BIN="llama-diffusion-gemma-http"
DG_DIR="${LCC_DG_DIR:-$HOME/llama.cpp-diffusion}"
DG_PORT="${DG_PORT:-8090}"
if [ "${LCC_TX_BACKEND:-}" = "cuda" ] && [ -x "$DG_DIR/run-diffusion-server.sh" ]; then
  if ! curl -fsS -m 2 "http://127.0.0.1:${DG_PORT}/health" >/dev/null 2>&1; then
    echo "[bridge] starting diffusion translator ($DG_DIR, port $DG_PORT)…"
    ("$DG_DIR/run-diffusion-server.sh" >> "${LCC_DG_LOG:-/tmp/dgemma-http.log}" 2>&1 &)
    for _ in $(seq 1 60); do
      curl -fsS -m 2 "http://127.0.0.1:${DG_PORT}/health" >/dev/null 2>&1 && break
      sleep 2
    done
    curl -fsS -m 2 "http://127.0.0.1:${DG_PORT}/health" >/dev/null 2>&1 \
      || { echo "[bridge] diffusion translator failed to come up (${LCC_DG_LOG:-/tmp/dgemma-http.log})"; exit 1; }
  fi
  # left running across bridge restarts on purpose: reloading 17GB per restart is the worse default
elif pgrep -f "$DG_BIN" >/dev/null 2>&1; then
  echo "[bridge] stopping leftover diffusion translator (frees its RAM before the MLX tier check)"
  pkill -f "$DG_BIN" || true
  sleep 1
fi

exec env PYTHONWARNINGS=ignore "$PY" -u "$(dirname "$0")/server.py"
