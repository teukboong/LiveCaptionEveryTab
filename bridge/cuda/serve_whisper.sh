#!/usr/bin/env bash
# Serve a q6 Whisper ggml via whisper.cpp's whisper-server, exposing the OpenAI-compatible
# /v1/audio/transcriptions surface the bridge's CUDA backend posts to (model=whisper -> port 8001 by
# default, distinct from the granite/qwen3 transformers server on 8000). CODE-COMPLETE / unverified here.
#
#   serve_whisper.sh [model_gguf]
#
# Env: LCC_CUDA_ASR_WHISPER_GGUF / LCC_CUDA_ASR_WHISPER_HOST / LCC_CUDA_ASR_WHISPER_PORT /
#      LCC_WHISPER_SERVER_BIN (default whisper-server)
set -euo pipefail

MODEL="${1:-${LCC_CUDA_ASR_WHISPER_GGUF:-$HOME/models/live-caption-cuda8/asr-gguf-q6/whisper/whisper-large-v3-q6_k.gguf}}"
HOST="${LCC_CUDA_ASR_WHISPER_HOST:-127.0.0.1}"
PORT="${LCC_CUDA_ASR_WHISPER_PORT:-8001}"
BIN="${LCC_WHISPER_SERVER_BIN:-whisper-server}"

command -v "$BIN" >/dev/null 2>&1 || { echo "whisper-server not found: $BIN (build whisper.cpp with -DWHISPER_BUILD_SERVER=ON)" >&2; exit 1; }
if [ ! -f "$MODEL" ]; then
  echo "whisper q6 model not found: $MODEL" >&2
  echo "run bridge/cuda/quantize_whisper_q6.sh (or the popup 다운로드) first." >&2
  exit 1
fi

echo "[whisper] serving $MODEL on http://$HOST:$PORT (/v1/audio/transcriptions)"
exec "$BIN" --model "$MODEL" --host "$HOST" --port "$PORT" --inference-path "/v1/audio/transcriptions" "${@:2}"
