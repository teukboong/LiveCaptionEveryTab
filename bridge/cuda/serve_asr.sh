#!/usr/bin/env bash
# ASR server — granite-speech-4.1(영어) + Qwen3-ASR(다국어) on CUDA via transformers.
# OpenAI /v1/audio/transcriptions on :8000. The bridge selects the model with the popup's 전사 엔진.
# Run inside WSL2. (These are the SAME ASR models as the Mac/MLX path — no whisper.)
#
#   $PY -m pip install -U qwen-asr "transformers>=4.57" torch torchaudio accelerate soundfile fastapi "uvicorn[standard]" python-multipart
#   bash serve_asr.sh
set -euo pipefail

PY="${LCC_PYTHON:-$HOME/.venvs/lcc/bin/python}"
[ -x "$PY" ] || { echo "Python venv 못 찾음: $PY  (SETUP-windows.md '3. WSL2 Python' 참고 또는 LCC_PYTHON 지정)"; exit 1; }

cd "$(dirname "$0")"
exec env PYTHONWARNINGS=ignore "$PY" -u asr_server.py
