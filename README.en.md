# Live Caption Every Tab — Realtime foreign→Korean captions for any site

[한국어](README.md) · **English** · [日本語](README.ja.md) · [Español](README.es.md) · [中文](README.zh.md)

> 🤖 This project was built **entirely through vibe coding (AI pair-programming)** — from the code to the documentation.

On YouTube, Twitch, **X**, or any site, it captures the browser tab's audio and uses a **local Gemma-4** to transcribe + translate, showing 2-line captions (source / Korean) over the video. (Tab capture is domain-agnostic, so any tab with sound works.)
For transcription you pick in the popup between **Granite Speech 4.1** (strong English) and **Qwen3-ASR** (multilingual incl. Japanese/Korean). Both emit punctuation and casing natively, and gate silence with `[no speech]`.

## Why this exists (similar tools already exist)

Realtime captioning/translation tools mostly split into two camps, and the combination **"in the browser / any live tab / fully local LLM by-meaning translation"** was missing — this fills that gap.

| | This project | Whisper-based browser extensions | Desktop players (e.g. LLPlayer) |
|---|---|---|---|
| **Input** | **Any tab** with sound (incl. live streams) | Tab audio | Downloaded video / files·URLs fed into a player |
| **ASR** | Granite / Qwen3 (native punctuation·truecasing; silence·music gated with `[no speech]`) | Mostly Whisper | Mostly Whisper |
| **Translation** | **Local LLM (Gemma-4)** by-meaning — keeps context·pronouns | None / literal MT / cloud | Local LLM possible (Ollama, etc.) |
| **Execution** | 100% local (zero cloud) | Local~mixed | Local |
| **Target language** | Korean-first (+multilingual) | Varies | Multilingual (per-language tuning varies) |

- **Whisper-based extensions** capture the tab well, but Whisper tends to hallucinate captions over silence/music, and translation is often absent, literal, or cloud-based. → Here it's solved differently: punctuation-native ASR + silence gating + local Gemma by-meaning translation.
- **Desktop players** have great local-LLM translation, but you must download the video or feed it into the player, which doesn't fit live streams / arbitrary sites. → Here, no downloading — it overlays **right on any tab that makes sound**.

Everything is **local and free**. The trade-off is a hardware floor (see requirements in [SETUP.md](SETUP.md)). On lighter machines the translation model auto-tiers to fit memory (full/mid/lite).

**Platform (backend):** the same bridge·same extension run on two runtimes, selected with `LCC_BACKEND`.
- **`mlx`** (default, Apple Silicon): in-process MLX — Granite/Qwen3 ASR, 26B-A4B translation. → [SETUP.md](SETUP.md)
- **`cuda`** (Windows+NVIDIA, WSL2): OpenAI-compatible **HTTP** — llama.cpp translation (26B GGUF), **the same granite/qwen3 ASR as on Mac** (transformers, `cuda/asr_server.py`). The popup's **transcription-engine** toggle (English=granite / multilingual=qwen3) still applies (routed via the `model` field), and each engine can point at a different server. No whisper. → [SETUP-windows.md](SETUP-windows.md)

VAD·sentence assembly·scheduler·number-guard·prompt builder are **shared across platforms** (pure functions). Only the 3 GPU functions (transcribe/translate/summarize) change per runtime, and that boundary is `bridge/backend_cuda.py` (HTTP) and the "Backend seam" in server.py.

## Architecture
```
[Chrome extension] tabCapture (tab audio) ──WS(PCM16 16k)──▶ [bridge/server.py]
                                                        VAD + soft-cut ASR atom
                                                        → Granite / Qwen3-ASR transcription (punctuation·multilingual)
                                                        → unit assembler
                                                        → 26B-A4B MoE Korean translation
   [content.js 2-line overlay] ◀──WS(JSON caption)──────┘
```
- ASR picks between **two mlx-audio engines** in the popup (▸ Transcription engine). **Granite Speech 4.1 2B** (`ibm-granite/granite-speech-4.1-2b` · faithful English, ~0% WER) and **Qwen3-ASR 1.7B** (`Qwen/Qwen3-ASR-1.7B` · 52 languages incl. Japanese/Korean, auto language ID). Both emit punctuation·truecasing natively so sentence chunking just works. Shares the Apple GPU with the 26B (serialized). ⚠ granite needs the **conv fix on mlx-audio main** (see SETUP).
- A low-latency English-only Parakeet is a power-user escape hatch via `LCC_ASR_ENGINE=parakeet` only (CPU, parallel to translation; model `~/.local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8`, `sherpa-onnx==1.13.2`). The popup selector only exposes granite/qwen3.
- Translation: `mlx-community/gemma-4-26b-a4b-it-4bit` (mlx-lm) — default **quality prompt** (expert interpreter·by-meaning·no-translationese + 3 few-shots, cost amortized by KV-cache → natural spoken Korean rather than stiff written style). Low latency via `LCC_TX_PROFILE=fast`. **Target language is selectable** (KO/EN/JA/ZH/ES/FR/DE), source auto-detected, skipped when target=source.
- RAM ~26GB (weights) + a little KV per chunk. Latency ~2.9–3.4s per utterance chunk (ASR ~0.7s + translation ~1.4s + audio prefill + clause-boundary wait).
- MTP is pointless on this hardware, so unused (verified across MoE·dense·E4B).
- ⚠️ Needs genuine Chrome/Edge/Brave — some Chromium forks (e.g. ChatGPT Atlas) don't implement `chrome.tabCapture`.

## Run
### 1) Bridge server
```bash
# from the repo root (first time, run ./setup.sh to install venv·deps)
bash bridge/run_bridge.sh
# ready when "[bridge] ready  ws://127.0.0.1:8765" appears (first load ~40s)
```
- To keep it always on (opt-in, auto-restart on crash): `bash bridge/autostart.sh install` — ⚠ ~26GB RAM resident. Off: `… uninstall`
- To start/stop from the **popup button** without a terminal (`🚀 Start bridge`): run `bash extension/native-host/install-host.sh` once, then reload the extension (native messaging host — SETUP 6.5). It runs detached, so it survives closing the browser.
- If the bridge restarts/drops, the extension **auto-reconnects** (backoff) and buffers up to 6s of recent audio. Speech during longer outages may be lost.
### 2) Load the extension (Chrome)
1. `chrome://extensions` → turn on **Developer mode** (top right)
2. **Load unpacked** → select this repo's `extension/` folder
3. On a YouTube/Twitch video tab, **click the extension icon** → click **`▶ Start captions`** in the popup → badge `ON`, overlay appears
4. Popup settings: caption **size·vertical/horizontal position·source line·sync offset** (live), **sentence wait·voice detection** (applied on restart)
5. Stop again with **`■ Stop captions`**. (tabCapture requires a user click gesture → no auto-start)

## Features
- **Auto term priming**: auto-injects the page/video title as ASR·translation hints (toggle off in the popup).
- **Content-type presets**: pick a content type once (general·chat / conference·lecture / news·interview / personal streaming) and it bundles register (tone) + latency mode — lecture=formal·stable, news=balanced, streaming=colloquial·instant. Tone·sentence-endings·few-shot anchors adapt to the content, and the source language (EN/JA) is auto-detected to pick matching examples.
- **Glossary**: enter `name=translation` (one per line) in the popup to bias transcription + always render that term identically in translation (removes the wobble of a name translated differently each line). `Term hints` is free-text biasing.
- **Accuracy mode (2-pass re-transcription)**: when on, multi-clause sentences finalized by a natural end (pause/eos) or terminal punctuation get their accumulated audio re-transcribed once as a whole right before commit → removes boundary errors from stitching VAD fragments. Finalization is ~0.7s slower, so it's a toggle (default OFF). Units whose alignment broke from overlap/split are auto-excluded (`unit_pure` guard).
- **Streaming captions**: the source line appears first per ASR atom; the Korean preview is debounced/coalesced. Committed captions are prioritized in the final queue.
- **3 latency modes**: `aggressive` overlaps Parakeet CPU transcription with MLX translation as much as possible and pre-translates the current unit preview latest-only; `balanced` previews only when MLX is idle; `stable` shows only committed translations. Final translation always takes priority over preview.
- **Lookahead video delay**: in video-delay mode the actual audio is transcribed·translated immediately, and captions are scheduled to the real PCM stream-start clock and the utterance window (`start_ms`/`end_ms`). The popup's sync offset allows ±2s fine-tuning.
- **Sync debug**: when enabled in the popup, it shows `kind/unit/start/end/due/now/lag/delay/offset/q` below the caption and in the console to verify output isn't earlier than due time.
- **Translation cache/priority**: if preview and final share the same source, re-translation is avoided, and final is processed before preview.
- **Caption log**: 📜 (bottom-right) → scrollback panel / bilingual `.md` export.
- **Summary·Q&A**: the panel's ✨Summary · question box — the local 26B summarizes/answers over past captions (streaming).

## Troubleshooting
- "Bridge disconnected" on the overlay → check `run_bridge.sh` is running and port 8765.
- No captions → check the video has actual speech (non-speech is skipped as `[no speech]`) and the tab is making sound.
- No sound → tab capture intercepting playback; offscreen keeps the `source→destination` playback connection, so it's usually fine.
- Port-in-use error → `lsof -ti:8765 | xargs kill -9`.

## Tuning levers
- Reduce latency: translation uses the quality prompt by default (cost amortized by KV-cache). To reduce further, use `LCC_TX_PROFILE=fast` for a compact prompt and lower `SEG_SILENCE_MS`/`SOFT_MAX_SEC`. If you see truncation in long accuracy mode, raise only `LCC_ASR_MAX_TOKENS=96`.
- Felt parallelism: for English broadcasts, default to `Parakeet + aggressive` in the popup. Aggressive mode uses effective sentence silence ≤900ms, pending commit 120 chars/1.8s, preview debounce 180ms, 2 final recent contexts, 0 preview contexts to keep the MLX translation lane short. Parakeet soft-cut stays at 4.0s to avoid duplicate misrecognition. If captions swap too often, drop to `balanced`; if translation stability is paramount, `stable`. Server default is `LCC_LATENCY_MODE=aggressive`, accepting `stable|balanced|aggressive`.
- Output sync: the bridge transcribes long speech with a 4.5s soft-cut + 220ms overlap, and the screen schedules via a `performance.now()`-based stream clock. Short captions are merged only when the final backlog actually falls behind.
- Video delay: `delaySec` up to 12s. `videoDelay` mode captures at the original video frame resolution, capping frames at 60fps. Frame timestamps prefer `requestVideoFrameCallback` metadata, and the PCM tap prefers AudioWorklet.
- Improve translation quality: match the popup's **tone** preset to the content and pin proper nouns in the **glossary**. If you need cleaner transcription, turn on **accuracy mode** (2-pass). As a last resort, switch the translation model to 31B dense (5× slower). Benches: `bench_translate_quality.py` (tone/glossary A/B), `bench_2pass.py` (2-pass vs 1-pass) — both run with the bridge stopped.
- Hallucination/noise sensitivity: tune `webrtcvad.Vad(0..3)` aggressiveness.
- Local WS protection: by default only the Chrome extension origin + client token are allowed. To change the token, keep `LCC_WS_TOKEN` and `extension/protocol.js` in sync.
