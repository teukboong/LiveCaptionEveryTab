# inventory — S3 server.py module split

Authority: `.dryforge/004/spec.md` > `.dryforge/004/plan.md` > `.dryforge/004/handoff.md`.
Generated after the 2026-06-10 F3/A7/INV-16 correction. Code changes in T0: none.
Updated for the 2026-06-10 T2 approval #2: A10 border adapters stay in `server.py`.

## 1. External `server` consumers

Alias-aware scan included `import server`, `import server as s`, `import server as srv`, and lazy aliases such as
`import server as _srv`. The table below records the public surface that must keep working through the facade unless a
write patch-target is explicitly moved by a later extraction task.

| File | Alias | Access kind | Names |
|---|---:|---|---|
| `bridge/backend_cuda.py` | `_srv` | read | `_clean`, `_translate_messages`, `_translate_page_batch_messages`, `_emit_page_markers`, `_page_batch_max_tokens`, `_parse_page_batch_result`, `_ask_messages` |
| `bridge/bench_2pass.py` | `server` | read | `load_models`, `transcribe_pcm`, `_append_text_dedupe` |
| `bridge/bench_kv_boundary.py` | `server` | read/write | writes `_TX_GEN_MAX`, `_TX_WINDOW_MARGIN`, `_TX_KVREUSE`, `_TX_KV_WINDOW`; reads `load_models`, `_reset_tx_cache`, `translate_once`, `_TX_KV_WINDOW`, `_TX_GEN_MAX`, `_TX_KV_MAX`, `_TX_WINDOW_MARGIN`, `_tx_system`, `_fewshot`, `_src_lang`, `lm_tok`, `_tx_cache`, `_tx_cache_ids`, `_tx_cache_offset` |
| `bridge/bench_kv_reuse.py` | `server` | read/write | writes `_TX_KVREUSE`; reads `load_models`, `_reset_tx_cache`, `translate_once`, `_tx_cache`, `_tx_cache_offset`, `_tx_cache_ids` |
| `bridge/bench_kv_window.py` | `server` | read/write | writes `_TX_KVREUSE`; reads `load_models`, `lm_model`, `_TX_KV_MAX`, `_tx_system`, `_fewshot`, `_src_lang`, `lm_tok`, `_reset_tx_cache`, `translate_once`, `_TX_GEN_MAX`, `_TX_WINDOW_MARGIN`, `_tx_cache`, `_tx_cache_ids`, `_tx_cache_offset` |
| `bridge/bench_translate_quality.py` | `server` | read | `load_models`, `translate_once`, `_REGISTERS` |
| `bridge/install_models.py` | `s` | read | `asr_models`, `lm_models` |
| `bridge/test_assembler_decisions.py` | `s` | read | `_commit_decision`, `SR`, `TWO_PASS_MIN_SEC`, `TWO_PASS_MAX_SEC`, `_two_pass_eligible`, `Unit`, `_dedupe_commit_overlap` |
| `bridge/test_aux_lm.py` | `s` | read | `_lm_select_value`, `lm_models`, `AUX_LM_HEADROOM_GB`, `_aux_lm_choice` |
| `bridge/test_backend_cuda.py` | `srv` | read | `_translate_messages`, `_translate_page_batch_messages`, `_ask_messages` |
| `bridge/test_evs_controller.py` | `s` | read/write | writes `EVS_ON`, `EVS_ENTER_MS`, `EVS_EXIT_MS`, `EVS_CAP_DROP`, `EVS_AGE_DROP`; reads `_evs_step`, `AGG_PENDING_CAP`, `_lat_pending_cap`, `BAL_PENDING_MAX_AGE_MS`, `_lat_pending_max_age_ms` |
| `bridge/test_glossary_repair.py` | `s` | read/write | writes `GLOSSARY_REPAIR_ON`; reads `_gr_norm`, `_repair_glossary_terms` |
| `bridge/test_latency_profile.py` | `s` | read | pending/staleness latency constants, `_lat_*`, `_is_*_engine`, `_normalize_asr_engine`, `_TX_GEN_MAX`, `TX_PREVIEW_MAX_TOKENS`, `TX_FINAL_STREAM_EVERY` |
| `bridge/test_model_select.py` | `s` | read/write | writes `BACKEND`, `_free_mem_gb_mlx`, `_LM_RESOLVED`, `LM_MODEL`; reads `LM_MODEL`, `_LM_RESOLVED`, `lm_models`, `asr_models`, `_ASR_ENGINES`, `_is_*_engine`, `_normalize_asr_engine`, `_auto_lm_model`, `MLXA_REPOS`, `_finalize_model_config`, `_tx_system`, `_page_tx_system`, `_translation_context_signature` |
| `bridge/test_number_guard.py` | `s` | read/write | writes `NUMGUARD_ON`; reads `_sig_numbers`, `_missing_numbers`, `_ko_number_forms`, `_guard_numbers` |
| `bridge/test_policy.py` | `s` | read | `_source_risk`, `decide_commit`, `_commit_decision` |
| `bridge/test_scheduler_staleness.py` | `s` | read | `_preview_is_stale` |
| `bridge/test_term_memory.py` | `s` | read | `_mine_terms`, `_update_term_memory`, `_merge_auto_glossary`, `TERM_MEMORY_STATS_MAX` |
| `bridge/test_text_helpers.py` | `s` | read/write | writes `translate_once` plus KV/runtime cluster listed in section 2; reads text helpers, prompt/page marker helpers, `translate_page_long_once`, `_dom_translate_items`, `translate_page_batch_once`, stream partial helpers, repeat helpers, and KV/runtime state |

No external writes were found outside the bench 3 files and test 5 files called out by corrected F3.

## 2. Write patch-target table

A7 rule: patch the module where the tested consumer will look the name up after extraction. If the consumer stays in
`server.py`, the patch-target stays `server`.

| File | Patch name | Tested consumer | Post-extraction lookup module | New patch-target |
|---|---|---|---|---|
| `bridge/bench_kv_boundary.py` | `_TX_GEN_MAX` | `translator.translate_once` token cap path | `translator` | `translator._TX_GEN_MAX` |
| `bridge/bench_kv_boundary.py` | `_TX_WINDOW_MARGIN` | `translator.translate_once` KV window learning | `translator` | `translator._TX_WINDOW_MARGIN` |
| `bridge/bench_kv_boundary.py` | `_TX_KVREUSE` | `translator.translate_once` KV reuse switch | `translator` | `translator._TX_KVREUSE` |
| `bridge/bench_kv_boundary.py` | `_TX_KV_WINDOW` | `translator.translate_once` KV trim/window state | `translator` | `translator._TX_KV_WINDOW` |
| `bridge/bench_kv_reuse.py` | `_TX_KVREUSE` | `translator.translate_once` KV reuse switch | `translator` | `translator._TX_KVREUSE` |
| `bridge/bench_kv_window.py` | `_TX_KVREUSE` | `translator.translate_once` KV reuse switch | `translator` | `translator._TX_KVREUSE` |
| `bridge/test_evs_controller.py` | `EVS_ON` | `policy._evs_step` | `policy` | `policy.EVS_ON` |
| `bridge/test_evs_controller.py` | `EVS_ENTER_MS` | `policy._evs_step` | `policy` | `policy.EVS_ENTER_MS` |
| `bridge/test_evs_controller.py` | `EVS_EXIT_MS` | `policy._evs_step` | `policy` | `policy.EVS_EXIT_MS` |
| `bridge/test_evs_controller.py` | `EVS_CAP_DROP` | `policy._lat_pending_cap` | `policy` | `policy.EVS_CAP_DROP` |
| `bridge/test_evs_controller.py` | `EVS_AGE_DROP` | `policy._lat_pending_max_age_ms` | `policy` | `policy.EVS_AGE_DROP` |
| `bridge/test_number_guard.py` | `NUMGUARD_ON` | `policy._guard_numbers` | `policy` | `policy.NUMGUARD_ON` |
| `bridge/test_glossary_repair.py` | `GLOSSARY_REPAIR_ON` | `asr._repair_glossary_terms` | `asr` | `asr.GLOSSARY_REPAIR_ON` |
| `bridge/test_model_select.py` | `BACKEND` | `model_runtime._free_mem_gb_mlx` / config finalization | `model_runtime` | `model_runtime.BACKEND` |
| `bridge/test_model_select.py` | `_free_mem_gb_mlx` | `model_runtime._auto_lm_model` / `_finalize_model_config` | `model_runtime` | `model_runtime._free_mem_gb_mlx` |
| `bridge/test_model_select.py` | `_LM_RESOLVED` | `model_runtime._finalize_model_config` | `model_runtime` | `model_runtime._LM_RESOLVED` |
| `bridge/test_model_select.py` | `LM_MODEL` | `model_runtime._finalize_model_config` | `model_runtime` | `model_runtime.LM_MODEL` |
| `bridge/test_text_helpers.py` | `translate_once` | `server.translate_page_long_once` seam lookup | `server` | unchanged: `server.translate_once` |
| `bridge/test_text_helpers.py` | `lm_tok` | `translator.translate_page_batch_once` through runtime state | `model_runtime` | `model_runtime.lm_tok` |
| `bridge/test_text_helpers.py` | `lm_model` | `translator.translate_page_batch_once` through runtime state | `model_runtime` | `model_runtime.lm_model` |
| `bridge/test_text_helpers.py` | `mx` | `translator.translate_page_batch_once` through runtime dependency | `model_runtime` | `model_runtime.mx` |
| `bridge/test_text_helpers.py` | `lm_stream` | `translator.translate_page_batch_once` through runtime dependency | `model_runtime` | `model_runtime.lm_stream` |
| `bridge/test_text_helpers.py` | `make_prompt_cache` | `translator.translate_page_batch_once` through runtime dependency | `model_runtime` | `model_runtime.make_prompt_cache` |
| `bridge/test_text_helpers.py` | `trim_prompt_cache` | `translator.translate_page_batch_once` through runtime dependency | `model_runtime` | `model_runtime.trim_prompt_cache` |
| `bridge/test_text_helpers.py` | `can_trim_prompt_cache` | `translator.translate_page_batch_once` through runtime dependency | `model_runtime` | `model_runtime.can_trim_prompt_cache` |
| `bridge/test_text_helpers.py` | `_LM_IS_VLM` | `translator.translate_page_batch_once` through runtime state | `model_runtime` | `model_runtime._LM_IS_VLM` |
| `bridge/test_text_helpers.py` | `_TX_KV_WINDOW` | `translator.translate_page_batch_once` KV trim/window state | `translator` | `translator._TX_KV_WINDOW` |
| `bridge/test_text_helpers.py` | `_PAGE_TX_KVREUSE` | `translator.translate_page_batch_once` page KV reuse switch | `translator` | `translator._PAGE_TX_KVREUSE` |
| `bridge/test_text_helpers.py` | `_tx_cache` | `translator.translate_once` caption KV cache | `translator` | `translator._tx_cache` |
| `bridge/test_text_helpers.py` | `_tx_cache_ids` | `translator.translate_once` caption KV cache ids | `translator` | `translator._tx_cache_ids` |
| `bridge/test_text_helpers.py` | `_page_tx_cache` | `translator.translate_page_batch_once` page KV cache | `translator` | `translator._page_tx_cache` |
| `bridge/test_text_helpers.py` | `_page_tx_cache_ids` | `translator.translate_page_batch_once` page KV cache ids | `translator` | `translator._page_tx_cache_ids` |

## 3. Function, constant, and global assignment

Line numbers are from `bridge/server.py` before T1.

| Lines | Names | Target module | Reason |
|---:|---|---|---|
| 32-135 | `_SHERPA_ENGINES`, `_MLXA_ENGINES`, `_WHISPER_ENGINES`, `_ASR_ENGINES`, `_is_*_engine`, `_normalize_*`, `_clamp_int`, `_clamp_float`, `_config_bool` | `model_runtime` | env/config normalization and model engine family |
| 138-150 | `DOM_TX_MAX_*`, `PAGE_LONG_CHARS`, `PAGE_CHUNK_CHARS`, `PAGE_TX_BATCH_*`, `PAGE_BLOCK_*` | `page_markers` / `text_helpers` | page translation chunk and batch constants; split/chunk constants follow `_chunk_text`, marker limits follow marker module |
| 150-174 | `_dom_translate_items` | `server` | input adapter used by handler/tests; not part of marker serialization |
| 177-253 | MLX import bindings, `BACKEND`, model registries, headroom/default constants | `model_runtime` | runtime dependency surface and lazy model selection |
| 256-425 | `lm_models`, `asr_models`, memory probes, `_auto_lm_model`, aux helpers, `_resolve_lm_model`, `_finalize_model_config` | `model_runtime` | model registry and selection logic |
| 428-490 | ASR/model runtime globals, host/ws constants, audio/window constants | split | runtime model globals to `model_runtime`; host/ws and `_active_ws` remain `server`; audio constants stay facade-visible via owning modules |
| 493-614 | `_diarize`, `_require_mlx`, `_ensure_asr_loaded`, `_load_lm_weights`, `load_models` | `model_runtime` / `asr` | load paths and ASR load seam; mutable runtime state lives in `model_runtime` |
| 619-680 | pools/locks, translator knobs, latency constants, stream constants | `model_runtime` / `translator` / `policy` / `server` | pools to runtime; KV knobs to translator; policy constants to policy except adapter-only `TX_PREVIEW_MAX_TOKENS` stays server |
| 686-804 | `LatencyProfile`, policy-core `_lat_*`, EVS constants, `_evs_step`; A10 adapters `_lat_tx_max_tokens_for`, `_lat_soft_max_sec`, `_lat_sent_windows_for` | `policy` / `server` | policy core and EVS switches to policy; cross-owner border adapters stay server |
| 807-827 | `warm_mlx_selected` | seam watch: `server` or `model_runtime` | spec table says model_runtime, but function currently calls seam names `transcribe_pcm`, `translate_once`, `_ensure_asr_loaded`; T6 must satisfy INV-17 without moving `translate_page_long_once` |
| 829-850 | `_request_header`, `_origin_allowed` | `server` | websocket/request boundary |
| 853-977 | `_has_hangul`, `_lcp_words`, `_coalesce_batch`, sentence/dedupe/repeat helpers, related constants | `text_helpers` | pure text utilities shared by policy/translator/server |
| 980-1053 | `_commit_decision`, `_two_pass_eligible`, `NUMGUARD_ON`, numeric guard helpers | `policy` | commit policy and number guard |
| 1062-1141 | `GLOSSARY_REPAIR_ON`, `_GR_*`, `_gr_norm`, `_repair_glossary_terms` | `asr` | ASR glossary repair path; `_gr_norm` is moved with its only repair consumer and facade-reexported |
| 1152-1247 | `TERM_MEMORY_*`, `_TERM_*`, `_mine_terms`, `_update_term_memory`, `_merge_auto_glossary` | `term_memory` | term memory helpers; handle keeps session dictionaries |
| 1256-1322 | `_source_risk`, `InterpretDecision`, `decide_commit`, preview/stream partial helpers | `policy` | interpretation/staleness/partial emission policy |
| 1328-1368 | `transcribe_pcm` | `asr` seam implementation | MLX ASR implementation; server seam can be rebound by backend block |
| 1371-1373 | `_CLEAN_RE`, `_clean` | `text_helpers` | pure normalization helper used by backend_cuda and tests |
| 1380-1672 | `_TX_FEWSHOT`, `_PAGE_TX_FEWSHOT`, `_REGISTER_TONE`, `_REGISTERS`, `_src_lang`, `_fewshot`, glossary/prompt builders, `_translate_messages` | `prompts` plus `_src_lang` in `text_helpers` | prompt/message construction; `_src_lang` is pure text detection and facade-visible |
| 1684-1892 | `_PAGE_MARKER_RE`, page marker helpers, `_translate_page_batch_messages`, `_emit_page_markers`, `_parse_page_batch_result` | `page_markers` / `prompts` | marker serialization/parsing to page_markers; message builder to prompts |
| 1895-1908 | `_ask_messages` | `prompts` | ask prompt builder |
| 1911-2212 | KV cache helpers, `_vlm_generate_text`, `translate_once`, `translate_page_batch_once` | `translator` | translator state machine and seam implementations |
| 2219-2242 | `_SENT_SPLIT_RE`, `_split_sentences`, `_chunk_text` | `text_helpers` | pure long-page chunk helpers |
| 2245-2274 | `translate_page_long_once` | `server` | F1/A2: must keep server seam lookup of `translate_once` |
| 2277-2293 | `run_ask` | `translator` | ask translation seam implementation |
| 2326-3806 | `Unit`, `handle`, `_port_in_use`, `main`, `_is_loopback_host` | `server` | websocket orchestration, native-host entrypoint, and seam runtime |

### T2 A10 border-adapter assignment

| Name | Target module | Reason |
|---|---|---|
| `_lat_tx_max_tokens_for` | `server` | reads translator-owned `_TX_GEN_MAX`; all non-self callers are server-resident handle/startup paths |
| `_lat_soft_max_sec` | `server` | calls model-runtime engine taxonomy `_is_sherpa_engine`; all non-self callers are server-resident |
| `_lat_sent_windows_for` | `server` | reads server facade audio constants `SEG_SILENCE_MS` / `WINDOW_MS`; all non-self callers are server-resident |
| `TX_PREVIEW_MAX_TOKENS` | `server` | only the server-resident `_lat_tx_max_tokens_for` adapter reads it |
| `LatencyProfile`, `_lat_profile`, `_lat_tx_stream_every_for`, `_lat_preview_debounce_ms`, `_lat_pending_cap`, `_lat_pending_max_age_ms`, `_lat_effective_sent_silence_ms`, `_evs_step`, `EVS_*`, `_commit_decision`, `_two_pass_eligible`, `NUMGUARD_ON`, `_sig_numbers`, `_ko_number_forms`, `_missing_numbers`, `_guard_numbers`, `_source_risk`, `InterpretDecision`, `decide_commit`, `_preview_is_stale`, `_stream_visible_chars`, `_stream_partial_substantial`, `_stream_partial_should_emit` | `policy` | policy core with no cross-owner mutable/runtime reads after A10 adapters stay server |
| `SR`, `WINDOW_*`, `SEG_SILENCE_MS`, `SENT_SILENCE_MS`, `SPEECH_PAD_MS`, `PREROLL_WINDOWS`, `SOFT_*`, `HARD_MAX_SEC`, `MIN_SEC`, `LA_*`, `TWO_PASS_*`, `PENDING_*`, `PREVIEW_*`, `AGG_*`, `BAL_*`, `SPEC_*`, `TX_FINAL_STREAM_EVERY`, `TX_FINAL_STREAM_MIN_*`, `TX_FINAL_STREAM_DELTA_CHARS` | `policy` | read by moved policy core or policy-derived live scheduler constants; server keeps the old surface by importing/reexporting these names |

### F4 mutable global ownership

| Owner | Names |
|---|---|
| `translator` | `_tx_cache`, `_tx_cache_ids`, `_page_tx_cache`, `_page_tx_cache_ids`, `_TX_KV_WINDOW`; plus writable knobs `_TX_KVREUSE`, `_PAGE_TX_KVREUSE`, `_TX_GEN_MAX`, `_TX_WINDOW_MARGIN` |
| `model_runtime` | `lm_model`, `lm_tok`, `LM_MODEL`, `_LM_RESOLVED`, `_LM_IS_VLM`, `_sampler`, `silero`, `aux_lm_model`, `aux_lm_tok`, `_AUX_LM_IS_VLM`, `ASR_ENGINE`, `mlxa_model`, `mlxa_loaded_engine`, `parakeet_asr`, `whisper_loaded_repo`; runtime deps `mx`, `lm_stream`, `make_prompt_cache`, `trim_prompt_cache`, `can_trim_prompt_cache` |
| `server` | `_active_ws` |

## 4. Facade delegation draft

`server.__getattr__` must delegate live reads for moved mutable state and keep static reexports for functions/constants.

| Facade names read externally | Owner after extraction |
|---|---|
| `_append_text_dedupe`, `_dedupe_commit_overlap`, `_next_sentence_cut`, `_weak_tail`, `_short_suffix_duplicate`, `_src_lang`, `_split_sentences`, `_chunk_text`, `_clean`, `_gr_norm`, `_repeat_cache_eligible`, `_repeat_key`, `MIN_SENT_CHARS`, `_TARGET_LANGS` | `text_helpers` |
| `_commit_decision`, `_two_pass_eligible`, policy-core `_lat_*`, `_evs_step`, `EVS_*`, `NUMGUARD_ON`, `_sig_numbers`, `_missing_numbers`, `_ko_number_forms`, `_guard_numbers`, `_source_risk`, `decide_commit`, `InterpretDecision`, `_preview_is_stale`, `_stream_*`, policy latency/pending constants | `policy` |
| `_lat_tx_max_tokens_for`, `_lat_soft_max_sec`, `_lat_sent_windows_for`, `TX_PREVIEW_MAX_TOKENS` | `server` |
| `_translation_context_signature`, `_tx_system`, `_page_tx_system`, `_write_tx_system`, `_translate_messages`, `_translate_page_batch_messages`, `_ask_messages`, `_fewshot`, `_parse_glossary`, `_REGISTERS` | `prompts` |
| `_page_batch_max_tokens`, `_emit_page_markers`, `_parse_page_batch_result`, `_page_marker_*`, page partial constants | `page_markers` |
| `_mine_terms`, `_update_term_memory`, `_merge_auto_glossary`, `TERM_MEMORY_*` | `term_memory` |
| `BACKEND`, `LM_MODEL`, `_LM_RESOLVED`, `lm_model`, `lm_tok`, `_LM_IS_VLM`, `mx`, `lm_stream`, prompt-cache functions, `lm_models`, `asr_models`, `_ASR_ENGINES`, `_is_*_engine`, `_normalize_*`, `_free_mem_gb_mlx`, `_auto_lm_model`, `_finalize_model_config`, `_aux_lm_choice`, `_lm_select_value`, `AUX_LM_HEADROOM_GB`, `MLXA_REPOS`, `load_models`, `_ensure_asr_loaded` | `model_runtime` |
| `GLOSSARY_REPAIR_ON`, `_repair_glossary_terms`, `transcribe_pcm` | `asr` |
| `_TX_KVREUSE`, `_PAGE_TX_KVREUSE`, `_TX_KV_WINDOW`, `_TX_GEN_MAX`, `_TX_WINDOW_MARGIN`, `_TX_KV_MAX`, `_tx_cache`, `_tx_cache_ids`, `_page_tx_cache`, `_page_tx_cache_ids`, `_reset_tx_cache`, `_reset_page_tx_cache`, `_tx_cache_offset`, `translate_once`, `translate_page_batch_once`, `run_ask` | `translator` |

## 5. Seam call inventory

Seam names: `transcribe_pcm`, `translate_once`, `translate_page_batch_once`, `run_ask`, `warm_mlx_selected`,
`_ensure_asr_loaded`.

| Location | Bare seam call | Disposition |
|---|---|---|
| `server.py:589`, `server.py:594` in `load_models` | `_ensure_asr_loaded(ASR_ENGINE)` | T6/T7 boundary: if `load_models` moves to `model_runtime`, state updates must be module-attribute based and INV-17 checked |
| `server.py:814-825` in `warm_mlx_selected` | `_ensure_asr_loaded`, `transcribe_pcm`, `translate_once` | watch item: moving this verbatim to `model_runtime` would create extracted-module seam calls; T6 must either keep orchestration in `server` or produce a spec-compliant split |
| `server.py:2098` in `translate_once` | recursive `translate_once(...)` | same owner `translator`; allowed if self-recursion remains inside translator |
| `server.py:2203` in `translate_page_batch_once` | recursive `translate_page_batch_once(...)` | same owner `translator`; allowed if self-recursion remains inside translator |
| `server.py:2256` in `translate_page_long_once` | `translate_once(...)` | must remain in `server.py`; `server.translate_once` patch-target unchanged |
| `server.py:3435` in `handle` | `translate_page_batch_once(...)` | server orchestration seam call; remains valid through server seam binding |
| `server.py:3835` in `main` | `warm_mlx_selected(True, True)` | server startup seam call; remains valid through server seam binding |

No extracted module may import `server` or call another seam implementation directly.

## 6. `handle()` import draft

`handle()` and its closures currently reference extracted helpers directly. As modules land, `server.py` should import
these names explicitly or use module-qualified owner names where mutable state is involved:

- From `text_helpers`: `_append_text_dedupe`, `_coalesce_batch`, `_dedupe_commit_overlap`,
  `_next_sentence_cut`, `_repeat_cache_eligible`, `_repeat_key`, `_short_suffix_duplicate`, `_weak_tail`.
- From `policy`: `LatencyProfile`, `_commit_decision`, `_guard_numbers`, `_lat_effective_sent_silence_ms`,
  `_lat_pending_cap`, `_lat_pending_max_age_ms`, `_lat_preview_debounce_ms`, `_lat_profile`,
  `_lat_tx_stream_every_for`,
  `_preview_is_stale`, `_source_risk`, `_stream_partial_should_emit`, `_two_pass_eligible`, `decide_commit`.
- Server-resident A10 adapters: `_lat_tx_max_tokens_for`, `_lat_soft_max_sec`, `_lat_sent_windows_for`.
- From `prompts`: `_translate_messages`, `_translate_page_batch_messages`, `_ask_messages`, `_parse_glossary`.
- From `page_markers`: `_emit_page_markers`, `_page_batch_max_tokens`, `_parse_page_batch_result`,
  `_page_partial_should_emit`.
- From `term_memory`: `_merge_auto_glossary`, `_mine_terms`, `_update_term_memory`.
- From `model_runtime`: `aux_lm_ready`, `_aux_runtime`, `load_models`, and runtime constants/functions as needed.
- From `asr`: `_repair_glossary_terms`, `transcribe_pcm` seam implementation.
- From `translator`: `_reset_tx_cache`, `_reset_page_tx_cache`, `translate_once`, `translate_page_batch_once`,
  `run_ask` seam implementations.

`translate_page_long_once`, `Unit`, websocket/origin helpers, backend seam rebinding, `_active_ws`, and `main` stay in
`server.py`.
