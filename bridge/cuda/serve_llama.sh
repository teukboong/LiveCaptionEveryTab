#!/usr/bin/env bash
# Translation server — Gemma-4-26B-A4B via llama.cpp llama-server (OpenAI /v1/chat/completions on :8080).
# Run inside WSL2 (Ubuntu). Build llama.cpp with CUDA first, or install a prebuilt with `llama-server` on PATH.
#
#   # build (once):  https://github.com/ggml-org/llama.cpp  ->  cmake -B build -DGGML_CUDA=ON && cmake --build build -j
#   bash serve_llama.sh
set -euo pipefail

MODEL="${LCC_LLAMA_GGUF:-$HOME/models/gemma-4-26b-a4b-it-Q4_K_M.gguf}"
PORT="${LCC_LLAMA_PORT:-8080}"
NGL="${LCC_LLAMA_NGL:-999}"        # GPU layers to offload (999 = all)
CTX="${LCC_LLAMA_CTX:-4096}"
BIN="${LCC_LLAMA_BIN:-llama-server}"

command -v "$BIN" >/dev/null 2>&1 || { echo "llama-server('$BIN')를 PATH에서 못 찾음 — llama.cpp를 CUDA로 빌드하거나 LCC_LLAMA_BIN으로 지정"; exit 1; }
[ -f "$MODEL" ] || { echo "GGUF 없음: $MODEL  (LCC_LLAMA_GGUF로 지정하거나 SETUP-windows.md '4. 모델 받기' 참고)"; exit 1; }

# --jinja: honour chat_template_kwargs:{enable_thinking:false} that the bridge sends (uncensored finetunes
#          default to a hidden <think> channel; without this the caption could be the model's reasoning).
# -fa:     flash-attention. -ngl: offload to the 3090.
exec "$BIN" -m "$MODEL" --host 0.0.0.0 --port "$PORT" -ngl "$NGL" -c "$CTX" --jinja -fa ${LCC_LLAMA_EXTRA:-}
