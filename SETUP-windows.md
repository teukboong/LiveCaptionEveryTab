# Live Caption Every Tab — Windows + NVIDIA 설치 가이드 (WSL2)

Mac(Apple MLX)와 **같은 확장프로그램**을, 이번엔 **NVIDIA GPU**로 돌린다. 전사도 Mac과 **같은 모델**
(granite-speech-4.1=영어 / Qwen3-ASR=다국어)을 CUDA에서 쓴다(whisper 안 씀). 브라우저 쪽은 한 줄도 안 바뀌고,
추론만 **WSL2 안의 llama.cpp(번역) + granite/qwen3(전사)** 가 맡는다. Windows Chrome → `ws://127.0.0.1:8765`
는 WSL2의 **localhost 포워딩**으로 그대로 연결된다.

```
[Windows Chrome 확장] ──WS(PCM16 16k)──▶ [WSL2: bridge/server.py  (LCC_BACKEND=cuda)]
                                            VAD + 문장 조립 + 스케줄러 (Mac과 동일 코드)
                                            ├─HTTP─▶ granite/qwen3 ASR :8000  (전사, Mac과 동일 모델)
                                            └─HTTP─▶ llama.cpp 26B     :8080  (모국어 번역)
   [content.js 오버레이 2줄] ◀──WS(JSON)────┘
```

> 브리지 핵심 로직(VAD·문장컷·스케줄러·넘버가드·프롬프트)은 Mac과 **완전히 동일**. 플랫폼이 바뀌는 건 GPU 3함수
> (전사/번역/요약)뿐이고, CUDA에서는 그게 OpenAI 호환 **HTTP 호출**로 대체된다(`bridge/backend_cuda.py`).

---

## 0. 요구 사항

| 항목 | 필요 | 비고 |
|---|---|---|
| **Windows** | **11** (또는 WSL2 지원 10) | WSL2 GPU 패스스루 |
| **GPU** | **NVIDIA, VRAM 16GB+** (24GB 권장: 3090/4090) | 26B Q4 ~17GB + granite/qwen3 ~2GB씩 |
| **WSL2** | Ubuntu 22.04+ | `wsl --install` |
| **NVIDIA 드라이버** | 최신 (Windows용 Game/Studio) | WSL2가 이걸 그대로 씀. WSL2 안엔 드라이버 설치 X |
| **브라우저** | 정품 Chrome/Edge/Brave (Windows) | Atlas/Arc 등 포크는 탭 캡처 미지원 |
| **디스크** | ~30GB | 모델 |

---

## 1. WSL2 + CUDA

PowerShell(관리자)에서:
```powershell
wsl --install -d Ubuntu
wsl --update
```
재부팅 후 Ubuntu를 한 번 실행해 사용자 생성. **Windows용 NVIDIA 드라이버만** 최신이면 WSL2에서 GPU가 보인다.
WSL2(Ubuntu) 터미널에서 확인:
```bash
nvidia-smi          # GPU가 보이면 OK (WSL2 안에 드라이버 따로 설치하지 말 것)
```
CUDA 툴킷(llama.cpp 빌드용)은 WSL2 안에 설치:
```bash
sudo apt update && sudo apt install -y build-essential cmake git ffmpeg python3-venv
# CUDA Toolkit (WSL-Ubuntu용) — https://developer.nvidia.com/cuda-downloads (Linux→WSL-Ubuntu)
```

---

## 2. 코드 받기 (WSL2 안에서)

```bash
# Windows의 폴더를 WSL2에서 쓰면 디스크 I/O가 느리다 → WSL2 홈에 두는 걸 권장
git clone <이 저장소> ~/live-caption     # 또는 tar 풀기
cd ~/live-caption/projects/live-caption
```

---

## 3. WSL2 Python 환경

CUDA 브리지 자체는 MLX가 필요 없다(가볍다 — 번역/전사 다 HTTP). venv 하나에 **브리지 + 전사서버** 의존성을 깐다:
```bash
python3 -m venv ~/.venvs/lcc
~/.venvs/lcc/bin/pip install -U pip
# 브리지 코어 (stdlib HTTP라 무거운 패키지 없음)
~/.venvs/lcc/bin/pip install websockets numpy silero-vad onnxruntime
# 전사 서버 (granite/qwen3 = transformers, CUDA)
~/.venvs/lcc/bin/pip install -U "transformers>=4.46" torch torchaudio accelerate soundfile fastapi "uvicorn[standard]" python-multipart
```
> torch는 CUDA 빌드로 깔려야 한다(`python -c "import torch;print(torch.cuda.is_available())"` → True). 아니면
> https://pytorch.org 의 CUDA wheel 명령으로 재설치.

---

## 4. 모델 받기

**① 번역 — Gemma-4-26B-A4B GGUF (Q4_K_M, ~17GB).** HuggingFace에서 `gemma-4-26b-a4b-it`의 **Q4_K_M GGUF**를 받아
`~/models/`에 둔다(예: `~/models/gemma-4-26b-a4b-it-Q4_K_M.gguf`). 프리빌트 GGUF가 없으면 llama.cpp의
`convert_hf_to_gguf.py`로 HF 가중치에서 직접 변환한 뒤 `llama-quantize ... Q4_K_M` 한다.

**② 전사 — granite-speech-4.1 + Qwen3-ASR.** `asr_server.py` 첫 요청 때 자동 다운로드(각 ~2-4GB). 미리 받으려면:
```bash
~/.venvs/lcc/bin/python - <<'PY'
from huggingface_hub import snapshot_download as d
for r in ["ibm-granite/granite-speech-4.1-2b", "Qwen/Qwen3-ASR-1.7B"]:
    print("downloading", r); d(r)
PY
```

---

## 5. llama.cpp (CUDA) 빌드

```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp && cd ~/llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j
# 결과: ~/llama.cpp/build/bin/llama-server
export PATH="$HOME/llama.cpp/build/bin:$PATH"   # serve_llama.sh가 llama-server를 PATH에서 찾음
```

---

## 6. 설정 + 실행 (WSL2, 터미널 3개)

설정 파일을 한 번 복사:
```bash
cd ~/live-caption/projects/live-caption/bridge/cuda
cp lcc-cuda.env.example ~/.lcc-cuda.env
# 기본값이 다 맞으면 그대로 둬도 됨. GGUF 경로만 다르면 ~/.lcc-cuda.env 의 LCC_LLAMA_GGUF 수정.
```

세 개를 각각 켠다(순서대로):
```bash
# 터미널 1 — 번역 서버 (:8080)
cd ~/live-caption/projects/live-caption/bridge/cuda && bash serve_llama.sh

# 터미널 2 — 전사 서버 granite/qwen3 (:8000)
cd ~/live-caption/projects/live-caption/bridge/cuda && bash serve_asr.sh

# 터미널 3 — 브리지 (:8765)
cd ~/live-caption/projects/live-caption/bridge/cuda && bash run_bridge_cuda.sh
```
브리지에 `[bridge] ready (CUDA HTTP backend …)` 와 두 엔드포인트 `reachable` 로그가 뜨면 준비 완료.

---

## 7. Windows Chrome에 확장 로드

Mac과 **동일**. WSL2의 코드 폴더는 Windows 탐색기에서 `\\wsl$\Ubuntu\home\<유저>\live-caption\...` 로 보인다.
1. Windows Chrome → `chrome://extensions` → **개발자 모드** 켜기
2. **압축해제된 확장 프로그램을 로드** → `…\live-caption\projects\live-caption\extension` 선택
3. 소리 나는 탭에서 **확장 아이콘 → `▶ 자막 시작`**. 오버레이에 원문+한국어 2줄 등장.

> 확장은 `ws://127.0.0.1:8765` 로 붙는다. WSL2 localhost 포워딩이 Windows localhost를 WSL2로 넘겨줘서 **수정 불필요**.

---

## 7.5. 전사 엔진 (영어 / 다국어) — Mac과 같은 모델

팝업의 **▸ 전사 엔진** = **Granite (영어)** / **Qwen3-ASR (일어·다국어)** 가 Mac과 **동일한 ASR 모델**이고,
CUDA에서도 그대로 먹는다. 선택값이 `model=granite|qwen3` 으로 ASR 서버에 전달돼 그 모델로 전사한다(granite는
ASR 지시 프롬프트, qwen3는 무프롬프트 자동감지 — server.transcribe_pcm과 동일). 실행 중 바꾸면 **다음 발화부터**
적용(브리지 로그 `cuda asr engine=… model=…`).

`asr_server.py` 가 두 모델을 lazy 로드해 한 포트(:8000)에서 같이 서빙한다(24GB면 둘 다 상주 가능). 한 엔진을
**다른 서버/포트**에 따로 꽂고 싶으면 엔진별 독립이다:
```bash
LCC_CUDA_ASR_GRANITE_URL=http://127.0.0.1:8000/v1/audio/transcriptions
LCC_CUDA_ASR_QWEN3_URL=http://127.0.0.1:8001/v1/audio/transcriptions   # 별도 포트의 다른 서버
```
> ⚠ `asr_server.py` 의 transformers 전사 호출(`_transcribe`)은 **실행 검증을 못 한 스타팅 포인트**다. 모델 카드/
> transformers 버전에 따라 processor·generate 호출이 다르면 그 블록만 한 줄 고치면 된다(각 모델 격리돼 있음).
> vLLM로 granite/qwen3를 서빙한다면 `asr_server.py` 대신 그 OpenAI 엔드포인트로 `LCC_CUDA_ASR_*_URL`만 돌려도 됨.

---

## 8. 트러블슈팅

| 증상 | 해결 |
|---|---|
| 오버레이 "브릿지 연결 끊김" | WSL2의 터미널 3(브리지)이 `ready` 인지. 안 되면 `~/.lcc-cuda.env` 에 `LCC_HOST=0.0.0.0` 추가 후 재시작(localhost 포워딩이 127.0.0.1을 못 넘기는 환경) |
| 브리지에 `asr endpoint NOT reachable` | 터미널 2(asr) 먼저 켜기. 포트 8000 충돌이면 `LCC_ASR_PORT`/`LCC_CUDA_ASR_URL` 같이 변경 |
| 브리지에 `chat endpoint NOT reachable` | 터미널 1(llama) 먼저. GGUF 경로(`LCC_LLAMA_GGUF`)·`llama-server` PATH 확인 |
| 번역이 `<think>...` 같은 게 섞임 | `serve_llama.sh` 의 `--jinja` 가 빠졌거나 템플릿이 무시. llama.cpp 최신 빌드로 |
| `asr_server.py` 가 모델 로드/전사에서 에러 | transformers 버전·모델 카드와 호출이 다를 수 있음 → `_transcribe` 블록만 카드 예시대로 수정(7.5 주의). `torch.cuda.is_available()` 확인 |
| VRAM 부족(OOM) | 안 쓰는 ASR 엔진은 로드 안 됨(lazy). 번역 GGUF를 Q4_K_S/Q3로. `LCC_ASR_DTYPE=float16` |
| 자막이 안 뜸 | 실제 발화 구간인지(무음/음악은 `[no speech]`로 정상 스킵), 탭에서 소리 나는지, 터미널 3 로그에 `[cap …]` 뜨는지 |

포트 점유: `ss -ltnp | grep -E ':(8765|8080|8000)'` → 점유 PID kill.

---

## 메모리 줄이기 (VRAM 빡빡할 때)

- 전사: 주로 쓰는 엔진 하나만 띄우면 그것만 로드된다(lazy). `LCC_ASR_DTYPE=float16` 로 dtype 고정.
- 번역: 더 작은 GGUF 양자화(Q4_K_S / Q3_K_M). 품질 약간↓.
- 26B(~17GB) + granite/qwen3(각 ~2GB) 라 24GB면 여유. 16GB면 ASR 한 엔진 + Q4_K_S 조합부터.

개인 학습/시청용. 모델은 각 라이선스(Gemma·Granite·Qwen) 따름.
