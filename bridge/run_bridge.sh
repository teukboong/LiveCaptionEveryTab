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

# Diffusion translator (tx_http registry model / LCC_TX_BACKEND=cuda): the PYTHON bridge owns the
# external server's lifetime (spawned at seam-bind, stopped at exit — model_runtime.ensure_diffusion_
# server), so it loads/unloads with the bridge like the in-process models. Here we only clear an
# ORPHAN on non-tx launches: a leftover instance holds ~17GB and silently downgrades the MLX
# translator tier. On tx launches a healthy leftover is left for the bridge to adopt (skips ~20s load).
if [ "${LCC_TX_BACKEND:-}" != "cuda" ] && [ "${LCC_LM_MODEL:-}" != "diffusiongemma-26b" ] \
   && pgrep -f "llama-diffusion-gemma-http" >/dev/null 2>&1; then
  echo "[bridge] stopping orphaned diffusion translator (frees its RAM before the MLX tier check)"
  pkill -f "llama-diffusion-gemma-http" || true
  sleep 1
fi

exec env PYTHONWARNINGS=ignore "$PY" -u "$(dirname "$0")/server.py"
