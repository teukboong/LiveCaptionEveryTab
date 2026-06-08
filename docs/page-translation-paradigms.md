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

남은 다음 단계 후보:

- **Inline ghost** — 지금 ghost는 hover overlay뿐이다. 최종 번역 text node *옆*에 원문을 항상 아주 가볍게 보존하는 inline 변형을 더하면, hover 없이도 원문이 보인다.
- **semantic block batching** — 지금은 text node 중심으로 microbatch를 만들지만, paragraph/list/card 단위로 주변 sibling text를 함께 hint로 보내면 긴 글 번역 품질이 오른다. 실제 replacement는 node별로 하되, prompt에는 block context를 준다.
