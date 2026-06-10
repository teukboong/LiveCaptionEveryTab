# Page Translation Paradigms

이 문서는 LiveCaptionEveryTab의 페이지 번역을 단순한 "DOM text replace"가 아니라 브라우저용 번역 인터페이스로 확장하기 위한 아이디어 노트다.

## 1. Living DOM Translation

페이지 텍스트를 완성 결과가 올 때까지 기다렸다가 한 번에 바꾸지 않고, 모델이 생성 중인 현재 segment를 `dom_translate_partial`로 흘려보낸다. 브라우저는 해당 partial을 실제 text node에 speculative하게 칠하고, `dom_translate_result`가 오면 확정한다.

핵심 원칙:

- partial은 캐시에 저장하지 않는다.
- partial은 request ownership을 통과한 현재 pending work에만 적용한다.
- final result가 오면 partial을 덮어쓴다.
- final result가 없거나 오류가 나면 partial을 원문으로 복원하고 requeue한다.
- partial은 viewport 안의 짧은 text node에만 적용한다.
- `prefers-reduced-motion: reduce`에서는 partial을 적용하지 않는다.

이 방식은 페이지가 "번역되는 중"임을 눈으로 보여주면서도, 최종 번역 품질과 캐시 오염을 분리한다.

## 2. Translation Lens, not Translation Page

페이지 번역을 "원문을 없애는 기능"이 아니라 "언어 view를 바꾸는 렌즈"로 볼 수 있다.

가능한 모드:

- **Native Lens**: 현재처럼 DOM을 대상 언어로 치환한다.
- **Bilingual Lens**: hover/focus 시 원문을 툴팁이나 inline ghost로 보여준다.
- **Semantic Lens**: 제목, 버튼, 댓글, 본문, 가격, 코드, 날짜 등 노드 역할별로 번역 정책을 달리한다.
- **Reading Lens**: viewport 중심으로 읽는 순서를 예측하고, 눈앞의 문단을 먼저 번역한다.
- **Archive Lens**: 페이지별 번역 memory를 저장해 같은 문맥의 같은 표현은 즉시 물성화한다.

중요한 패러다임 전환은 "전체 페이지를 번역 완료 상태로 만드는 것"보다 "사용자가 읽으려는 영역이 항상 먼저 번역되어 있는 것"이다.

## 3. Materialized Translation View

브라우저 확장은 페이지 DOM 자체를 데이터베이스처럼 보고, 번역 결과를 하나의 materialized view로 유지할 수 있다.

```text
source DOM node + page context + target + register + glossary
  -> translation key
  -> speculative partial
  -> confirmed final
  -> page cache / label cache
```

이 관점에서는 mutation observer가 단순 scan loop가 아니라 "view invalidation engine"이 된다. URL, target language, register, glossary, context hint가 바뀌면 관련 materialized view를 무효화하고 원문에서 다시 view를 구성한다.

## 4. Trust Gradient UI

모든 번역을 똑같이 확정처럼 보여주지 않는다.

- partial: 짧게 살아 움직이는 speculative text
- final: 일반 텍스트
- cached: 즉시 표시하되 필요하면 낮은 priority로 background verify
- glossary-hit: 용어 고정 표시
- unchanged: 원문 유지
- failed/requeued: 원문 유지 또는 아주 짧은 shimmer

현재 구현은 partial/final/cache의 안정성 계층을 코드상으로 분리한다. 이후 CSS class나 data attribute를 추가하면 시각적 trust gradient까지 줄 수 있다.

## 5. Locality-First Translation

페이지 번역의 병목은 "문서 전체 번역"이 아니라 "사용자가 보는 곳의 latency"다. 따라서 priority는 다음 순서가 좋다.

1. viewport 안의 UI label과 heading
2. viewport 안의 본문 첫 문장
3. viewport 근처의 다음 paragraph
4. 동일 source text fan-out
5. idle time의 below-the-fold prefetch

이 프로젝트는 이미 hot/cold queue, dedup fan-out, idle prefetch 구조를 갖고 있으므로, Living DOM Translation과 잘 맞는다.

## 6. 진행 상태와 다음 단계

이미 들어간 것:

- **Bilingual Ghost Mode (hover)** — 팝업 `원문 보기 (번역 위에 마우스)`(`pageBilingual`). 번역된 text node에 마우스를 올리면 원문을 가볍게 되살려, 번역을 신뢰하되 원문을 잃지 않는다.
- **cache-then-verify** — 팝업 `캐시 번역 idle 재확인`(`pageVerify`). 캐시된 번역은 즉시 표시하고, idle time에 모델이 다시 확인해서 바뀐 경우에만 조용히 patch한다.
- **Inline ghost** — 팝업 `원문 같이 보기`(`pageBilingualInline`). 번역 적용된 긴 문단 블록 아래에 원문을 옅게 상시 표시(`data-lcc-orig` + CSS `::after`, 복원 가능). Bilingual Lens의 inline 변형.
- **semantic block batching** — Policy A(anchor-collapse: 스타일로 쪼개진 leaf 블록을 한 segment로 번역해 anchor에 접기) + Policy R(⟦n⟧ placeholder로 링크/버튼을 자리 보존하며 블록을 한 문장으로 번역). per-node 경로도 주변 블록 텍스트를 reference-only ctx로 싣는다.
- **탭 의미 메모리 (Archive Lens의 용어판)** — 자막 final에서 채굴한 반복 용어가 도메인별로 영속화되어(`term-memory.js`), 같은 사이트 재방문 시 자막·페이지 양쪽 glossary에 자동 시드된다.
- **듀얼 모델 라우팅** — aux 번역기 상주 시 짧은 DOM 배치는 aux(즉시), 긴 문단·verify는 main(품질). `dom_translate_result.engine` + pageVerify 조합으로 "speed layer 페인트 → quality layer 확인"이 Trust Gradient의 실행판이 된다.
- **iframe 커버리지** — 실콘텐츠 프레임 전부에서 페이지 번역이 돈다(프레임별 큐/캐시·요청 id 태그).
- **write-back (역방향 렌즈)** — 입력창에서 내 글을 페이지 언어로(⇄ 칩/Alt+T, main 모델, 되돌리기 지원). 읽기 렌즈를 '참여'로 확장.
- **이미지 OCR 번역** — Alt+이미지 hover → 렌더된 픽셀 캡처 → Apple Vision OCR → 페이지 번역 경로 → 위치 맞춘 오버레이. DOM 밖 텍스트(짤·스크린샷)까지 렌즈가 닿는다.

남은 다음 단계 후보:

- **Trust Gradient 시각화** — engine/cached/glossary-hit이 이미 데이터로 흐르므로, CSS class/data attribute로 살짝 노출하면 §4의 시각적 등급이 완성된다.
- **Reading Lens 고도화** — 스크롤 속도/방향 예측으로 prefetch 우선순위를 더 똑똑하게.
