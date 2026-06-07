# Nemotron-3.5 multilingual ASR (archived recipe)

> **Archived — removed from the live bridge.** A/B testing (English + Korean + Japanese TTS) showed
> Nemotron's multilingual ASR is clearly worse than the MLX-Gemma (E2B) engine — it drops words and
> mangles numbers (e.g. "스물여섯" → "스물여서"; Japanese dropped ~half the words) — and sherpa-onnx's
> cache-aware NeMo decoder is **greedy-only** so it can't be tuned up at runtime. The runtime backend
> (`backend_nemotron.py`) was deleted; recover it from git history
> (`git show 0d2fe901a:projects/live-caption/bridge/backend_nemotron.py`). This doc +
> `tools/bake_nemotron_prompt.py` remain as the full reproduction recipe if it's ever worth revisiting
> (e.g. a fixed `ko-KR` langid bake instead of `auto`).

A third ASR engine (`nemotron`) alongside Parakeet (EN) and MLX-Gemma (multilingual). It runs
NVIDIA's **`nemotron-3.5-asr-streaming-0.6b`** (cache-aware FastConformer-RNNT, 40 locales incl.
**Korean/Japanese/Chinese**) on **sherpa-onnx CPU** — auto-detects the spoken language and transcribes.

Select it in the popup ("Nemotron (다국어 자동)") or `LCC_ASR_ENGINE=nemotron`. The ONNX model is **not
checked in** (~650 MB); build it once with the recipe below and drop it in the model dir.

## Why a custom build (the catch)

The upstream checkpoint is **not directly sherpa-onnx-loadable**: it's a *prompt-conditioned* RNNT
(`EncDecRNNTBPEModelWithPrompt`). The target language is injected as a learned soft-prompt **after** the
encoder — a one-hot langid (128-dim) is concatenated to the encoder output (feature dim) and projected
back through `prompt_kernel = Linear(D+128, 2D) → ReLU → Linear(2D, D)`. Crucially this is
**shape-preserving** (no extra frames), so we can **bake a fixed prompt** into the exported encoder and
the result is a plain transducer that sherpa-onnx decodes normally. We bake `auto` (prompt idx 101) →
one model auto-detects all 40 languages and appends a `<xx-XX>` tag (the backend strips it).

## Reproduction recipe (one-time, ~25 min)

Runs on macOS CPU (no GPU). Uses a throwaway venv so the bridge env stays clean — the bridge only needs
**sherpa-onnx ≥ 1.13.0** at runtime (already present for Parakeet); NeMo is export-time only.

```bash
# 1) throwaway venv with Python 3.11 (uv auto-fetches it) + NeMo from git main
#    (the multilingual class EncDecRNNTBPEModelWithPrompt is only on main, not the pip release)
uv venv --python 3.11 /tmp/nemo-export/.venv
uv pip install --python /tmp/nemo-export/.venv/bin/python \
    "nemo_toolkit[asr] @ git+https://github.com/NVIDIA/NeMo.git@main" \
    onnxruntime kaldi-native-fbank soundfile librosa

# 2) plain transducer export (k2-fsa/sherpa-onnx's EN script, swap the model id + one chunk size)
cd /tmp/nemo-export
curl -fsSL https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/scripts/nemo/nemotron-speech-streaming-en-0.6b/export_onnx.py -o export_onnx.py
sed -i '' 's#nvidia/nemotron-speech-streaming-en-0.6b#nvidia/nemotron-3.5-asr-streaming-0.6b#g' export_onnx.py
sed -i '' 's#chunk_size_ms_list = \[80, 160, 560, 1120\]#chunk_size_ms_list = [1120]#' export_onnx.py
.venv/bin/python export_onnx.py        # -> 1120/{encoder,decoder,joiner}.int8.onnx + tokens.txt

# 3) bake the 'auto' language prompt into encoder.int8.onnx (ONNX graph surgery)
cp bridge/tools/bake_nemotron_prompt.py .
.venv/bin/python bake_nemotron_prompt.py     # edits 1120/encoder.int8.onnx in place

# 4) install into the model dir the bridge looks for
DIR=~/.local/share/models/live-caption/nemotron-3.5-multilingual-1120ms
mkdir -p "$DIR"
cp 1120/encoder.int8.onnx 1120/decoder.int8.onnx 1120/joiner.int8.onnx tokens.txt "$DIR"/
```

The bake (`bridge/tools/bake_nemotron_prompt.py`): for a fixed langid the one-hot folds into Linear1's
bias (`W1f = W1[:, :D]`, `b1f = b1 + W1[:, D+101]`), so it splices a 2-layer MLP
(`Transpose → MatMul+Add → ReLU → MatMul+Add → Transpose`) onto the encoder output before the graph
output. `int8` is self-contained (no external data).

## How the bridge uses it

`backend_nemotron.py` → `sherpa_onnx.OnlineRecognizer.from_transducer(...)`, fed a complete VAD clause
(accept_waveform + 0.5 s tail pad + input_finished + drain). The `<xx-XX>` auto-detect tag is stripped.
Shares the sherpa-onnx CPU pool (`_sherpa_pool`, with Parakeet), so it never stalls MLX translation.

## Notes / tuning

- **Experimental.** Nemotron's value is **multilingual auto-detect**, not English (Parakeet wins there).
  On full-clause finals it gets content words right but still drops function words and can mangle numbers
  (TTS A/B: "스물여섯" → "스물여서", "오늘은" dropped). The 0.6b streaming model has a real ceiling.
- **Decoding knobs** (env, no re-export — set + restart to A/B, logged at load as `[nemotron] decoding=…`):
  `LCC_NEMOTRON_DECODING` (default `greedy_search`), `LCC_NEMOTRON_BLANK_PENALTY` (0.0),
  `LCC_NEMOTRON_TAIL_PAD_MS` (700), `LCC_NEMOTRON_BEAM`, `LCC_NEMOTRON_THREADS`.
  **Caveat:** sherpa-onnx's cache-aware NeMo transducer impl is **greedy-only** — `modified_beam_search`
  is rejected at runtime, which also rules out hotword/contextual biasing (beam-only). In a TTS A/B,
  tail-pad (500–900 ms) and blank-penalty (0–1.0) moved the output **not at all**; and the rough
  "ranked **mas**" / "latest **pa**" truncations seen live were a *streaming-partial* artifact, absent in
  full-clause finals. So these are mostly safety knobs, not quality wins.
- **The real quality lever is the export**, not runtime: bake a fixed `ko-KR`=14 langid instead of
  `auto`=101 in the surgery script (dedicates the model to Korean instead of spending capacity on language
  ID), and/or drop `int8` for an fp32 encoder. Chunk size at export (560 ms vs 1120 ms) is a *latency*
  lever, not quality — 1120 ms already carries the most right-context.
- sherpa-onnx 1.13.2 (the bridge's version) already supports the cache-aware Nemotron format — no upgrade.
