"""Speaker tagging lite — per-clause speaker embeddings (sherpa-onnx, CPU) + online clustering.

Podcasts/interviews/debates read much better when captions mark who is talking. This stays deliberately
small: every VAD-passed clause gets ONE embedding from a sherpa-onnx speaker model (CPU — the same
no-GPU-contention deal as Parakeet), and a pure online cosine-similarity clusterer assigns stable
1-based labels per connection. No segmentation model, no offline re-clustering — tab audio is messy
(music/SFX), so labels are a UX hint, not ground truth.

The embedding model auto-downloads on first enable (~25MB, 3D-Speaker ERes2Net base — speaker
embeddings transfer across languages well enough for tagging). Override with LCC_SPK_MODEL.
Pure clustering tested in test_diarize.py; the extractor needs the model file (not in model-free tests).
"""
import math
import os
import threading
import urllib.request

SPK_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                 "speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
SPK_MODEL_PATH = os.environ.get(
    "LCC_SPK_MODEL",
    os.path.expanduser("~/.local/share/models/live-caption/3dspeaker_eres2net_base_16k.onnx"))
SPK_THRESHOLD = float(os.environ.get("LCC_SPK_THRESHOLD", "0.55"))   # cosine floor to join an existing speaker
SPK_MAX_SPEAKERS = max(2, int(os.environ.get("LCC_SPK_MAX", "6")))
SPK_MIN_SEC = float(os.environ.get("LCC_SPK_MIN_SEC", "0.6"))        # shorter audio gives junk embeddings
SPK_EMA = float(os.environ.get("LCC_SPK_EMA", "0.12"))               # centroid drift rate on re-assignment
SPK_THREADS = max(1, int(os.environ.get("LCC_SPK_THREADS", "2")))
SR = 16000

_extractor = None
_extractor_lock = threading.Lock()


def _norm(vec):
    s = math.sqrt(sum(x * x for x in vec))
    if s <= 0:
        return None
    return [x / s for x in vec]


class OnlineSpeakerClusters:
    """Pure online clustering over unit-norm embeddings: cosine to running centroids, join when above
    the threshold (EMA-updating the centroid), open a new speaker below it while capacity remains, and
    once at capacity fall back to the closest existing speaker WITHOUT polluting its centroid. Labels
    are stable 1-based ints for the lifetime of the instance (one per bridge connection)."""

    def __init__(self, threshold=None, max_speakers=None, ema=None):
        self.threshold = SPK_THRESHOLD if threshold is None else float(threshold)
        self.max_speakers = SPK_MAX_SPEAKERS if max_speakers is None else int(max_speakers)
        self.ema = SPK_EMA if ema is None else float(ema)
        self.centroids = []

    def add(self, vec):
        """Assign a (raw) embedding to a speaker label. Returns 1-based int, or None on a bad vector."""
        v = _norm(list(vec or ()))
        if v is None:
            return None
        best_i, best_sim = -1, -2.0
        for i, c in enumerate(self.centroids):
            sim = sum(a * b for a, b in zip(c, v))
            if sim > best_sim:
                best_i, best_sim = i, sim
        if best_i >= 0 and best_sim >= self.threshold:
            mixed = [(1.0 - self.ema) * a + self.ema * b for a, b in zip(self.centroids[best_i], v)]
            self.centroids[best_i] = _norm(mixed) or self.centroids[best_i]
            return best_i + 1
        if len(self.centroids) < self.max_speakers:
            self.centroids.append(v)
            return len(self.centroids)
        return (best_i + 1) if best_i >= 0 else None     # at capacity: closest label, centroid untouched


def model_present():
    return os.path.isfile(SPK_MODEL_PATH) and os.path.getsize(SPK_MODEL_PATH) > 1_000_000


def _download_model():
    os.makedirs(os.path.dirname(SPK_MODEL_PATH), exist_ok=True)
    tmp = SPK_MODEL_PATH + ".part"
    print(f"[diarize] downloading speaker model (~25MB) -> {SPK_MODEL_PATH}", flush=True)
    urllib.request.urlretrieve(SPK_MODEL_URL, tmp)        # nosec - fixed release asset URL
    os.replace(tmp, SPK_MODEL_PATH)


def ensure_extractor():
    """Load (and if needed download) the speaker embedding extractor. Idempotent; raises on failure.
    Call on a CPU pool — the download can take a while on a slow link."""
    global _extractor
    with _extractor_lock:
        if _extractor is not None:
            return _extractor
        if not model_present():
            _download_model()
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=SPK_MODEL_PATH, num_threads=SPK_THREADS, provider="cpu")
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        print("[diarize] speaker embedding extractor ready", flush=True)
        return _extractor


def embed(pcm: bytes):
    """One speaker embedding for a 16k mono PCM16 clause, or None when the clip is too short or the
    extractor isn't ready. Call on a CPU pool."""
    if not pcm or len(pcm) < int(SPK_MIN_SEC * SR) * 2:
        return None
    ext = ensure_extractor()
    import numpy as np
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    stream = ext.create_stream()
    stream.accept_waveform(SR, samples)
    stream.input_finished()
    if not ext.is_ready(stream):
        return None
    return list(ext.compute(stream))
