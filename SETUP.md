# Live Caption Every Tab — Mac 설치 가이드 (처음부터)

유튜브·트위치·**X** 등 **어떤 사이트든 브라우저 탭의 외국어 음성을 실시간 한국어 자막**으로 띄우는 도구. (소리 나는 탭이면 도메인 무관)
전부 **로컬에서** 돌아간다(클라우드 X, 무료). Apple Silicon Mac + 로컬 Gemma-4 모델 사용.

> 처음 1번만 ~15분(주로 모델 다운로드). 이후엔 `브릿지 실행 → 확장 클릭` 두 단계.

---

## 0. 요구 사항 (먼저 확인!)

| 항목 | 필요 | 비고 |
|---|---|---|
| **Mac** | **Apple Silicon (M1~M4)** | Intel Mac 불가 (mlx가 Apple GPU 전용) |
| **메모리(RAM)** | **32GB 권장** (최소 24GB) | 모델이 ~26GB 상주. 16GB는 사실상 불가 |
| **브라우저** | **정품 Google Chrome** (또는 Edge/Brave) | ⚠️ **ChatGPT Atlas·Arc 등 일부 포크는 탭 캡처 미지원 → 안 됨** |
| **디스크** | ~30GB 여유 | 모델 캐시 |

RAM이 부족하면 → 맨 아래 **"메모리 줄이기"** 참고.

---

## 1. 기본 도구 설치 (Homebrew·Python·ffmpeg)

터미널(Terminal.app)을 열고:

```bash
# Homebrew (이미 있으면 건너뜀)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.13 + ffmpeg
brew install python@3.13 ffmpeg
```

Chrome가 없으면 https://www.google.com/chrome 에서 설치.

---

## 2. 코드 받기

```bash
git clone https://github.com/teukboong/LiveCaptionEveryTab.git
cd LiveCaptionEveryTab     # bridge/ extension/ 가 보이는 폴더
```

이후 명령들은 **이 폴더(저장소 루트) 안에서** 실행한다고 가정.

---

## 3. Python 환경 + 라이브러리

한 줄 설치(권장) — 저장소 안에 `.venv`를 만들고 Apple Silicon 의존성을 깐다:

```bash
./setup.sh            # python3 -m venv .venv  +  pip install '.[mlx]'
```

수동으로 하려면:

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install '.[mlx]'   # core + mlx-lm + mlx-audio(전사 엔진)
```

> 전사 엔진(granite·qwen3)은 mlx-audio로 돈다. granite-speech-4.1의 conv 수정이 PyPI 0.4.3엔 없고
> git main에 있어 pyproject가 main을 핀해 둔다(0.4.4 릴리스되면 핀 교체). 다른 venv를 쓰면 실행 시
> `LCC_PYTHON=/그/경로/bin/python` 으로 지정하면 됨.

---

## 4. 모델 다운로드 (~26GB, 1번만)

HuggingFace에서 자동으로 받지만, 미리 받아두면 첫 실행이 빠름:

```bash
.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download as d
for r in ["ibm-granite/granite-speech-4.1-2b",        # 전사 — 영어 (Granite) ~5GB
          "Qwen/Qwen3-ASR-1.7B",                       # 전사 — 일어·다국어 (Qwen3-ASR) ~4GB
          "mlx-community/gemma-4-26b-a4b-it-4bit"]:    # 한국어 번역 (full 티어) ~14GB
    print("downloading", r); d(r)
print("완료")
PY
```

(받다 끊겨도 다시 실행하면 이어받음.) RAM이 작으면 번역은 **티어**가 자동으로 더 작은 모델(E4B/E2B)을
고른다 — 아래 "메모리 줄이기" 참고. 자동감지가 고른 모델은 첫 실행 로그(`[bridge] tier=…`)에 찍힌다.

---

## 5. 브릿지(로컬 서버) 실행

```bash
bash bridge/run_bridge.sh
```

- 모델 로딩에 **~40초**. **`[bridge] ready  ws://127.0.0.1:8765`** 가 뜨면 준비 완료.
- 이 터미널 창은 **켜둔 채로** 둔다(자막 쓰는 동안 계속 실행).
- 끄려면 그 창에서 `Ctrl+C`.
- 터미널 없이 **팝업 버튼으로 켜고 끄고 싶으면** → 7번 아래 "팝업에서 브릿지 켜기" 참고(1회 설치 후엔 터미널 불필요).

---

## 6. Chrome 확장 설치 (1번만)

1. Chrome 주소창에 `chrome://extensions` 입력 → 이동
2. 우측 상단 **개발자 모드** 켜기
3. **압축해제된 확장 프로그램을 로드** 클릭 → 받은 폴더 안의 **`extension`** 폴더 선택
4. (선택) 툴바 퍼즐🧩 아이콘 → **Live Caption Every Tab** 옆 **📌 고정**

---

## 6.5. (선택) 팝업에서 브릿지 켜기 — 터미널 없이

매번 터미널에서 `run_bridge.sh`를 치기 싫으면, 팝업의 **`🚀 브릿지 켜기`** 버튼으로 켜고/끌 수 있다. 브라우저는 보안상 로컬 프로그램을 직접 못 띄우므로 **네이티브 메시징 호스트**를 1회 설치한다(터미널 한 줄):

```bash
bash extension/native-host/install-host.sh
```
- 설치 후 `chrome://extensions`에서 확장을 **↻ 새로고침**(확장 ID가 고정 ID로 바뀜).
- 이제 팝업 상단의 **`🚀 브릿지 켜기`** → 모델 로드(~40초) 후 **🟢 켜짐**. 옆 **⏹** 로 종료.
- 이렇게 띄운 브릿지는 **detached** 라 브라우저를 닫아도 계속 돈다(끄려면 팝업 ⏹ 또는 `bash extension/native-host/install-host.sh uninstall` 후 수동).
- 제거: `bash extension/native-host/install-host.sh uninstall`

---

## 7. 사용법

1. **브릿지가 실행 중인지 확인** (5번 터미널에 `ready`).
2. 유튜브·트위치·X 등 **소리 나는 탭**으로 가서 재생.
3. **확장 아이콘 클릭** → 팝업의 **`▶ 자막 시작`** 클릭.
4. 영상 위에 **원문(위) + 한국어(아래)** 2줄 자막 등장. 발화 후 ~3–4초 지연.
5. 멈추려면 팝업의 **`■ 자막 중지`**.

### 팝업 설정
- **한국어 크기 / 상하·좌우 위치 / 원문 줄 표시 / 자막 보정** → 즉시 적용
- **말투**(캐주얼·강연·뉴스·잡담) → 콘텐츠에 맞추면 어조·종결어미가 자연스러워짐
- **용어집** → `이름=번역`을 줄마다 하나씩 (예: `Blackwell=블랙웰`). 그 용어를 전사·번역에서 항상 같게 고정
- **정확도 모드** → 켜면 문장을 통째로 한 번 더 전사해 경계 단어 오류↓ (확정이 살짝 느려짐)
- **문장 대기**(끊는 타이밍) / **음성 감지**(잡음·음악 무시 정도) → *자막 다시 시작* 시 적용
- ※ 말투·용어집·정확도·문장 대기·음성 감지는 **자막 다시 시작** 시 반영됨

---

## 8. 트러블슈팅

| 증상 | 해결 |
|---|---|
| 팝업 "실패: ...not been invoked..." | **Chrome이 아닌 브라우저**(Atlas/Arc 등). 정품 Chrome/Edge/Brave에서 실행 |
| "Cannot capture a tab with an active stream" | `chrome://extensions`에서 확장 **↻ 새로고침** 후 다시 |
| 자막이 안 뜸 | ① 브릿지 `ready` 확인 ② 유튜브 탭 **F5** 후 다시 시작 ③ 실제 *발화* 구간인지(음악/무음은 자막 안 뜸=정상) |
| 오버레이에 "브릿지 연결 끊김" | 브릿지(터미널)가 실행 중인지, 포트 8765 |
| `run_bridge.sh: Python venv를 못 찾음` | 3번 venv 경로 확인. 다르면 `LCC_PYTHON=경로/bin/python bash bridge/run_bridge.sh` |
| 너무 느림 / RAM 부족 | 아래 "메모리 줄이기" |

확장이 메시지를 받는지 보려면: 유튜브 페이지에서 `F12` → Console 에 `[lcc] content recv: caption ...` 가 뜨면 정상.

---

## 메모리 줄이기 (RAM 빡빡할 때)

번역 모델은 **사용 가능한 메모리(유휴 VRAM)에 맞춰 자동으로 티어가 정해진다** — 손 안 대도 됨.
아무것도 안 하면 첫 실행에서 여유 메모리를 재서 가장 큰 티어를 고르고, `[bridge] tier=…` 로그에 근거를 찍는다.

| 티어 | 번역 모델 | 가중치 | 권장 여유 메모리 |
|---|---|---|---|
| `full` | gemma-4-26B-A4B | ~14GB | ~22GB 이상 |
| `mid`  | gemma-4-E4B (QAT) | ~5GB | ~12GB |
| `lite` | gemma-4-E2B (+ASR 0.6B) | ~3.2GB | ~8GB |

티어를 **고정**하려면(매 실행 동일) `.env`에 한 줄:

```bash
# .env  (cp .env.example .env)
LCC_LM_TIER=mid          # full | mid | lite
# 또는 특정 모델 직접: LCC_LM_MODEL=mlx-community/gemma-4-26b-a4b-it-6bit
```

미리 받아두려면(4번) 해당 티어 모델 id를 받으면 됨. 자동감지 임계값은 `.env.example`의 `LCC_LM_NEED_*`로 조정 가능.

---

## 동작 원리 (궁금하면)

```
[Chrome 확장] 탭 오디오 캡처 → WebSocket(16k PCM) → [로컬 브릿지(Python)]
   VAD + soft-cut으로 짧은 ASR atom 생성
   → Granite/Qwen3 (mlx-audio) 전사  ("말 없으면 [no speech]" → 환각 자막 방지)
   → atom을 번역 가능한 unit으로 조립
   → 문장/절 완성 시 Gemma-4-26B-A4B(MoE) 한국어 번역
      - final 번역 우선
      - preview 번역은 debounce/coalesce/drop
      - 같은 source는 cache hit로 재번역 회피
[content script 오버레이 2줄] ◀── WebSocket(자막 + start_ms/end_ms) ──┘
```

- 일반 자막 도구가 쓰는 Whisper는 음악·무음에서 "시청해주셔서 감사합니다" 같은 **환각 자막**을 뱉는데, 이 도구는 Gemma 오디오 모델에 "말 없으면 출력 금지"를 지시해 원천 차단.
- 영어는 한국어와 어순이 반대라, 원문 줄은 먼저 보여주고 한국어는 preview와 final을 분리한다. final은 문장/절 단위로 기록에 남는다.
- 번역은 최근 final 문맥을 유지해서 대명사·용어가 일관됨.
- 영상 지연 모드는 실제 오디오를 먼저 bridge로 보내고, bridge에 들어가기 시작한 PCM clock을 content script의 `performance.now()` 기준으로 보정해 지연된 영상/소리가 그 발화 구간에 도착할 때 자막을 출력한다. 자막은 기본적으로 `end_ms + delaySec + 싱크보정` 쪽으로 잡아, 번역 latency는 지연 buffer 안에 숨기되 청크 전체 번역이 발화보다 먼저 드러나지 않게 한다. canvas capture는 원본 video frame 해상도를 유지하고 최대 60fps로 제한하며, frame buffer timestamp는 `requestVideoFrameCallback` metadata를 우선 사용한다.

개인 학습/시청용. 모델은 각 라이선스(Gemma) 따름.
