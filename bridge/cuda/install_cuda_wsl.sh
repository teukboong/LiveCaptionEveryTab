#!/usr/bin/env bash
# One-click WSL2/CUDA setup for the Windows Live Caption package.
# Run as root inside Ubuntu WSL. It installs the bridge venv, builds llama.cpp with CUDA,
# downloads the chosen tier's translation GGUF (arg/LCC_LM_TIER: full|mid|lite, default mid — one tier, not
# all, to save disk), and writes ~/.lcc-cuda.env for popup-managed start/stop. Tiers are also switchable later
# from the popup. Usage: install_cuda_wsl.sh [full|mid|lite]
set -euo pipefail

ROOT="${LCC_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
MODEL_ROOT="${LCC_MODEL_ROOT:-$HOME/models/live-caption-cuda8}"
RUNTIME_ROOT="${LCC_RUNTIME_ROOT:-$HOME/runtime}"
VENV="${LCC_PYTHON_VENV:-$HOME/.venvs/lcc-asr}"
LLAMA_DIR="${LCC_LLAMA_DIR:-$RUNTIME_ROOT/llama.cpp-live-caption}"
LLAMA_REPO="${LCC_LLAMA_REPO:-https://github.com/ggml-org/llama.cpp.git}"
LLAMA_REF="${LCC_LLAMA_REF:-master}"
LLAMA_BIN="$LLAMA_DIR/build/bin/llama-server"

TIER="${1:-${LCC_LM_TIER:-mid}}"
case "$TIER" in
  full) GEMMA_REPO="${LCC_GEMMA_REPO:-google/gemma-4-26B-A4B-it-qat-q4_0-gguf}" ;;
  lite) GEMMA_REPO="${LCC_GEMMA_REPO:-google/gemma-4-E2B-it-qat-q4_0-gguf}" ;;
  *)    TIER="mid"; GEMMA_REPO="${LCC_GEMMA_REPO:-google/gemma-4-E4B-it-qat-q4_0-gguf}" ;;
esac
GEMMA_DIR="$MODEL_ROOT/$TIER"   # GEMMA_PATH is set after download (located, not a guessed filename)

ASR_GRANITE_HF="${LCC_CUDA_ASR_GRANITE_HF:-ibm-granite/granite-speech-4.1-2b-GGUF:Q8_0}"
ASR_QWEN3_HF="${LCC_CUDA_ASR_QWEN3_HF:-mradermacher/Qwen3-ASR-1.7B-GGUF:Q6_K}"

log() { printf '\n== %s ==\n' "$*"; }
as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

log "APT packages"
as_root apt-get update
as_root DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential ca-certificates cmake curl ffmpeg git git-lfs jq libcurl4-openssl-dev \
  lsof nvidia-cuda-toolkit pciutils python3 python3-pip python3-venv unzip

log "WSL NVIDIA GPU check"
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi is not visible inside WSL. Install/update the Windows NVIDIA driver, reboot, then rerun install-windows-oneclick.bat." >&2
  exit 1
fi

log "Python venv"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip==26.0.1 setuptools==80.9.0 wheel==0.45.1
"$VENV/bin/python" -m pip install \
  huggingface-hub==1.8.0 \
  numpy==2.0.2 \
  onnxruntime==1.19.2 \
  silero-vad==6.2.1 \
  websockets==15.0.1

log "llama.cpp CUDA build"
mkdir -p "$RUNTIME_ROOT"
if [ ! -d "$LLAMA_DIR/.git" ]; then
  git clone "$LLAMA_REPO" "$LLAMA_DIR"
fi
git -C "$LLAMA_DIR" fetch --tags --depth 1 origin "$LLAMA_REF" || git -C "$LLAMA_DIR" fetch --tags origin "$LLAMA_REF"
git -C "$LLAMA_DIR" checkout FETCH_HEAD
cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_CUDA=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build "$LLAMA_DIR/build" --config Release -j"$(nproc)" --target llama-server llama-cli
[ -x "$LLAMA_BIN" ] || { echo "llama-server build output missing: $LLAMA_BIN" >&2; exit 1; }

log "Download translation model (tier: $TIER)"
mkdir -p "$GEMMA_DIR"
GEMMA_PATH="$("$VENV/bin/python" - "$GEMMA_REPO" "$GEMMA_DIR" <<'PY'
import os, sys, glob
from huggingface_hub import snapshot_download

repo, out_dir = sys.argv[1:]
token = os.environ.get("HF_TOKEN") or None
try:
    path = snapshot_download(repo_id=repo, allow_patterns=["*.gguf"], local_dir=out_dir, token=token)
except Exception as exc:
    raise SystemExit(
        f"failed to download {repo}. If gated, accept the license on Hugging Face, set HF_TOKEN, and rerun "
        f"install-windows-oneclick.bat. error={exc}"
    )
ggufs = sorted(glob.glob(os.path.join(path, "**", "*.gguf"), recursive=True))
main = [g for g in ggufs if "mmproj" not in os.path.basename(g).lower()] or ggufs
if not main:
    raise SystemExit(f"no .gguf found in {repo}")
print(main[0])
PY
)"
[ -f "$GEMMA_PATH" ] || { echo "translation GGUF download failed: $GEMMA_REPO" >&2; exit 1; }

log "Write CUDA environment"
cat > "$HOME/.lcc-cuda.env" <<EOF
LCC_ROOT=$ROOT
LCC_PYTHON=$VENV/bin/python
LCC_LLAMA_BIN=$LLAMA_BIN
LCC_LLAMA_GGUF=$GEMMA_PATH
LCC_LM_TIER=$TIER
LCC_LLAMA_CTX=2048
LCC_LLAMA_NGL=all
LCC_CUDA_CHAT_PORT=18080
LCC_CUDA_ASR_PORT=8000
LCC_CUDA_ASR_NGL=all
LCC_CUDA_ASR_SWITCH_CMD=$ROOT/bridge/cuda/switch_asr_gguf.sh
LCC_CUDA_ASR_LLAMA_BIN=$LLAMA_BIN
LCC_CUDA_ASR_GRANITE_HF=$ASR_GRANITE_HF
LCC_CUDA_ASR_QWEN3_HF=$ASR_QWEN3_HF
LCC_CUDA_TIMEOUT=120
LCC_HOST=127.0.0.1
# If WSL2 localhost-forwarding doesn't reach 127.0.0.1, set LCC_HOST=0.0.0.0 AND LCC_ALLOW_INSECURE_BIND=1
# (0.0.0.0 exposes the bridge to your LAN with the built-in token — see SETUP-windows.md).
EOF

log "Smoke"
"$ROOT/bridge/cuda/lcc_cuda_stack.sh" status qwen3
echo "Setup complete. Use the Chrome extension popup to start/stop the bridge."
