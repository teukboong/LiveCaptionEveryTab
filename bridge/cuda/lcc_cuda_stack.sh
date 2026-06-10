#!/usr/bin/env bash
# Manage the CUDA live-caption stack for the Chrome extension popup.
# Starts/stops the translation llama-server, the selected GGUF ASR server, and the bridge.
set -euo pipefail

CMD="${1:-status}"
ENGINE="${2:-${LCC_ASR_ENGINE:-granite}}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${LCC_ROOT:-$(cd "$HERE/../.." && pwd)}"
CUDA_DIR="${LCC_CUDA_DIR:-$HERE}"
LOG_DIR="${LCC_CUDA_LOG_DIR:-$HOME/models/live-caption-cuda8/logs}"
STATE_DIR="${LCC_CUDA_STATE_DIR:-$HOME/.cache/live-caption-cuda}"
mkdir -p "$LOG_DIR" "$STATE_DIR"

CUDA_ENV="${LCC_CUDA_ENV:-$HOME/.lcc-cuda.env}"
[ -f "$CUDA_ENV" ] && { set -a; . "$CUDA_ENV"; set +a; }

PY="${LCC_PYTHON:-$HOME/.venvs/lcc-asr/bin/python}"
LLAMA_BIN="${LCC_LLAMA_BIN:-$(command -v llama-server || echo "$HOME/llama.cpp/build/bin/llama-server")}"   # one-click sets LCC_LLAMA_BIN; else PATH, else conventional build dir

CHAT_HOST="${LCC_CUDA_CHAT_HOST:-127.0.0.1}"
CHAT_PORT="${LCC_CUDA_CHAT_PORT:-18080}"
CHAT_MODEL_PATH="${LCC_LLAMA_GGUF:-$HOME/models/live-caption-cuda8/gemma-4-E4B-it-qat-q4_0/gguf/gemma-4-E4B_q4_0-it.gguf}"
CHAT_CTX="${LCC_LLAMA_CTX:-2048}"
CHAT_NGL="${LCC_LLAMA_NGL:-all}"
CHAT_PID="$STATE_DIR/translation-${CHAT_PORT}.pid"

ASR_HOST="${LCC_CUDA_ASR_HOST:-127.0.0.1}"
ASR_PORT="${LCC_CUDA_ASR_PORT:-8000}"
ASR_SWITCH_CMD="${LCC_CUDA_ASR_SWITCH_CMD:-$CUDA_DIR/switch_asr_gguf.sh}"

BRIDGE_PORT="${LCC_PORT:-8765}"
BRIDGE_HOST="${LCC_BRIDGE_HOST:-${LCC_HOST:-127.0.0.1}}"   # loopback by default (WSL2 localhost-forwarding reaches it); for 0.0.0.0 also set LCC_ALLOW_INSECURE_BIND=1
BRIDGE_PID="$STATE_DIR/bridge-${BRIDGE_PORT}.pid"
BRIDGE_LOG="$LOG_DIR/bridge-popup-$(date -u +%Y%m%dT%H%M%SZ).log"

is_listening() {
  local port="$1"
  ss -ltn "( sport = :$port )" 2>/dev/null | awk 'NR > 1 { found=1 } END { exit found ? 0 : 1 }'
}

listener_pids() {
  local port="$1"
  ss -ltnp "( sport = :$port )" 2>/dev/null \
    | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
    | sort -u
}

stop_port() {
  local port="$1"
  local pids=()
  mapfile -t pids < <(listener_pids "$port" || true)
  [ "${#pids[@]}" -eq 0 ] && return 0
  kill "${pids[@]}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    is_listening "$port" || return 0
    sleep 0.25
  done
  if is_listening "$port"; then
    kill -9 "${pids[@]}" 2>/dev/null || true
  fi
}

wait_health() {
  local url="$1"
  local label="$2"
  local secs="${3:-90}"
  for _ in $(seq 1 "$secs"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "$label health timeout: $url" >&2
  return 1
}

start_translation() {
  if is_listening "$CHAT_PORT"; then
    return 0
  fi
  [ -x "$LLAMA_BIN" ] || { echo "llama-server 없음: $LLAMA_BIN" >&2; return 1; }
  [ -f "$CHAT_MODEL_PATH" ] || { echo "translation GGUF 없음: $CHAT_MODEL_PATH" >&2; return 1; }
  local log="$LOG_DIR/translation-popup-$(date -u +%Y%m%dT%H%M%SZ).log"
  nohup "$LLAMA_BIN" \
    --model "$CHAT_MODEL_PATH" \
    --host "$CHAT_HOST" \
    --port "$CHAT_PORT" \
    --ctx-size "$CHAT_CTX" \
    --parallel 1 \
    --gpu-layers "$CHAT_NGL" \
    --cache-type-k q8_0 \
    --cache-type-v q8_0 \
    --flash-attn auto \
    --jinja \
    --reasoning off \
    --no-webui >"$log" 2>&1 &
  echo "$!" > "$CHAT_PID"
  wait_health "http://127.0.0.1:$CHAT_PORT/health" "translation" "${LCC_CUDA_CHAT_WAIT_SECS:-120}" || {
    tail -80 "$log" >&2 || true
    return 1
  }
}

start_asr() {
  [ -x "$ASR_SWITCH_CMD" ] || { echo "ASR switch script 없음: $ASR_SWITCH_CMD" >&2; return 1; }
  LCC_CUDA_ASR_PORT="$ASR_PORT" \
  LCC_CUDA_ASR_HOST="$ASR_HOST" \
  LCC_CUDA_ASR_LLAMA_BIN="$LLAMA_BIN" \
  "$ASR_SWITCH_CMD" "$ENGINE"
}

start_bridge() {
  if is_listening "$BRIDGE_PORT"; then
    return 0
  fi
  [ -x "$PY" ] || { echo "Python venv 없음: $PY" >&2; return 1; }
  [ -f "$ROOT/bridge/server.py" ] || { echo "bridge server.py 없음: $ROOT/bridge/server.py" >&2; return 1; }
  (
    cd "$CUDA_DIR"
    export LCC_BACKEND=cuda
    export LCC_ASR_ENGINE="$ENGINE"
    export LCC_HOST="$BRIDGE_HOST"
    export LCC_PORT="$BRIDGE_PORT"
    export LCC_PYTHON="$PY"
    export LCC_CUDA_CHAT_URL="${LCC_CUDA_CHAT_URL:-http://127.0.0.1:$CHAT_PORT/v1/chat/completions}"
    export LCC_CUDA_CHAT_MODEL="${LCC_CUDA_CHAT_MODEL:-local}"
    export LCC_CUDA_ASR_URL="${LCC_CUDA_ASR_URL:-http://127.0.0.1:$ASR_PORT/v1/audio/transcriptions}"
    export LCC_CUDA_ASR_SWITCH_CMD="$ASR_SWITCH_CMD"
    export LCC_CUDA_ASR_LLAMA_BIN="$LLAMA_BIN"
    export LCC_CUDA_ASR_PORT="$ASR_PORT"
    export LCC_CUDA_ASR_NGL="${LCC_CUDA_ASR_NGL:-all}"
    export LCC_CUDA_ASR_GRANITE_MODEL="${LCC_CUDA_ASR_GRANITE_MODEL:-local}"
    export LCC_CUDA_ASR_QWEN3_MODEL="${LCC_CUDA_ASR_QWEN3_MODEL:-local}"
    export LCC_CUDA_TIMEOUT="${LCC_CUDA_TIMEOUT:-120}"
    exec nohup env PYTHONWARNINGS=ignore "$PY" -u ../server.py
  ) >"$BRIDGE_LOG" 2>&1 &
  echo "$!" > "$BRIDGE_PID"
  for _ in $(seq 1 "${LCC_CUDA_BRIDGE_WAIT_SECS:-60}"); do
    is_listening "$BRIDGE_PORT" && return 0
    sleep 1
  done
  tail -80 "$BRIDGE_LOG" >&2 || true
  return 1
}

status_json() {
  local chat=false asr=false bridge=false
  is_listening "$CHAT_PORT" && chat=true
  is_listening "$ASR_PORT" && asr=true
  is_listening "$BRIDGE_PORT" && bridge=true
  printf '{"ok":true,"translation":%s,"asr":%s,"bridge":%s,"running":%s,"ports":{"translation":%s,"asr":%s,"bridge":%s}}\n' \
    "$chat" "$asr" "$bridge" "$bridge" "$CHAT_PORT" "$ASR_PORT" "$BRIDGE_PORT"
}

case "$CMD" in
  start)
    start_translation
    start_asr
    start_bridge
    status_json
    ;;
  stop)
    stop_port "$BRIDGE_PORT"
    stop_port "$ASR_PORT"
    stop_port "$CHAT_PORT"
    rm -f "$BRIDGE_PID" "$CHAT_PID"
    status_json
    ;;
  restart)
    "$0" stop "$ENGINE" >/dev/null || true
    "$0" start "$ENGINE"
    ;;
  status)
    status_json
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status} [granite|qwen3|whisper]" >&2
    exit 2
    ;;
esac
