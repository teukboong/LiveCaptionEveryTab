# Live Caption Every Tab — Realtime foreign→your-language captions for any site

[한국어](README.md) · **English** · [日本語](README.ja.md) · [Español](README.es.md) · [中文](README.zh.md)

> 🤖 This project was built **entirely through vibe coding (AI pair-programming)** — from the code to the documentation.

On YouTube, Twitch, **X**, or any site, it captures the browser tab's audio and uses a **local Gemma-4** to transcribe + translate, showing 2-line captions (source / your language) over the video. (Tab capture is domain-agnostic, so any tab with sound works.)
For transcription you pick in the popup between **Granite Speech 4.1** (strong English), **Qwen3-ASR** (multilingual incl. Japanese/Korean), and **Whisper Large v3** (multilingual). Granite/Qwen3 emit punctuation and casing natively and gate silence with `[no speech]`; Whisper runs as a dedicated engine with its own decode (no prompt).

> The world holds endless video and audio, yet the language barrier still stands as a **content barrier**.
> This was built in the spirit of poking one small hole in that wall.

## Why this exists (similar tools already exist)

Realtime captioning/translation tools mostly split into two camps, and the combination **"in the browser / any live tab / fully local LLM by-meaning translation"** was missing — this fills that gap.

| | This project | Whisper-based browser extensions | Desktop players (e.g. LLPlayer) |
|---|---|---|---|
| **Input** | **Any tab** with sound (incl. live streams) | Tab audio | Downloaded video / files·URLs fed into a player |
| **ASR** | Granite / Qwen3 / Whisper (Granite·Qwen3 do native punctuation·truecasing; silence·music gated with `[no speech]`) | Mostly Whisper | Mostly Whisper |
| **Translation** | **Local LLM (Gemma-4)** by-meaning — keeps context·pronouns | None / literal MT / cloud | Local LLM possible (Ollama, etc.) |
| **Execution** | 100% local (zero cloud) | Local~mixed | Local |
| **Target language** | Korean-first (+multilingual) | Varies | Multilingual (per-language tuning varies) |

- **Whisper-based extensions** capture the tab well, but Whisper tends to hallucinate captions over silence/music, and translation is often absent, literal, or cloud-based. → Here it's solved differently: punctuation-native ASR + silence gating + local Gemma by-meaning translation.
- **Desktop players** have great local-LLM translation, but you must download the video or feed it into the player, which doesn't fit live streams / arbitrary sites. → Here, no downloading — it overlays **right on any tab that makes sound**.
- **Not just sound — text too.** The page body (DOM) in the same tab often needs translating as well, but the browser's built-in / cloud page translation ships the text out and leans literal. → Here the *same local Gemma, glossary, and context* that run the captions are applied to the page too, swapping the body DOM in place with no overlay. The goal was to handle a tab's **sound and text with one local translator**.

Everything is **local and free**. The trade-off is a hardware floor (see requirements in [SETUP.md](SETUP.md)). Translation and transcription models are now chosen from **dropdowns** in the popup — **Auto** (fits the model to free memory, now over a model registry), a curated list, or a custom HF id; models you haven't downloaded show a **Download button**.

## Platform — two runtimes (equally supported)

The same bridge·same extension run on both backends. Pick the one for your machine with `LCC_BACKEND`.

| Backend | Environment | Transcription (ASR) | Translation | Guide |
|---|---|---|---|---|
| **MLX** (`LCC_BACKEND=mlx`) | Apple Silicon | Granite/Qwen3 (mlx-audio, in-process) / Whisper (mlx_whisper, 6bit) | Gemma-4 (26B/E4B/E2B · pick or Auto) (mlx-lm) | [SETUP.md](SETUP.md) |
| **CUDA** (`LCC_BACKEND=cuda`) | Windows + NVIDIA (WSL2) | Granite/Qwen3 (transformers, `cuda/asr_server.py`) / Whisper (whisper.cpp q6, unverified) | llama.cpp · GGUF (26B/E4B/E2B · pick or Auto) (OpenAI-compatible HTTP) | [SETUP-windows.md](SETUP-windows.md) |

The transcription-engine choice (English=granite / multilingual=qwen3 / multilingual=whisper) is identical on both (routed via the `model` field). VAD·sentence assembly·scheduler·number-guard·prompt builder are **shared across both backends** (pure functions); only the 3 GPU functions (transcribe/translate/summarize) change per runtime, and that boundary is `bridge/backend_cuda.py` (HTTP) and the "Backend seam" in server.py. (The code default is `mlx`.)

## Architecture
```
[Chrome extension] tabCapture (tab audio) ──WS(PCM16 16k)──▶ [bridge/server.py]
                                                        VAD + soft-cut ASR atom
                                                        → Granite / Qwen3-ASR / Whisper transcription (punctuation·multilingual)
                                                        → unit assembler
                                                        → Gemma-4 translation
   [content.js 2-line overlay] ◀──WS(JSON caption)──────┘
```
- ASR picks between **three transcription engines** in the popup (▸ Transcription engine). **Granite Speech 4.1 2B** (`ibm-granite/granite-speech-4.1-2b` · faithful English, ~0% WER) and **Qwen3-ASR 1.7B** (`Qwen/Qwen3-ASR-1.7B` · 52 languages incl. Japanese/Korean, auto language ID) run via **mlx-audio**; both emit punctuation·truecasing natively so sentence chunking just works. **Whisper Large v3** (multilingual) runs via **mlx_whisper** as a dedicated engine (auto-quantized to **MLX 6bit** on download, own decode, no prompt). Shares the Apple GPU with the translator (serialized). ⚠ granite needs the **conv fix on mlx-audio main** (see SETUP).
- A low-latency English-only Parakeet is a power-user escape hatch via `LCC_ASR_ENGINE=parakeet` only (CPU, parallel to translation; model `~/.local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8`, `sherpa-onnx==1.13.2`). The popup selector only exposes granite/qwen3/whisper.
- Translation: a **selectable Gemma-4 model** — `gemma-26b` (26B-A4B, mlx-lm), `gemma-e4b` (E4B) and `gemma-e2b` (E2B) (E4B/E2B load via mlx_vlm), or **Auto** to fit free memory — default **quality prompt** (expert interpreter·by-meaning·no-translationese + 3 few-shots, cost amortized by KV-cache → natural spoken output rather than stiff written style). Low latency via `LCC_TX_PROFILE=fast`. **Target language is selectable** (45 languages — Gemma is broadly multilingual), source auto-detected, skipped when target=source.
- RAM ~26GB (gemma-26b weights; gemma-e4b ~8 / gemma-e2b ~6GB are smaller) + a little KV per chunk. Latency ~2.9–3.4s per utterance chunk (ASR ~0.7s + translation ~1.4s + audio prefill + clause-boundary wait).
- MTP is pointless on this hardware, so unused (verified across MoE·dense·E4B).
- ⚠️ Needs genuine Chrome/Edge/Brave — some Chromium forks (e.g. ChatGPT Atlas) don't implement `chrome.tabCapture`.

## Install (easiest path)

If the terminal isn't your thing, **double-click to install**:
- **macOS** — double-click `install-mac.command` (if blocked, right-click → Open). Sets up the venv, deps, and the popup host in one go.
- **Windows** — double-click `install-windows-oneclick.bat` (WSL2 + CUDA + model, automatic).

After that the **extension popup does everything** — start the bridge, and **pick & download** the translation and transcription models you want from the dropdowns (Download button on any you don't have yet) to save disk. (Terminal folks, optional: `./setup.sh [--models --tier lite]` still works — the tier flag maps to a model for back-compat.)

## Run
### 1) Bridge server
```bash
# from the repo root (first time, run ./setup.sh to install venv·deps)
bash bridge/run_bridge.sh
# ready when "[bridge] ready  ws://127.0.0.1:8765" appears (first load ~40s)
```
- To keep it always on (opt-in, auto-restart on crash): `bash bridge/autostart.sh install` — ⚠ ~26GB RAM resident (gemma-26b). Off: `… uninstall`
- Without a terminal, the popup does it all (**Start bridge** · pick & **download** models from the dropdowns) — it needs the native-messaging host, which **`./setup.sh` already installs** (the one bootstrap Chrome's sandbox can't do). Then reload the extension. Runs detached, survives closing the browser (SETUP 6.5).
- If the bridge restarts/drops, the extension **auto-reconnects** (backoff) and buffers up to 6s of recent audio. Speech during longer outages may be lost.
### 2) Load the extension (Chrome)
1. `chrome://extensions` → turn on **Developer mode** (top right)
2. **Load unpacked** → select this repo's `extension/` folder
3. On a YouTube/Twitch video tab, **click the extension icon** → pick the **`Page translation` / `Video translation`** toggle in the popup → click **`Start captions`** → badge `ON`
4. Popup settings: page translation replaces the real DOM text in place, video translation shows overlay captions. Caption **size·vertical/horizontal position·source line·sync offset** (live), **sentence wait·voice detection** (applied on restart)
5. Stop again with **`Stop captions`**. (tabCapture requires a user click gesture → no auto-start)

## Features
- **Term memory (auto-glossary)**: recurring names/terms mined from captions pin into the glossary automatically — consistent renderings + fuzzy ASR spelling repair. Remembered per site and re-seeded into both caption and page translation on your next visit (toggle in popup)
- **Dual-model concurrency**: with enough RAM, a small E2B translator loads next to the 26B and takes previews + page-DOM batches — finals stay 26B quality, and page translation stops competing with captions (`LCC_AUX_LM=off`)
- **Speaker tagging (beta)**: ①② speaker labels on captions for podcasts/interviews — CPU speaker embeddings + online clustering, ~25MB model auto-downloads on first use
- **Input write-back**: compose in your language in any text field, press the ⇄ chip or Alt+T to render it in the page's language (one-click revert) — the reading lens becomes participation
- **Image translation (macOS)**: Alt+hover an image → Apple Vision OCR (ANE, no model download) → translated overlay over memes/screenshots
- **Inline original (inline ghost)**: keep the original faintly under translated long paragraphs — no hover needed
- **Page translation in iframes**: embedded widgets and iframe bodies translate too (real content frames auto-detected)
- **Auto term priming**: auto-injects the page/video title as ASR·translation hints (toggle off in the popup).
- **Page-translation mode**: enable `Page translation` alone in the popup and it replaces the current tab's actual DOM text nodes with the translation directly, no overlay. Turn on `Page translation` + `Video translation` together and they share the same bridge connection, with page translation running as an auxiliary lane that yields and retries when final/preview caption translation is busy. You can give it its own page-specific register/glossary/hints, choose output between `live partial` / `final only`, hover the translated text to see the original (bilingual view), and `idle re-verify of cached translation` re-checks cached translations while idle and patches that spot if the model now disagrees. Page translation binds to the tab you started it on and does not follow tab switches (only that tab is translated); leave the page hint/glossary blank to inherit the video settings.
- **Content-type presets**: pick a content type once (general·chat / conference·lecture / news·interview / personal streaming) and it bundles register (tone) + latency mode — lecture=formal·stable, news=balanced, streaming=colloquial·instant. Tone·sentence-endings·few-shot anchors adapt to the content, and the source language (EN/JA) is auto-detected to pick matching examples.
- **Glossary**: enter `name=translation` (one per line) in the popup to bias transcription + always render that term identically in translation (removes the wobble of a name translated differently each line). `Term hints` is free-text biasing. You can also add a term right on the page with **Alt+G**, which opens an input bar prefilled with the last source line.
- **Custom translation prompt**: replace the descriptive part of the prompt with your own instructions while the output format + glossary are kept intact — applies to both captions and page translation.
- **Named presets**: save a translation bundle under a name and pick it back from **Simple**, so you can switch your whole translation setup in one tap.
- **Accuracy mode (2-pass re-transcription)**: when on, multi-clause sentences finalized by a natural end (pause/eos) or terminal punctuation get their accumulated audio re-transcribed once as a whole right before commit → removes boundary errors from stitching VAD fragments. Finalization is ~0.7s slower, so it's a toggle (default OFF). Units whose alignment broke from overlap/split are auto-excluded (`unit_pure` guard).
- **Streaming captions**: the source line appears first per ASR atom; the translated preview is debounced/coalesced. Committed captions are prioritized in the final queue.
- **3 latency modes**: `aggressive` overlaps ASR and translation on the same GPU (separate device locks) and pre-translates the current unit preview latest-only; `balanced` previews only when the GPU is idle; `stable` shows only committed translations. Final translation always takes priority over preview.
- **Lookahead video delay**: in video-delay mode the actual audio is transcribed·translated immediately, and captions are scheduled to the real PCM stream-start clock and the utterance window (`start_ms`/`end_ms`). The popup's sync offset allows ±2s fine-tuning.
- **Sync debug**: when enabled in the popup, it shows `kind/unit/start/end/due/now/lag/delay/offset/q` below the caption and in the console to verify output isn't earlier than due time.
- **Translation cache/priority**: if preview and final share the same source, re-translation is avoided, and final is processed before preview.
- **Caption log**: the popup's caption scrollback + bilingual `.md` export (`.md` button).
- **"What did they just say" (Alt+R)**: replay recent captions in a panel — a text DVR with no audio replay (recent finalized captions, newest at the bottom, Esc to close).
- **Summary·Q&A**: the panel's Summary · question box — the local Gemma summarizes/answers over past captions (streaming).

## Troubleshooting
- "Bridge disconnected" on the overlay → check `run_bridge.sh` is running and port 8765.
- No captions → check the video has actual speech (non-speech is skipped as `[no speech]`) and the tab is making sound.
- No sound → tab capture intercepting playback; offscreen keeps the `source→destination` playback connection, so it's usually fine.
- Port-in-use error → use the popup's `Bridge Stop` first; if a listener remains, run `python3 extension/native-host/lcc_bridge_host.py stop` to stop only this checkout's bridge. If it reports a foreign PID, inspect the owner with `lsof -nP -iTCP:8765 -sTCP:LISTEN`.

## Tuning levers
- Reduce latency: translation uses the quality prompt by default (cost amortized by KV-cache). To reduce further, use `LCC_TX_PROFILE=fast` for a compact prompt and lower `SEG_SILENCE_MS`/`SOFT_MAX_SEC`. If you see truncation in long accuracy mode, raise only `LCC_ASR_MAX_TOKENS=96`.
- Felt parallelism: the default `aggressive` mode overlaps ASR and translation on a single GPU (separate `_ASR_DEVICE_LOCK`/`_MLX_DEVICE_LOCK`) to fill bandwidth gaps. It uses effective sentence silence ≤900ms, pending commit 120 chars/1.8s, preview debounce 180ms, 2 final recent contexts, 0 preview contexts to keep the translation lane short. If captions swap too often, drop to `balanced`; if translation stability is paramount, `stable`. Server default is `LCC_LATENCY_MODE=aggressive`, accepting `stable|balanced|aggressive`. If you need lower latency for English only, the `LCC_ASR_ENGINE=parakeet` escape hatch (CPU transcription, so it runs parallel to GPU translation, soft-cut 4.0s).
- Output sync: the bridge transcribes long speech with a 4.5s soft-cut + 220ms overlap, and the screen schedules via a `performance.now()`-based stream clock. Short captions are merged only when the final backlog actually falls behind.
- Video delay: `delaySec` up to 12s. `videoDelay` mode captures at the original video frame resolution, capping frames at 60fps. Frame timestamps prefer `requestVideoFrameCallback` metadata, and the PCM tap prefers AudioWorklet.
- Improve translation quality: match the popup's **tone** preset to the content and pin proper nouns in the **glossary**. If you need cleaner transcription, turn on **accuracy mode** (2-pass). As a last resort, switch the translation model to 31B dense (5× slower). Benches: `bench_translate_quality.py` (tone/glossary A/B), `bench_2pass.py` (2-pass vs 1-pass) — both run with the bridge stopped.
- Hallucination/noise sensitivity: tune `webrtcvad.Vad(0..3)` aggressiveness.
- Local WS protection: by default only the Chrome extension origin + client token are allowed. To change the token, keep `LCC_WS_TOKEN` and `extension/protocol.js` in sync.
