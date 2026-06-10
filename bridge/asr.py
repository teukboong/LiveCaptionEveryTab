"""ASR runtime leaves for the live-caption bridge.

This module owns the MLX ASR implementation and post-ASR glossary repair. Model
instances still live in model_runtime; ASR loaders update that module by
attribute assignment so runtime state has a single owner.
"""
import difflib
import os
import re

import numpy as np

from backend_parakeet import ParakeetAsr
import model_runtime
from text_helpers import _gr_norm

ASR_MAX_TOKENS = max(32, int(os.environ.get("LCC_ASR_MAX_TOKENS", "64")))   # per-segment generation cap (granite/qwen3)


def _ensure_asr_loaded(engine: str):
    model_runtime._finalize_model_config()   # resolve MLXA_REPOS / qwen3 default before the first load
    engine = model_runtime._normalize_asr_engine(engine, model_runtime.ASR_ENGINE)
    if engine == "parakeet":
        if not model_runtime.PARAKEET_MODEL_DIR:
            raise RuntimeError("LCC_PARAKEET_MODEL_DIR is required when LCC_ASR_ENGINE=parakeet")
        if model_runtime.parakeet_asr is None:
            print(
                f"[bridge] loading Parakeet ASR ({model_runtime.PARAKEET_PROVIDER}, threads={model_runtime.PARAKEET_THREADS}) from {model_runtime.PARAKEET_MODEL_DIR}…",
                flush=True,
            )
            model_runtime.parakeet_asr = ParakeetAsr(
                model_runtime.PARAKEET_MODEL_DIR,
                num_threads=model_runtime.PARAKEET_THREADS,
                provider=model_runtime.PARAKEET_PROVIDER,
            )
        return engine

    if model_runtime._is_mlxa_engine(engine):
        model_runtime._require_mlx()
        if model_runtime.mlxa_model is None or model_runtime.mlxa_loaded_engine != engine:
            from mlx_audio.stt.utils import load_model as _mlxa_load
            repo = model_runtime.MLXA_REPOS[engine]
            print(f"[bridge] loading {repo} ({engine} audio ASR)…", flush=True)
            model_runtime.mlxa_model = _mlxa_load(repo)
            model_runtime.mlxa_loaded_engine = engine
        return engine

    if model_runtime._is_whisper_engine(engine):
        # Whisper (large-v3) — dedicated ASR via mlx_whisper. The 6bit model is produced/fetched by
        # install_models (prequant-first, else local quantize); here we just ensure mlx_whisper is present
        # and record the repo. mlx_whisper.transcribe() lazily loads + caches the model by path, so the
        # first real transcribe warms it. No prompt (own decode + langID) — INV-7.
        model_runtime._require_mlx()
        repo = model_runtime.WHISPER_REPO
        if model_runtime.whisper_loaded_repo != repo:
            import mlx_whisper  # noqa: F401  — fail fast if the dep is missing (install ensures it)
            print(f"[bridge] using {repo} (whisper ASR)…", flush=True)
            model_runtime.whisper_loaded_repo = repo
        return engine

    raise RuntimeError(f"unknown ASR engine: {engine}")


# --- Post-ASR glossary repair (phonetic/fuzzy) ---------------------------------------------------------
# Granite drops punctuation/casing the moment ANY text hint is appended to its prompt (see transcribe_pcm),
# so glossary terms cannot bias the ASR itself. Repair downstream instead: fuzzy-match each user glossary
# source term against the transcript and rewrite near-misses ("black well" / "Blackwel") to the canonical
# spelling BEFORE translation, where the pinned glossary rendering then applies exactly. Pure; applied in
# handle()'s transcribe() wrapper. Off: LCC_ASR_GLOSSARY_REPAIR=0. Tested in test_glossary_repair.py.
GLOSSARY_REPAIR_ON = os.environ.get("LCC_ASR_GLOSSARY_REPAIR", "1") == "1"
_GR_MIN_TERM_CHARS = 4          # shorter terms ("AI", "Go") are too collision-prone to fuzzy-match
_GR_RATIO = 0.84                # SequenceMatcher floor on normalized strings (exact match short-circuits)
_GR_TOKEN_RE = re.compile(r"\S+")
_GR_EDGE_RE = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)

def _repair_glossary_terms(text: str, glossary_pairs):
    """Rewrite fuzzy ASR spellings of glossary source terms to their canonical form. Window sizes n-1..n+1
    around each term's word count catch split ('black well') and merged ('SamAltman') transcriptions; the
    surrounding punctuation of the matched span is preserved. Replacements never overlap, longest-window
    match wins, and an exact normalized match of a DIFFERENT glossary term is never rewritten."""
    if not GLOSSARY_REPAIR_ON or not text or not glossary_pairs:
        return text
    terms = []
    norms = {}
    for src, _tgt in glossary_pairs:
        src = (src or "").strip()
        n = _gr_norm(src)
        toks = [t for t in (_gr_norm(w) for w in src.split()) if t]
        if len(n) >= _GR_MIN_TERM_CHARS and toks:
            terms.append((src, n, toks))
            norms[n] = src
    if not terms:
        return text
    tokens = list(_GR_TOKEN_RE.finditer(text))
    if not tokens:
        return text
    edits = []                                   # (start, end, replacement) on the original string
    taken = [False] * len(tokens)
    def _tok_fuzzy(a, b):
        if min(len(a), len(b)) < 3:                      # tiny tokens must match exactly
            return a == b
        return difflib.SequenceMatcher(None, a, b).ratio() >= _GR_RATIO

    for src, term_norm, term_toks in terms:
        nwords = len(term_toks)
        for width in sorted({w for w in (nwords + 1, nwords, nwords - 1) if 1 <= w <= 4}, reverse=True):
            i = 0
            while i + width <= len(tokens):
                if any(taken[i:i + width]):
                    i += 1
                    continue
                span = text[tokens[i].start():tokens[i + width - 1].end()]
                m = _GR_EDGE_RE.match(span)
                pre, core, post = m.group(1), m.group(2), m.group(3)
                cand = _gr_norm(core)
                if not cand or abs(len(cand) - len(term_norm)) > 3:
                    i += 1
                    continue
                exact = cand == term_norm
                if not exact and cand in norms:          # exactly some OTHER term -> leave it alone
                    i += 1
                    continue
                if not exact:
                    # Fuzzy only at the term's own word count, compared TOKEN BY TOKEN — a whole-span ratio
                    # would let "met SamAltman" absorb the neighboring word. Split/merge windows (n±1)
                    # must match the normalized term exactly.
                    wtoks = [_gr_norm(text[t.start():t.end()]) for t in tokens[i:i + width]]
                    if (width != nwords or len(wtoks) != nwords
                            or not all(_tok_fuzzy(a, b) for a, b in zip(wtoks, term_toks))):
                        i += 1
                        continue
                if core != src:                          # exact-with-different-surface still canonicalizes
                    edits.append((tokens[i].start() + len(pre),
                                  tokens[i + width - 1].end() - len(post), src))
                for k in range(i, i + width):
                    taken[k] = True
                i += width
    if not edits:
        return text
    out = text
    for start, end, rep in sorted(edits, reverse=True):
        out = out[:start] + rep + out[end:]
    return out


# ASR prompts now live with the active mlx-audio backend wiring above.


def transcribe_pcm(pcm: bytes, hint: str = "", asr_engine=None):
    engine = model_runtime._normalize_asr_engine(asr_engine, model_runtime.ASR_ENGINE)
    if engine == "parakeet":
        if model_runtime.parakeet_asr is None:
            raise RuntimeError("Parakeet ASR is not loaded")
        return model_runtime.parakeet_asr.transcribe_pcm(pcm, hint=hint)

    model_runtime.mx.set_default_device(model_runtime.mx.gpu)
    # 16k mono float32 array straight to the audio model: load_audio()/generate take ndarrays as-is
    # (no resample), so we skip the per-segment /tmp WAV write+decode round-trip.
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    audio *= 1.0 / 32768.0

    if model_runtime._is_mlxa_engine(engine):                          # mlx-audio audio-LLM (granite / qwen3)
        if model_runtime.mlxa_model is None:
            raise RuntimeError(f"{engine} ASR is not loaded")
        # No ASR-side text hint: qwen3 auto-detects language + punctuates with no prompt; granite's punctuation
        # is fragile — ANY appended hint ("Keywords:"/"Expected names:") suppresses capitalization+punctuation —
        # so keep the clean instruction. (Glossary still biases the 26B translation; only source-side name
        # spelling is dropped, and both models already transcribe names well.)
        gen_kw = {"prompt": model_runtime.GRANITE_ASR_PROMPT} if engine == "granite" else {}
        res = model_runtime.mlxa_model.generate(audio, temperature=0.0, max_tokens=ASR_MAX_TOKENS, **gen_kw)
        raw = getattr(res, "text", None)
    elif model_runtime._is_whisper_engine(engine):                     # Whisper large-v3 — own decode, no prompt (INV-7)
        import mlx_whisper
        # Each VAD chunk is independent: don't condition on previous text (avoids cross-chunk drift).
        res = mlx_whisper.transcribe(audio, path_or_hf_repo=model_runtime.WHISPER_REPO,
                                     temperature=0.0, condition_on_previous_text=False, verbose=False)
        raw = res.get("text") if isinstance(res, dict) else getattr(res, "text", None)
    else:
        raise RuntimeError(f"unknown ASR engine: {engine}")
    text = (raw if raw is not None else str(res)).strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    dedup = []
    for l in lines:                       # collapse consecutive echoed lines; keep distinct content
        if not dedup or dedup[-1] != l:
            dedup.append(l)
    text = " ".join(dedup)
    if not text or "[no speech]" in text.lower():
        return None
    return text


def build_asr_hint(context_hint, glossary_pairs, auto_glossary_pairs):
    # free-text context + user glossary source terms + auto-pinned terms -> ASR name biasing
    terms = ", ".join(s for s, _ in glossary_pairs)
    auto = ", ".join(s for s, _ in auto_glossary_pairs)
    return "; ".join(x for x in (context_hint, terms, auto) if x)[:240]
