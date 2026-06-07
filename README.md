# Live Caption Every Tab — 모든 사이트 실시간 외국어→한국어 자막

**한국어** · [English](README.en.md) · [日本語](README.ja.md) · [Español](README.es.md) · [中文](README.zh.md)

> 🤖 이 프로젝트는 코드부터 문서까지 **전부 바이브 코딩(AI 페어 프로그래밍)으로** 만들었습니다.

유튜브·트위치·**X**·기타 어떤 사이트든, 브라우저 탭 오디오를 잡아 **로컬 Gemma-4**로 전사+번역해서 영상 위에 2줄 자막(원문/한국어)을 띄운다. (탭 캡처는 도메인 무관이라 소리 나는 탭이면 다 됨)
전사는 **Granite Speech 4.1**(영어 강함)·**Qwen3-ASR**(일어·한국어 등 다국어) 중 팝업에서 고른다. 둘 다 구두점·대소문자를 직접 찍고, 말 없으면 `[no speech]`로 게이트한다.

## 왜 만들었나 (비슷한 도구는 이미 있는데)

실시간 자막/번역 도구는 대체로 두 갈래로 나뉘는데, **"브라우저에서 / 살아있는 아무 탭이나 / 완전 로컬 LLM으로 의미 번역"** 조합이 비어 있어서 그 자리를 채우려고 만들었다.

| | 이 프로젝트 | Whisper 기반 브라우저 확장 | 데스크탑 플레이어 (예: LLPlayer) |
|---|---|---|---|
| **입력** | 소리 나는 **아무 탭**(라이브 스트림 포함) | 탭 오디오 | 받아온 영상 / 플레이어에 넣은 파일·URL |
| **전사(ASR)** | Granite / Qwen3 (구두점·truecasing 네이티브, 무음·음악은 `[no speech]` 게이트) | 주로 Whisper | 주로 Whisper |
| **번역** | **로컬 LLM(Gemma-4)** 의미 번역 — 문맥·대명사 유지 | 없음 / 직역 MT / 클라우드 | 로컬 LLM 가능(Ollama 등) |
| **실행** | 100% 로컬 (클라우드 0) | 로컬~혼합 | 로컬 |
| **대상 언어** | 한국어 우선(+다국어) | 도구마다 | 다국어(언어별 최적화는 제각각) |

- **Whisper 기반 확장**은 탭은 잘 잡지만 전사가 Whisper라 무음/음악 구간에서 환각 자막이 나기 쉽고, 번역은 없거나 직역·클라우드인 경우가 많다. → 여기선 구두점 네이티브 ASR + 무음 게이팅 + 로컬 Gemma 의미 번역으로 그 부분을 다르게 풀었다.
- **데스크탑 플레이어**는 로컬 LLM 번역 품질이 좋지만 영상을 받아오거나 플레이어에 넣어야 해서 라이브 스트림·임의 사이트엔 잘 안 맞는다. → 여기선 받을 필요 없이 **소리 나는 탭이면 그 자리에서** 얹는다.

전부 **로컬·무료**다. 대신 하드웨어 바닥이 있다(아래 [SETUP.md](SETUP.md)의 요구 사항 참고). 가벼운 환경은 번역 모델을 메모리에 맞춰 자동 티어링한다(full/mid/lite).

**플랫폼(backend):** 같은 브리지·같은 확장이 두 런타임에서 돈다. `LCC_BACKEND`로 고른다.
- **`mlx`** (기본, Apple Silicon): 인프로세스 MLX — 전사 Granite/Qwen3, 번역 26B-A4B. → [SETUP.md](SETUP.md)
- **`cuda`** (Windows+NVIDIA, WSL2): OpenAI 호환 **HTTP** — 번역 llama.cpp(26B GGUF), 전사 **Mac과 같은 granite/qwen3**(transformers, `cuda/asr_server.py`). 팝업의 **전사 엔진(영어=granite/다국어=qwen3)** 토글이 그대로 먹고(`model` 필드로 라우팅), 엔진별로 다른 서버에 꽂을 수도 있음. whisper 안 씀. → [SETUP-windows.md](SETUP-windows.md)

VAD·문장 조립·스케줄러·넘버가드·프롬프트 빌더는 **플랫폼 공유**(순수 함수). 런타임이 바뀌는 건 GPU 3함수(전사/번역/요약)뿐이고, 그 경계가 `bridge/backend_cuda.py`(HTTP)와 server.py의 "Backend seam"이다.

## 구조
```
[Chrome 확장] tabCapture(탭 오디오) ──WS(PCM16 16k)──▶ [bridge/server.py]
                                                        VAD + soft-cut ASR atom
                                                        → Granite / Qwen3-ASR 전사 (구두점·다국어)
                                                        → unit assembler
                                                        → 26B-A4B MoE 한국어 번역
   [content.js 오버레이 2줄] ◀──WS(JSON caption)────────┘
```
- ASR은 **두 개의 mlx-audio 엔진** 중 팝업에서 선택(▸ 전사 엔진). **Granite Speech 4.1 2B**(`ibm-granite/granite-speech-4.1-2b` · 영어 충실, WER 0%대)와 **Qwen3-ASR 1.7B**(`Qwen/Qwen3-ASR-1.7B` · 일어·한국어 포함 52언어, 언어 자동감지). 둘 다 구두점·truecasing을 네이티브로 찍어 문장 청킹이 그대로 된다. 26B와 같은 Apple GPU 공유(직렬). ⚠ granite는 mlx-audio **main의 conv 수정** 필요(SETUP 참고).
- 영어 전용 저지연 Parakeet은 파워유저 탈출구 `LCC_ASR_ENGINE=parakeet`로만(CPU·번역과 병렬, 모델 `~/.local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8`, `sherpa-onnx==1.13.2`). 팝업 셀렉터에는 granite/qwen3만 노출.
- 번역: `mlx-community/gemma-4-26b-a4b-it-4bit` (mlx-lm) — 기본 **quality 프롬프트**(expert interpreter·by-meaning·no-translationese + few-shot 3, KV-cache로 비용 amortize → 문어체보다 자연 구어체). 저지연은 `LCC_TX_PROFILE=fast`. **대상 언어 선택 가능**(한/영/일/중/스/프/독), 소스 자동감지, 대상=소스면 스킵.
- RAM ~26GB(가중치) + 청크당 소량 KV. 지연 ~2.9–3.4s/발화 청크(ASR ~0.7s + 번역 ~1.4s + 오디오 prefill + 절 경계 대기)
- MTP는 이 하드웨어에서 무의미해 미사용(MoE·dense·E4B 전부 검증)
- ⚠️ 정품 Chrome/Edge/Brave 필요 — ChatGPT Atlas 등 일부 Chromium 포크는 `chrome.tabCapture` 미구현

## 실행
### 1) 브릿지 서버
```bash
# 저장소 루트에서 (첫 1회는 ./setup.sh 로 venv·의존성 설치)
bash bridge/run_bridge.sh
# "[bridge] ready  ws://127.0.0.1:8765" 뜨면 준비 완료 (첫 로드 ~40s)
```
- 항상 켜두려면(옵트인, 크래시 자동재시작): `bash bridge/autostart.sh install` — ⚠ ~26GB RAM 상주. 끄기: `… uninstall`
- 터미널 없이 **팝업 버튼**으로 켜고/끄려면(`🚀 브릿지 켜기`): `bash extension/native-host/install-host.sh` 1회 실행 후 확장 새로고침 (네이티브 메시징 호스트 — SETUP 6.5). detached로 떠서 브라우저 닫아도 유지
- 브릿지가 재시작/끊겨도 확장이 **자동 재연결**(백오프)하고 최근 오디오를 최대 6초 버퍼링함. 그보다 긴 장애 동안의 발화는 유실될 수 있음
### 2) 확장 로드 (Chrome)
1. `chrome://extensions` → 우측 상단 **개발자 모드** 켜기
2. **압축해제된 확장 프로그램을 로드** → 이 저장소의 `extension/` 폴더 선택
3. 유튜브/트위치 영상 탭에서 **확장 아이콘 클릭** → 팝업의 **`▶ 자막 시작`** 클릭 → 배지 `ON`, 오버레이 등장
4. 팝업 설정: 자막 **크기·상하/좌우 위치·원문 줄·싱크 보정** (실시간), **문장 대기·음성감지**(재시작 시 적용)
5. 다시 `■ 자막 중지`. (tabCapture는 사용자 클릭 제스처 필수 → 자동시작 불가)

## 기능
- **자동 용어 프라이밍**: 페이지/영상 제목을 ASR·번역 힌트로 자동 주입 (팝업에서 끄기)
- **영상 종류 프리셋**: 팝업에서 콘텐츠 유형(일반·잡담 / 컨퍼런스·강연 / 뉴스·인터뷰 / 개인 스트리밍)을 한 번 고르면 말투(register)와 지연 모드를 묶어서 맞춘다 — 강연=격식·안정, 뉴스=균형, 스트리밍=구어·즉각. 어조·종결어미·few-shot 앵커가 콘텐츠에 맞게 바뀌고, 소스 언어(EN/JA)도 자동감지해 맞는 예시를 고름
- **용어집**: 팝업에 `이름=번역`(줄마다 하나)을 넣으면 그 용어를 전사 바이어싱 + 번역에서 항상 같게 렌더링(이름이 줄마다 다르게 번역되는 흔들림 제거). `용어 힌트`는 자유 텍스트 바이어싱
- **정확도 모드(2패스 재전사)**: 켜면 자연 종료(pause/eos)나 종결부호로 확정되는 다절(多節) 문장의 누적 오디오를 확정 직전 한 번 더 통째로 전사 → VAD 조각 이어붙임 경계 오류 제거. 확정이 ~0.7s 느려져 토글(기본 OFF). 오버랩/스플릿으로 정렬이 깨진 유닛은 자동 제외(`unit_pure` 가드)
- **스트리밍 자막**: 원문은 ASR atom 단위로 먼저 뜨고, 한국어 preview는 debounce/coalesce됨. 확정 자막은 final queue에서 우선 처리
- **지연 모드 3단계**: `공격`은 Parakeet CPU 전사와 MLX 번역을 최대한 겹치고 현재 unit preview를 latest-only로 미리 번역, `균형`은 MLX idle일 때만 preview, `안정`은 확정 번역만 표시. final 번역은 항상 preview보다 우선
- **Lookahead 영상 지연**: 영상 지연 모드에서는 실제 오디오는 즉시 전사·번역하고, 자막은 실제 PCM 스트림 시작 clock과 발화 구간(`start_ms`/`end_ms`)에 맞춰 예약 출력. popup의 싱크 보정으로 ±2초 미세 조정 가능
- **싱크 디버그**: popup에서 켜면 자막 아래와 console에 `kind/unit/start/end/due/now/lag/delay/offset/q`를 표시해 실제 출력이 due time보다 빠른지 확인 가능
- **번역 캐시/우선순위**: preview와 final이 같은 source면 재번역을 피하고, final 번역은 preview보다 먼저 처리
- **자막 기록**: 우하단 📜 → 스크롤백 패널 / 이중언어 `.md` 내보내기
- **요약·질문**: 패널의 ✨요약 · 질문창 — 로컬 26B가 지나간 자막을 요약/질의응답 (스트리밍)

## 트러블슈팅
- 오버레이에 "브릿지 연결 끊김" → `run_bridge.sh` 실행 중인지, 포트 8765 확인
- 자막이 안 뜸 → 영상에 실제 발화가 있는지(비음성은 `[no speech]`로 스킵됨), 탭에서 소리가 나는지
- 소리가 안 들림 → 탭 캡처가 재생을 가로채는 경우. offscreen이 `source→destination` 재생 연결을 유지하므로 보통 정상
- 포트 점유 에러 → `lsof -ti:8765 | xargs kill -9`

## 튜닝 레버
- 지연 줄이기: 번역은 기본 quality 프롬프트(KV-cache로 비용 amortize). 더 줄이려면 `LCC_TX_PROFILE=fast`로 compact 프롬프트를 쓰고 `SEG_SILENCE_MS`/`SOFT_MAX_SEC`를 낮춘다. 긴 정확도 모드에서 잘림이 보이면 `LCC_ASR_MAX_TOKENS=96`만 올린다.
- 병렬 체감: 영어 방송은 팝업에서 `Parakeet + 공격`을 기본으로 둔다. 공격 모드는 effective sentence silence <=900ms, pending commit 120자/1.8s, preview debounce 180ms, final recent context 2개, preview context 0개로 MLX 번역 레인을 짧게 쓴다. Parakeet soft-cut은 오인식 중복을 피하려고 4.0s를 유지한다. 자막이 자주 갈아끼워져 거슬리면 `균형`, 번역 안정성이 최우선이면 `안정`으로 낮춘다. 서버 기본값은 `LCC_LATENCY_MODE=aggressive`이며 `stable|balanced|aggressive`를 받는다.
- 출력 싱크: bridge는 4.5초 soft-cut + 220ms overlap으로 긴 발화를 전사하고, 화면은 `performance.now()` 기반 stream clock으로 예약한다. final backlog가 실제로 밀릴 때만 짧은 자막을 병합
- 영상 지연: `delaySec`는 최대 12초. `videoDelay` 모드는 원본 video frame 해상도로 캡처하고, 프레임은 최대 60fps로만 제한한다. frame timestamp는 `requestVideoFrameCallback` metadata를 우선 사용하고, PCM tap은 AudioWorklet 우선으로 처리
- 번역 품질↑: 팝업의 **말투** 프리셋을 콘텐츠에 맞추고, **용어집**에 고유명사를 핀. 더 깨끗한 전사가 필요하면 **정확도 모드**(2패스)를 켠다. 최후 수단으로 번역 모델을 31B dense로(5배 느려짐). 벤치: `bench_translate_quality.py`(말투/용어집 A/B), `bench_2pass.py`(2패스 vs 1패스) — 둘 다 브릿지 정지 후 실행
- 환각/잡음 민감도: `webrtcvad.Vad(0..3)` 공격성 조절
- 로컬 WS 보호: 기본은 Chrome extension origin + client token만 허용. token을 바꾸려면 `LCC_WS_TOKEN`과 `extension/protocol.js`를 같이 맞춰야 함
