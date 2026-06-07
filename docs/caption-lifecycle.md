# Caption Lifecycle — 표시 모델 SSOT

이 문서는 "한 발화가 화면에 **언제 무엇으로** 나타나는가"의 단일 기준(SSOT)이다.
표시·정렬·staleness 규칙은 bridge(`server.py`)와 client(`content.js`/`delay.js`) 두 곳에 흩어져
있으므로, 그 규칙을 여기서 한 번 정의하고 양쪽 코드는 이 문서를 따른다. 표시 동작을 바꾸려면
**먼저 이 문서를 고치고** 그다음 양쪽 코드를 맞춘다.

> 범위: 표시(display) 계약만. ASR/번역/VAD 내부 알고리즘은 코드와 주석이 SSOT.

---

## 1. 핵심 개념

| 개념 | 정의 | 소유자 |
|---|---|---|
| **unit** | 번역 단위(보통 한 문장/절). `unit_id`는 연결 내 **단조 증가**. | bridge `next_unit()` |
| **rev** | 한 unit 내 **소스 개정 번호**. 소스가 자라거나 다시 전사되면 +1. | bridge `current_rev` |
| **start_ms/end_ms** | 발화 구간(bridge `audio_ms` 기준). 영상모드 cue 정렬에 사용. | bridge |
| **phase** | 그 메시지의 출처/완성도 표식(아래 표). client가 provenance로 사용. | bridge |

**불변식**
- `unit_id`는 한 연결 안에서만 의미가 있고 단조 증가한다. 재연결(`reanchor`/WS 재오픈)하면
  `audio_ms`가 0으로 리셋되므로 **이전 unit_id는 전부 무효**다.
- 같은 unit에 대해 `rev`는 단조 증가한다. 더 작은 `rev`/`unit_id`는 **stale**이며 더 최신을 덮으면 안 된다.
- 한 unit은 정확히 한 번 **final**(`type:"caption"`)로 확정된다(`finalized_units`로 보장).

---

## 2. 메시지 계약 (bridge → client)

세 가지 `type`만 나간다. 모두 `unit_id`/`rev`/`start_ms`/`end_ms`를 포함.

### 2.1 `source` — 원문 라인 (번역 전)
```
{ type:"source", text, unit_id, rev, start_ms, end_ms }
```
- emit: `emit_source()` (LA 확정 prefix / 절 누적 / 커밋 직전).
- 의미: "이 unit의 원문이 여기까지 확정/성장했다." 번역은 아직 없거나 뒤따른다.

### 2.2 `caption_partial` — 진행 중 번역 (확정 아님)
```
{ type:"caption_partial", kind:"preview"|"final_stream", phase, unit_id, rev,
  source, ko, start_ms, end_ms, display_ms, ... }
```
- `kind:"preview"` (`phase:"preview"`): 확정 **전** 미리 번역. latency mode·debounce·busy 게이트로 제어,
  언제든 stale로 폐기될 수 있음(`preview_is_stale`).
- `kind:"final_stream"` (`phase:"final_stream"`): **확정 번역이 토큰 단위로 스트리밍** 중. 이 unit은
  곧 final로 확정된다. `_stream_partial_should_emit` 게이트로 토막·역행을 억제.
- `phase:"degraded_stream"` / `degraded:true`: 번역 실패로 마지막 양호한 KO partial을 degraded로 노출.

### 2.3 `caption` — 확정 (commit)
```
{ type:"caption", kind:"commit", phase:"final"|"degraded_stream", unit_id, rev,
  source, ko, start_ms, end_ms, display_ms, reason, degraded, translation_error, ... }
```
- emit: `translation_loop`(final job).
- 의미: "이 unit의 **최종** 자막." 한 unit당 한 번. `reason` = punct/eos/cap/age/pause.

### phase 값 요약
| phase | type/kind | 뜻 |
|---|---|---|
| `preview` | caption_partial/preview | 확정 전 미리 번역(폐기 가능) |
| `final_stream` | caption_partial/final_stream | 확정 번역 스트리밍 중 |
| `degraded_stream` | caption_partial 또는 caption | 번역 실패 폴백(마지막 KO partial) |
| `final` | caption/commit | 최종 확정 |

---

## 3. 한 unit의 정상 생애

```
 (말 시작)
   │  LA 확정 prefix가 자람 (LCC_LA=1일 때)
   ├─▶ source(rev↑)                      ← 원문 라인 갱신
   │  절이 누적/문장 경계 미도달
   ├─▶ caption_partial preview (debounce) ← 확정 전 미리보기(있을 수도/없을 수도)
   │  문장 경계(punct) 또는 pause/eos/cap/age
   ├─▶ source(최종 원문)
   ├─▶ caption_partial final_stream …     ← 확정 번역 토큰 스트리밍
   └─▶ caption commit (phase:final)       ← 확정. 이후 이 unit은 finalized.
```

- **preview는 선택적**이다. busy/stale/latency-mode에 따라 안 나올 수 있다(`preview_startable`).
- **final_stream도 선택적**이다(aggressive mode + 게이트 통과 시). 안 나오면 preview에서 바로 commit으로 점프.
- preview의 소스가 final 소스와 충분히 같으면 **preview promotion**으로 재번역을 건너뛴다
  (`_preview_promotable`, 유사도 ≥ `PREVIEW_PROMOTE_SIMILARITY`).

---

## 4. Staleness & 순서 규칙 (반드시 지켜야 하는 것)

bridge와 client 양쪽에서 이 규칙들이 거울처럼 적용된다.

1. **epoch 가드** (bridge): LA partial은 `speech_epoch`가 바뀌면(이전 발화의 결과) 폐기.
   `server.py` inference_loop의 partial 분기.
2. **unit 단조성** (both): `new.unit_id < current.unit_id`인 live/final_stream은 **드롭**.
   - client: `content.js` caption_partial 분기 stale 가드, `delay.js` `live()` 가드.
3. **preview 무효화** (bridge): preview job은 다음 중 하나면 폐기 — unit이 finalized / `current_unit_id`가
   다름 / `current_rev`가 다름 / `latest_preview_rev`가 다름 (`preview_is_stale`).
4. **한 번만 final** (bridge): `finalized_units`로 같은 unit 재확정 차단.
5. **in-place 확정** (client, audio mode): 이미 `final_stream`을 화면에 **보여준** unit은, 그 commit이
   도착했을 때 — (a) 아직 같은 unit을 보고 있으면 **그 자리에서 solid final로 교체**, (b) 이미 다음
   자막으로 넘어갔으면 **재생하지 않음**(드롭). 큐에 남은 동일 unit final도 제거.
   `content.js` `lccStreamedFinalUnits`/`lccDropQueuedUnit`/`replacingVisibleStream`.
6. **재연결 무효화** (client): WS 재오픈/`reanchor` 시 모든 cue·live·koState 리셋(이전 audio_ms 무효).

---

## 5. Client 재구성 (mode별로 다른 두 경로)

### 5.1 audio mode — `content.js` 페이서
- 150ms 페이서(`lccPace`)가 **final 큐**(`lccFinalQ`, dueAt 정렬)와 **live partial**(`lccLivePartial`)을 구동.
- `source`/`preview`/`final_stream` → `lccLivePartial`로 들어가 화면의 "진행 중" 라인을 그린다.
- `caption`(final) → 큐에 예약하거나, §4-5의 in-place 규칙으로 즉시 교체/드롭.
- **KO LocalAgreement split** (`lccKoSplitInto`/`lccKoState`): 한국어 라인을 stable(solid) / draft(dim)로
  쪼개, 확정된 머리는 고정하고 꼬리만 흐리게. preview·final_stream에 적용.
- lag이 크면(`LCC_LAG_CAP_MS`) 짧은 final들을 **merge**해 따라잡음(`lccTakeFinal`).

### 5.2 video mode — `delay.js` cue track
- 페이서 없음. 지연 캔버스가 `performance.now()` 기반 한 clock을 쓰고, cue는 `start_ms`/`end_ms`로
  **화면에 실제 떠 있는 프레임**에 잠긴다(`renderSub`).
- `caption` → cue 누적(`s.cues`), `source`/partial → `s.live`(in-progress 라인).
- content.js는 video mode에서 자기 페이서 대신 `window.__lccVideoSub`로 라우팅(`v.final`/`v.live`).

---

## 6. 두 개의 LocalAgreement (혼동 주의)

서로 다른 층의 LA가 **둘** 있다. 이름이 겹쳐 헷갈리기 쉬움:

| | 위치 | 대상 | 목적 |
|---|---|---|---|
| **source-side LA** | bridge `_lcp_words` | **원문(영어) 단어** | 두 연속 가설이 합의한 prefix를 확정 source로 스트리밍(n=2) |
| **KO-side LA** | client `lccKoSplitInto` | **한국어 단어** | 확정 prefix를 solid, 발산 꼬리를 dim으로 표시 |

source-side는 "무엇을 원문으로 확정하나", KO-side는 "번역 라인의 어디까지 굳었나"를 다룬다.

---

## 7. Latency mode가 표시에 미치는 영향

| mode | preview | final_stream | 특징 |
|---|---|---|---|
| `aggressive` | latest-only 미리 번역 | 켬(게이트 통과 시) | Parakeet CPU ∥ MLX 최대 겹침 |
| `balanced` | MLX idle일 때만 | 보통 끔 | |
| `stable` | **없음** | 없음 | 확정 번역만 표시 |

`final` 번역은 항상 `preview`보다 우선(priority 0 vs 5, `trans_q`).

---

## 8. 이 문서를 바꿔야 하는 변경들

- 새 `phase`/`kind`/메시지 필드 추가·의미 변경
- staleness/순서 규칙 변경(§4)
- audio/video client 재구성 로직 변경(§5)
- latency mode의 표시 효과 변경(§7)

그 외(번역 품질, KV reuse, VAD 튜닝 등)는 코드·벤치가 SSOT다.
