#!/usr/bin/env bash
# Produce a q6 (q6_k) Whisper ggml for whisper.cpp — prefer a ready quantized download, else download the
# base ggml and quantize locally. Called by bridge/install_models.py for the CUDA Whisper engine
# ("준비된 것 우선, 없으면 로컬 양자화"). CODE-COMPLETE / unverified on this machine (no CUDA box).
#
#   quantize_whisper_q6.sh <src_repo_or_marker> <out_gguf_path>
#
# Env:
#   LCC_WHISPER_Q6_HF        a ready q6 ggml HF ref ("repo:filename"); downloaded as-is if set (prequant path)
#   LCC_WHISPER_BASE_HF      base ggml HF ref (default ggerganov/whisper.cpp:ggml-large-v3.bin)
#   LCC_WHISPER_QUANTIZE_BIN whisper.cpp quantize binary (default: quantize)
set -euo pipefail

SRC="${1:-large-v3}"
OUT="${2:-$HOME/models/live-caption-cuda8/asr-gguf-q6/whisper/whisper-large-v3-q6_k.gguf}"
Q6_HF="${LCC_WHISPER_Q6_HF:-}"
BASE_HF="${LCC_WHISPER_BASE_HF:-ggerganov/whisper.cpp:ggml-large-v3.bin}"
QUANT_BIN="${LCC_WHISPER_QUANTIZE_BIN:-quantize}"

mkdir -p "$(dirname "$OUT")"

if [ -f "$OUT" ]; then
  echo "whisper q6 already present: $OUT"; exit 0
fi

# huggingface download helper: "repo:filename" -> local file path (uses the bridge venv's hf CLI).
hf_get() {
  local ref="$1" repo file
  repo="${ref%%:*}"; file="${ref#*:}"
  python3 - "$repo" "$file" <<'PY'
import sys
from huggingface_hub import hf_hub_download
print(hf_hub_download(repo_id=sys.argv[1], filename=sys.argv[2]))
PY
}

# 1) prequant q6 available -> just fetch it
if [ -n "$Q6_HF" ]; then
  echo "fetching ready q6 whisper: $Q6_HF"
  src_path="$(hf_get "$Q6_HF")"
  cp -f "$src_path" "$OUT"
  echo "whisper q6 ready (prequant): $OUT"; exit 0
fi

# 2) otherwise download the base ggml and quantize to q6_k locally
command -v "$QUANT_BIN" >/dev/null 2>&1 || {
  echo "whisper.cpp quantize binary not found: $QUANT_BIN (build whisper.cpp, or set LCC_WHISPER_Q6_HF to a ready q6)" >&2
  exit 1
}
echo "downloading base whisper ggml: $BASE_HF"
base_path="$(hf_get "$BASE_HF")"
echo "quantizing -> q6_k: $OUT"
"$QUANT_BIN" "$base_path" "$OUT" q6_k
echo "whisper q6 ready (locally quantized): $OUT"
