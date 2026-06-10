#!/usr/bin/env bash
# Switch the CUDA ASR endpoint to one GGUF engine. Intended for small-VRAM systems:
# the bridge calls this from backend_cuda.ensure_asr_loaded(engine), so a popup
# engine change frees the previous ASR model before loading the next one.
set -euo pipefail

ENGINE="${1:-${LCC_CUDA_ASR_ENGINE:-}}"
case "$ENGINE" in
  granite|qwen3|whisper) ;;
  *) echo "usage: $0 granite|qwen3|whisper" >&2; exit 2 ;;
esac

BIN="${LCC_CUDA_ASR_LLAMA_BIN:-${LCC_LLAMA_BIN:-llama-server}}"
HOST="${LCC_CUDA_ASR_HOST:-127.0.0.1}"
PORT="${LCC_CUDA_ASR_PORT:-8000}"
CTX="${LCC_CUDA_ASR_CTX:-2048}"
NGL="${LCC_CUDA_ASR_NGL:-all}"
GPU="${LCC_CUDA_ASR_CUDA_VISIBLE_DEVICES:-${CUDA_VISIBLE_DEVICES:-0}}"
LOG_DIR="${LCC_CUDA_ASR_LOG_DIR:-$HOME/models/live-caption-cuda8/logs}"
STATE_DIR="${LCC_CUDA_ASR_STATE_DIR:-$HOME/.cache/live-caption-cuda}"
STATE="$STATE_DIR/asr-switch-${PORT}.env"

# whisper is a different binary (whisper.cpp's whisper-server) on its OWN port (default 8002), not the
# granite/qwen3 llama-server on 8000. The bridge already routes model=whisper to that port, so "switching"
# to whisper just means ensuring that server is up. Start it in the background (idempotent) and gate on the
# port listening, then hand back — we leave the 8000 server alone (distinct VRAM endpoint).
if [ "$ENGINE" = "whisper" ]; then
  WHOST="${LCC_CUDA_ASR_WHISPER_HOST:-127.0.0.1}"
  WPORT="${LCC_CUDA_ASR_WHISPER_PORT:-8002}"
  SERVE_WHISPER="$(cd "$(dirname "$0")" && pwd)/serve_whisper.sh"
  WSTATE="$STATE_DIR/asr-switch-whisper-${WPORT}.env"
  mkdir -p "$LOG_DIR" "$STATE_DIR"
  whisper_listening() { ss -ltn "( sport = :$WPORT )" 2>/dev/null | awk 'NR > 1 { f = 1 } END { exit f ? 0 : 1 }'; }
  if whisper_listening; then
    echo "[asr-switch] whisper already serving on $WHOST:$WPORT"
    exit 0
  fi
  [ -x "$SERVE_WHISPER" ] || { echo "serve_whisper.sh not found/executable: $SERVE_WHISPER" >&2; exit 1; }
  WLOG="$LOG_DIR/asr-switch-whisper-$(date +%Y%m%dT%H%M%S).log"
  LCC_CUDA_ASR_WHISPER_HOST="$WHOST" LCC_CUDA_ASR_WHISPER_PORT="$WPORT" \
    nohup "$SERVE_WHISPER" > "$WLOG" 2>&1 &
  wpid="$!"
  for _ in $(seq 1 "${LCC_CUDA_ASR_SWITCH_WAIT_SECS:-90}"); do
    if ! kill -0 "$wpid" 2>/dev/null; then
      echo "[asr-switch] whisper-server exited pid=$wpid log=$WLOG" >&2
      tail -120 "$WLOG" >&2 || true
      exit 1
    fi
    if whisper_listening; then
      {
        printf 'ENGINE_ACTIVE=%q\n' "whisper"
        printf 'PID_ACTIVE=%q\n' "$wpid"
        printf 'PORT_ACTIVE=%q\n' "$WPORT"
        printf 'LOG_ACTIVE=%q\n' "$WLOG"
      } > "$WSTATE"
      echo "[asr-switch] ready engine=whisper pid=$wpid port=$WPORT log=$WLOG"
      exit 0
    fi
    sleep 1
  done
  echo "[asr-switch] timeout engine=whisper pid=$wpid log=$WLOG" >&2
  tail -120 "$WLOG" >&2 || true
  exit 1
fi

GRANITE_MODEL="${LCC_CUDA_ASR_GRANITE_GGUF:-$HOME/models/live-caption-cuda8/asr-gguf-q6/granite/granite-speech-4.1-2b-Q6_K.gguf}"
GRANITE_MMPROJ="${LCC_CUDA_ASR_GRANITE_MMPROJ:-$HOME/models/live-caption-cuda8/asr-gguf-q6/granite/mmproj-model-f16.gguf}"
GRANITE_HF="${LCC_CUDA_ASR_GRANITE_HF:-}"
QWEN3_MODEL="${LCC_CUDA_ASR_QWEN3_GGUF:-$HOME/models/live-caption-cuda8/asr-gguf-q6/qwen3/Qwen3-ASR-1.7B-Q6_K.gguf}"
QWEN3_MMPROJ="${LCC_CUDA_ASR_QWEN3_MMPROJ:-$HOME/models/live-caption-cuda8/asr-gguf-q6/qwen3/mmproj-Qwen3-ASR-1.7B-bf16.gguf}"
QWEN3_HF="${LCC_CUDA_ASR_QWEN3_HF:-}"

case "$ENGINE" in
  granite) MODEL="$GRANITE_MODEL"; MMPROJ="$GRANITE_MMPROJ"; HF_REF="$GRANITE_HF" ;;
  qwen3) MODEL="$QWEN3_MODEL"; MMPROJ="$QWEN3_MMPROJ"; HF_REF="$QWEN3_HF" ;;
esac

command -v "$BIN" >/dev/null 2>&1 || { echo "llama-server not found: $BIN" >&2; exit 1; }
if [ -n "${HF_REF:-}" ]; then
  MODEL_ARGS=(-hf "$HF_REF")
else
  [ -f "$MODEL" ] || { echo "ASR model not found: $MODEL" >&2; exit 1; }
  [ -f "$MMPROJ" ] || { echo "ASR mmproj not found: $MMPROJ" >&2; exit 1; }
  MODEL_ARGS=(--model "$MODEL" --mmproj "$MMPROJ" --mmproj-offload)
fi
mkdir -p "$LOG_DIR" "$STATE_DIR"

health_ok() {
  curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1
}

current_engine=""
current_pid=""
if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  . "$STATE" || true
  current_engine="${ENGINE_ACTIVE:-}"
  current_pid="${PID_ACTIVE:-}"
fi

if [ "$current_engine" = "$ENGINE" ] && [ -n "$current_pid" ] && kill -0 "$current_pid" 2>/dev/null && health_ok; then
  echo "[asr-switch] already active engine=$ENGINE pid=$current_pid port=$PORT"
  exit 0
fi

port_pids="$(ss -ltnp | sed -n "s/.*:$PORT .*pid=\([0-9][0-9]*\).*/\1/p" | sort -u | tr "\n" " ")"
for pid in $current_pid $port_pids; do
  [ -n "${pid:-}" ] || continue
  if kill -0 "$pid" 2>/dev/null; then
    echo "[asr-switch] stopping pid=$pid"
    kill "$pid" || true
  fi
done
sleep 1
for pid in $current_pid $port_pids; do
  [ -n "${pid:-}" ] || continue
  if kill -0 "$pid" 2>/dev/null; then
    echo "[asr-switch] force stopping pid=$pid"
    kill -9 "$pid" || true
  fi
done
rm -f "$STATE"

LOG="$LOG_DIR/asr-switch-${ENGINE}-$(date +%Y%m%dT%H%M%S).log"
CUDA_VISIBLE_DEVICES="$GPU" nohup "$BIN" \
  "${MODEL_ARGS[@]}" \
  --host "$HOST" \
  --port "$PORT" \
  --ctx-size "$CTX" \
  --parallel 1 \
  --gpu-layers "$NGL" \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --flash-attn auto \
  --jinja \
  --reasoning off \
  --no-webui \
  > "$LOG" 2>&1 &
pid="$!"

for _ in $(seq 1 "${LCC_CUDA_ASR_SWITCH_WAIT_SECS:-90}"); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[asr-switch] server exited engine=$ENGINE pid=$pid log=$LOG" >&2
    tail -120 "$LOG" >&2 || true
    exit 1
  fi
  if health_ok; then
    {
      printf 'ENGINE_ACTIVE=%q\n' "$ENGINE"
      printf 'PID_ACTIVE=%q\n' "$pid"
      printf 'PORT_ACTIVE=%q\n' "$PORT"
      printf 'LOG_ACTIVE=%q\n' "$LOG"
    } > "$STATE"
    echo "[asr-switch] ready engine=$ENGINE pid=$pid port=$PORT log=$LOG"
    exit 0
  fi
  sleep 1
done

echo "[asr-switch] timeout engine=$ENGINE pid=$pid log=$LOG" >&2
tail -120 "$LOG" >&2 || true
exit 1
