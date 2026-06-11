"""Deterministic model-free backend for bridge E2E tests.

This module is imported only when ``LCC_BACKEND=fake``. It mirrors the backend
seam used by ``backend_cuda.py`` but performs no model loading, sleeping,
networking, randomness, or time-based work.
"""
from __future__ import annotations

import numpy as np


SR = 16000
FAKE_TRANSCRIPT_PREFIX = "fake utterance"
FAKE_TRANSLATION_PREFIX = "[fake-ko] "
FAKE_ASK_PREFIX = "[fake-answer] "
FAKE_SILERO_SENTINEL = object()


def _clean_text(value) -> str:
    return " ".join(str(value or "").split())


def fake_translate_text(text, target: str = "Korean", profile: str = "caption") -> str:
    src = _clean_text(text)
    if not src:
        return ""
    return f"{FAKE_TRANSLATION_PREFIX}{target}/{profile}: {src}"


def fake_ask_text(mode: str, transcript_text: str, question: str = "", target: str = "Korean") -> str:
    mode = _clean_text(mode) or "summary"
    q = _clean_text(question)
    tr = _clean_text(transcript_text)[:160]
    subject = q if q else tr
    return f"{FAKE_ASK_PREFIX}{target}/{mode}: {subject}"


def _pcm_has_signal(pcm: bytes) -> bool:
    if not pcm or len(pcm) < 2:
        return False
    arr = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16)
    return bool(arr.size and int(np.max(np.abs(arr.astype(np.int32)))) > 128)


def transcribe_pcm(pcm: bytes, hint: str = "", asr_engine=None):
    del hint, asr_engine
    if not _pcm_has_signal(pcm):
        return None
    dur_ms = int(round((len(pcm) / 2) * 1000 / SR))
    return f"{FAKE_TRANSCRIPT_PREFIX} {dur_ms}ms."


def translate_once(text, recent_pairs=(), target="Korean", hint="", register="casual",
                   glossary_pairs=(), on_update=None, kv_reuse=None, max_tokens=None,
                   stream_every=None, profile="caption", custom="", runtime=None, meta=None):
    del recent_pairs, hint, register, glossary_pairs, kv_reuse, max_tokens, stream_every, custom, runtime
    if meta is not None:
        meta["truncated"] = False    # deterministic fake render: never cap-truncated
    out = fake_translate_text(text, target=target, profile=profile)
    if on_update is not None and out:
        on_update(out)
    return out


def translate_page_batch_once(items, recent_pairs=(), target="Korean", hint="", register="casual",
                              glossary_pairs=(), max_tokens=None, kv_reuse=None,
                              on_segment=None, on_partial=None, custom="", runtime=None):
    del recent_pairs, hint, register, glossary_pairs, max_tokens, kv_reuse, custom, runtime
    out = {}
    for item in items or ():
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        source = str(item.get("text", "")).strip()
        if not item_id or not source:
            continue
        target_text = fake_translate_text(source, target=target, profile="page")
        out[item_id] = target_text
        if on_partial is not None:
            on_partial(item_id, source, target_text)
        if on_segment is not None:
            on_segment(item_id, source, target_text)
    return out


def run_ask(mode, transcript_text, question="", target="Korean", on_partial=None):
    out = fake_ask_text(mode, transcript_text, question, target)
    if on_partial is not None and out:
        on_partial(out)
    return out


def warm_selected(asr=False, lm=False, asr_engine=None):
    del asr, lm, asr_engine
    return None


def ensure_asr_loaded(engine=None):
    return engine


class FakeVADIterator:
    """Small deterministic VADIterator-compatible amplitude gate."""

    def __init__(self, model, threshold=0.5, sampling_rate=SR,
                 min_silence_duration_ms=250, speech_pad_ms=120):
        del model, speech_pad_ms
        self.threshold = float(threshold)
        self.sampling_rate = int(sampling_rate or SR)
        self.min_silence_duration_ms = int(min_silence_duration_ms)
        self.in_speech = False
        self.silence_ms = 0
        self.cursor_samples = 0

    def reset_states(self):
        self.in_speech = False
        self.silence_ms = 0
        self.cursor_samples = 0

    def __call__(self, window):
        arr = np.asarray(window, dtype=np.float32)
        n = int(arr.size)
        win_ms = int(round(1000 * n / max(1, self.sampling_rate))) if n else 0
        level = float(np.mean(np.abs(arr))) if n else 0.0
        is_voice = level >= self.threshold
        start_at = self.cursor_samples
        self.cursor_samples += n
        if is_voice:
            self.silence_ms = 0
            if not self.in_speech:
                self.in_speech = True
                return {"start": start_at}
            return None
        if self.in_speech:
            self.silence_ms += win_ms
            if self.silence_ms >= self.min_silence_duration_ms:
                self.in_speech = False
                self.silence_ms = 0
                return {"end": self.cursor_samples}
        return None
