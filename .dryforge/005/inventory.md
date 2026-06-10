# inventory — S4 content.js split

Authority: `.dryforge/005/spec.md` > `.dryforge/005/plan.md` > `.dryforge/005/handoff.md`.
Generated before any S4 code split, from `extension/content.js` at `fdeeb5d`.

## 1. Current SSOT Load Order

All three currently agree:

| Surface | Current order |
|---|---|
| `extension/manifest.json` `content_scripts[0].js` | `protocol.js`, `pcm.js`, `page-seed.js`, `content.js`, `delay.js` |
| `extension/background.js` `LCC_CONTENT_FILES` | `protocol.js`, `pcm.js`, `page-seed.js`, `content.js`, `delay.js` |
| `tools/quality_gate.py` expected | `protocol.js`, `pcm.js`, `page-seed.js`, `content.js`, `delay.js` |

S4 target order after all splits:
`protocol.js`, `pcm.js`, `page-seed.js`, `caption-overlay.js`, `caption-scheduler.js`,
`page-translator.js`, `content.js`, `delay.js`.

## 2. File Assignment

| Lines | Names / block | Target | Reason / load-order note |
|---:|---|---|---|
| 2, 4-112 | `box`, `lccPeek`, `lccLastSrc`, `LCC_IS_TOP`, `lccShouldRender`, `host`, `ensureBox`, `applySettings`, `setSrc`, `lccRenderKoText`, `setLines`, `setKoSplit`, `setLinesSplit` | `caption-overlay.js` | Caption DOM render and fullscreen host helpers. `settings` stays in `content.js`; runtime reads are safe after all scripts load. |
| 3, 113-134 | `settings`, storage load/change listener | `content.js` | Shared config owner. Load-time listener touches `lccReclockPending`, so it stays in the last script. |
| 136-161 | `LCC_PEEK_DEBUG`, `lccSetPeek`, `lccEditableTarget`, Alt source-peek listeners | `caption-overlay.js` | Overlay interaction. Listener callbacks run after full load; `lccEditableTarget` is also used by residual content features, so it moves with overlay and remains global. |
| 162-227 | `lccGlossBar`, `lccAddGlossary`, `lccEnsureGlossBar`, `lccCloseGlossBar`, `lccToggleGlossBar`, Alt+G listener | `content.js` | Glossary storage and popup-config routing; not caption rendering. |
| 228-287 | `lccRecentPanel`, recent panel helpers, Alt+R listener | `content.js` | User-facing transcript panel; reads `lccTranscript` at runtime. |
| 289-303 | speaker marker state/helpers | `caption-overlay.js` | Speaker label display is part of caption rendering; scheduler calls `lccApplySpeaker` at runtime. |
| 305-577 | `lccFinalQ`, rev/unit maps, delay mode/clock, pacer, LocalAgreement, `lccShow*`, `lccPace*`, stream reset helpers | `caption-scheduler.js` | Caption queue, timing, dedupe, and rendering scheduler. Depends on overlay functions loaded first. |
| 578-690 | `lccHandleBridgeMessage` | `content.js` | Router/border adapter. It mixes caption, page-translation, OCR, and write-back dispatch, so verbatim split would violate B1; residual content owns it. |
| 694-704 | `chrome.runtime.onMessage.addListener(...)` | `content.js` | Load-time listener registration. Last-script position preserves router availability. |
| 705-711 | fullscreen re-host listener | `caption-overlay.js` | Overlay-only load-time listener; references only overlay-owned `box`/`ensureBox`. |
| 713-723 | `window.__lccOverlay = ...` | `content.js` | Load-time export reads overlay + scheduler + router names. Must stay after all split files load. |
| 726-764 | `lccPageContext`, `lccLastCtx`, `lccUpdateContext`, title timers/observer/pagehide cleanup | `content.js` | Page/video context and session lifecycle border adapter; pagehide touches scheduler, transcript, page translator, and overlay state. |
| 768-2155 | `lccPage*`, bilingual, verify, cache, scan, batch, start/config/stop/apply/retry/drop/done | `page-translator.js` | Page DOM translation state machine and caches. Loaded after scheduler. |
| 2156 | `lccResumePageTranslateIfActive();` | `content.js` | Load-time resume signal. Keeping it in the last script avoids sending `content-ready` before the message router/export setup exists. |
| 2157-2187 | transcript accumulation: `lccTranscript`, `lccFlushTranscript`, `resetTranscript`, `pushTranscript` | `caption-scheduler.js` | Caption data flow and recent panel source. Page lifecycle calls it at runtime after scheduler load. |
| 2191-2341 | write-back `lccWb*` and listeners | `content.js` | Message-driven content feature. It reads page-translate on/settings to gate UI but does not mutate page queue/cache/node state. |
| 2349-2510 | OCR `lccOcr*` and listeners | `content.js` | Message-driven content feature. It reads page-translate on/settings to gate UI but does not mutate page queue/cache/node state. |

## 3. Function Inventory

| Target | Functions |
|---|---|
| `caption-overlay.js` | `lccShouldRender`, `host`, `ensureBox`, `applySettings`, `setSrc`, `lccRenderKoText`, `setLines`, `setKoSplit`, `setLinesSplit`, `lccSetPeek`, `lccEditableTarget`, `lccSpeakerPrefix`, `lccApplySpeaker` |
| `caption-scheduler.js` | `lccReadMs`, `lccNow`, `lccSyncOffsetMs`, `lccPerfFromWall`, `lccStreamPerf`, `lccLagMs`, `lccReadyCount`, `lccReclockPending`, `lccSetPlaybackDelay`, `lccVideoSub`, `lccMarkStreamClock`, `lccDueAt`, `lccDecorateTiming`, `lccDebugLine`, `lccRememberFinalStream`, `lccSeenFinalStream`, `lccDropQueuedUnit`, `lccLcpWords`, `lccNormKoHyp`, `lccKoSplitInto`, `lccShowSplit`, `lccShow`, `lccShowItem`, `lccUnit`, `lccFresh`, `lccPaceReset`, `lccScheduleFinal`, `lccTakeFinal`, `lccPace`, `lccStartPacer`, `lccStopPacer`, `lccStreamResetIfRewound`, `lccFlushTranscript`, `resetTranscript`, `pushTranscript` |
| `page-translator.js` | `lccPageTranslatePolicy`, `lccPageHash`, `lccPageUrlKey`, `lccPageCacheNamespace`, `lccPageSourceNorm`, `lccPageSourceKey`, `lccPageLabelNamespace`, `lccPageConfigSignature`, `lccPageLooksLikeUiLabel`, `lccPageRequestOwns`, `lccPageLoadLabelCache`, `lccPageLabelRemember`, `lccPageScheduleLabelPersist`, `lccPageLoadCache`, `lccPageScheduleCachePersist`, `lccPageCachedEntry`, `lccPageCachedTarget`, `lccPageRememberCache`, `lccPageTextParts`, `lccPageHasLetters`, `lccPageNodeStyled`, `lccPageNearViewport`, `lccPageInViewport`, `lccPageAlreadyTarget`, `lccPageNodeAllowed`, `lccPageStateFor`, `lccPageNodeHoldsPendingSource`, `lccPageClearPartialState`, `lccPageRestorePartialNode`, `lccPagePruneWork`, `lccPageApplyToNode`, `lccPagePartialAllowed`, `lccPageApplyPartialToNode`, `lccPageBlockContext`, `lccPageBlockUnitFor`, `lccPageBindBlockMembers`, `lccPageApplyBlockTarget`, `lccPageQueueBlockUnit`, `lccPageApplyBlockResult`, `lccPagePh`, `lccPageAdvancePastSubtree`, `lccPageBlockUnitForR`, `lccPageBindBlockMembersR`, `lccPageQueueBlockUnitR`, `lccPageMapPhSegments`, `lccPageApplySegCollapse`, `lccPageBlockOptOutAndRequeue`, `lccPageApplyBlockResultR`, `lccPageQueueNode`, `lccPageScanNode`, `lccPageRoot`, `lccPageRequestIdle`, `lccPageCancelIdle`, `lccPageNow`, `lccPageStartScan`, `lccPageScanChunk`, `lccPageMaybePrefetch`, `lccPageVerifyEnabled`, `lccPageVerifyEnqueue`, `lccPageVerifyScheduleIdle`, `lccPageRouteFailureReason`, `lccPageWarnBatchRouteFailure`, `lccPageBatchRouteFailed`, `lccPageSendBatch`, `lccPageVerifyFlush`, `lccPageVerifyApply`, `lccPageVerifyDone`, `lccPageScheduleScan`, `lccPageScheduleFlush`, `lccPageFlush`, `lccPageClearTransient`, `lccPageNotifyReady`, `lccPageHandleUrlOrContextChange`, `lccPageStartUrlWatch`, `lccPageStopUrlWatch`, `lccPageBilingualEnabled`, `lccPageBilingualInlineEnabled`, `lccPageBilingualInlineMark`, `lccPageBilingualInlineClearAll`, `lccPageBilingualCapture`, `lccPageBilingualCaptureEl`, `lccPageBilingualEnsureGhost`, `lccPageBilingualShow`, `lccPageBilingualHideGhost`, `lccPageBilingualOnOver`, `lccPageBilingualOnOut`, `lccPageBilingualStart`, `lccPageBilingualStop`, `lccPageTranslateStart`, `lccPageTranslateConfig`, `lccPageTranslateStop`, `lccPageTranslatePartial`, `lccPageTranslateApply`, `lccPageTranslateDrop`, `lccPageRequeueKey`, `lccPageTranslateDone`, `lccPageTranslateRetry`, `lccResumePageTranslateIfActive` |
| `content.js` | `lccAddGlossary`, `lccEnsureGlossBar`, `lccCloseGlossBar`, `lccToggleGlossBar`, `lccCloseRecent`, `lccShowRecent`, `lccToggleRecent`, `lccHandleBridgeMessage`, `lccPageContext`, `lccUpdateContext`, `lccWbEnabled`, `lccWbPageLang`, `lccWbFieldEligible`, `lccWbFieldText`, `lccWbApply`, `lccWbEnsureBtn`, `lccWbHide`, `lccWbShowFor`, `lccWbTrigger`, `lccWbHandleResult`, `lccOcrEnabled`, `lccOcrEligible`, `lccOcrChipText`, `lccOcrChipHide`, `lccOcrEnsureChip`, `lccOcrShowChipFor`, `lccOcrHideOverlay`, `lccOcrShowOverlay`, `lccOcrTrigger`, `lccOcrHandleResult`, `lccOcrFindImgAt`, `lccOcrHoverCheck` |

## 4. Top-Level Globals

| Target | Globals / constants |
|---|---|
| `caption-overlay.js` | `box`, `lccPeek`, `lccLastSrc`, `LCC_IS_TOP`, `LCC_PEEK_DEBUG`, `lccSpeakersSeen`, `LCC_SPEAKER_MARKS` |
| `caption-scheduler.js` | `lccFinalQ`, `lccLatestRev`, `lccCommittedUnits`, `lccStreamedFinalUnits`, `lccLivePartial`, `lccHoldUntil`, `lccShown`, `lccShownUnit`, `lccShownKind`, `lccStreamStartPerf`, `lccMaxEndMs`, `lccDelayMode`, `lccPlaybackDelayMs`, `LCC_LAG_CAP_MS`, `LCC_CAPTION_LEAD_MS`, `LCC_CAPTION_MAX_MS`, `LCC_FINAL_STREAM_SEEN_TTL_MS`, `LCC_FINAL_QUEUE_CAP`, `lccLastKoT`, `lccKoState`, `lccPaceTimer`, `lccTranscript`, `lccSessionStart`, `lccStoreTimer` |
| `page-translator.js` | `LCC_PAGE_FRAME_TAG`, `LCC_PAGE_EXCLUDE_SELECTOR`, `LCC_PAGE_BATCH_POLICY`, `lccPageTranslateOn`, `lccPageTranslateSettings`, `lccPageTranslateObserver`, `lccPageTranslateScrollHandler`, `lccPageTranslateFlushTimer`, `lccPageTranslateScanTimer`, `lccPageTranslateReqSeq`, `lccPageTranslateEpoch`, `lccPageTranslateConfigSig`, `lccPageHotQueue`, `lccPageColdQueue`, `lccPageWork`, `lccPageTranslateNodes`, `lccPageTranslateState`, `lccPageTranslateRequests`, `lccPageTranslateStats`, `LCC_PAGE_PARTIAL_MAX_CHARS`, `LCC_PAGE_NODE_MAX_CHARS`, `lccBilingualOrig`, `lccBilingualGhost`, `lccBilingualOver`, `lccBilingualOut`, `lccBilingualHide`, `LCC_PAGE_BILINGUAL_MAX_CHARS`, `lccPageVerify*`, `LCC_PAGE_SCAN_*`, `lccPageScan*`, `LCC_PAGE_LABEL_*`, `lccPageLabel*`, `LCC_PAGE_CACHE_*`, `lccPageTranslateCache*`, `lccPageTranslateUrl*`, `lccPageTranslateLastContext`, `LCC_PAGE_TARGET_SCRIPT`, `LCC_PAGE_TARGET_SCRIPT_MIN`, `LCC_PAGE_BLOCK_*`, `LCC_PAGE_PH_*`, `LCC_PAGE_INLINE_*`, `lccPageAuxSeen` |
| `content.js` | `settings`, `lccGlossBar`, `lccRecentPanel`, `lccLastCtx`, `lccWb*`, `LCC_WB_*`, `lccOcr*` |

## 5. Top-Level Execution Inventory

| Lines | Execution | Target | Order proof |
|---:|---|---|---|
| 113-134 | `chrome.storage.local.get(...)` and `chrome.storage.onChanged.addListener(...)` | `content.js` | References shared `settings` and scheduler `lccReclockPending`; stays last. |
| 149-156 | Alt-peek key/blur/visibility listeners | `caption-overlay.js` | References overlay-owned names; callback-only runtime use of shared names. |
| 221-225 | Alt+G glossary listener | `content.js` | Content feature. |
| 282-286 | Alt+R recent listener | `content.js` | Content feature; reads transcript at runtime. |
| 694-704 | `chrome.runtime.onMessage.addListener(...)` | `content.js` | Router registration stays in final file. |
| 705-711 | fullscreenchange listener | `caption-overlay.js` | Overlay-owned names only. |
| 713-723 | `window.__lccOverlay = {...}` | `content.js` | Reads overlay+scheduler+router names; must be after split files. |
| 737-764 | context update, timers, title observer, pagehide cleanup | `content.js` | Cross-module lifecycle adapter. |
| 2156 | `lccResumePageTranslateIfActive();` | `content.js` | Avoids content-ready before router/export setup. |
| 2324-2341 | write-back focus/scroll/Alt+T listeners | `content.js` | Content feature. |
| 2495-2510 | OCR mouse/Alt/scroll listeners | `content.js` | Content feature. |

## 6. Border Functions / Cross-File Reads

| Name | Assignment | Reason |
|---|---|---|
| `settings` | `content.js` | Shared config state read by overlay, scheduler, and page translator. Storage listener is load-time and touches scheduler; keeping it last preserves INV-18. |
| `lccShouldRender` | `caption-overlay.js` | Reads scheduler-owned `lccDelayMode` at runtime only. |
| `lccEditableTarget` | `caption-overlay.js` | Used by overlay peek plus residual glossary/write-back shortcuts; pure DOM predicate. |
| `lccHandleBridgeMessage` | `content.js` | Mixed router over caption, page, write-back, OCR; cannot be verbatim split into scheduler branches. |
| `window.__lccOverlay` export | `content.js` | Load-time aggregate of overlay, scheduler, and router names. |
| `lccPageContext` / `lccUpdateContext` | `content.js` | Page-context route and lifecycle adapter, not page DOM translation state machine. |
| `lccResumePageTranslateIfActive` | `page-translator.js`; call in `content.js` | Function belongs to page translation, but load-time call stays last. |
| `lccWb*` | `content.js` | Reads `lccPageTranslateOn`/`lccPageTranslateSettings` only as an enable/target gate; does not mutate page queue/cache/node state. |
| `lccOcr*` | `content.js` | Reads `lccPageTranslateOn`/`lccPageTranslateSettings` only as an enable gate; does not mutate page queue/cache/node state. |

## 7. Load-Smoke Checklist

`extension/test_content_load.js` should read manifest order dynamically, evaluate the scripts in one VM context,
and assert these globals are functions/objects after load:

- `setLines`
- `setLinesSplit`
- `lccHandleBridgeMessage`
- `lccPageTranslateStart`
- `lccPageTranslateStop`
- `lccPageTranslateApply`
- `lccPageTranslatePartial`
- `lccWbHandleResult`
- `lccOcrHandleResult`
- `window.__lccOverlay`

This checklist covers overlay, scheduler/router, page translator, write-back, OCR, and the delay.js-facing
overlay export without claiming rendered-page behavior.

## 8. T5 Completion Checks

- SSOT order equals the final target order in all three surfaces.
- `content.js` retains router/config/context/glossary/recent/write-back/OCR only.
- `caption-overlay.js` has no `chrome.runtime.sendMessage`.
- `page-translator.js` has no `__lccVideoSub`.
- No `bridge/*`, `offscreen.js`, `popup.*`, `protocol.js`, `delay.js`, `pcm.js`, `page-seed.js`, or
  `content.css` changes in the S4 diff.
