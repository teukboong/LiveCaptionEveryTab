"""Speaker tagging lite — per-unit speaker embeddings (sherpa-onnx, CPU) + online clustering.

Podcasts/interviews/debates read much better when captions mark who is talking. This stays deliberately
small: every committed translation unit gets ONE embedding from a sherpa-onnx speaker model (CPU — the
same no-GPU-contention deal as Parakeet), and a pure online clusterer assigns stable labels per
connection. No segmentation model, no offline re-clustering — tab audio is messy (music/SFX), so labels
are a UX hint, not ground truth.

Accuracy comes from four stacked defenses (all pure, tested in test_diarize.py):
  - hysteresis: join a speaker (and update its centroid) only above `hi`; between `lo` and `hi` the clip
    is LABELED but never pollutes the centroid; below `lo` it is an unknown voice.
  - turn continuity: the previous unit's speaker gets a small similarity bonus — conversation turns are
    sticky, and this prior is nearly free.
  - two-strike new speakers: a single unknown clip (music sting, SFX, garbled audio) never opens a
    speaker; two consecutive mutually-similar unknowns do. The first of the pair stays untagged.
  - periodic merge: clusters whose centroids converge above `merge_at` collapse into the earlier label,
    healing the same-voice-split-into-three failure for all future clips.

Embedding models are curated (auto-download on first enable; LCC_SPK_MODEL_ID picks, LCC_SPK_MODEL
pins an explicit file). Default is the strongest VoxCeleb-trained large-margin model sherpa-onnx ships
(ResNet293-LM) — VoxCeleb is YouTube interview audio, i.e. exactly this domain; one embedding per
committed sentence costs ~0.2s of CPU, which the sherpa pool absorbs without touching the GPU. True
research SOTA (WavLM fusion, ReDimNet) would drag in a torch runtime for a marginal clean-benchmark
gain — the practical ceiling here is clip length/noise, not the last 0.1% EER.
"""
import math
import os
import threading
import urllib.request

_SPK_URL_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
SPK_DIR = os.path.expanduser("~/.local/share/models/live-caption")
# Per-model cosine thresholds (hi = join+update, lo = label-only floor); calibrate live with LCC_SPK_DEBUG=1.
SPK_MODELS = {
    "resnet293": {  # default: WeSpeaker ResNet293 VoxCeleb LM (109MB) — strongest sherpa-onnx asset
        "file": "wespeaker_en_voxceleb_resnet293_LM.onnx",   # (VoxCeleb1-O EER ~0.45%); ~190ms per 4s clip on CPU
        "url": _SPK_URL_BASE + "wespeaker_en_voxceleb_resnet293_LM.onnx",
        # Calibrated on two-voice TTS clips: same-speaker cos 0.85-0.97, different-speaker 0.45-0.53.
        # lo MUST clear the impostor range — the original 0.35 put different speakers inside the
        # label-only band, so a second speaker could never open ("화자 구분이 안 됨" bug).
        "hi": 0.66, "lo": 0.58,
    },
    "campplus": {   # WeSpeaker CAM++ VoxCeleb LM (28MB) — measured POOR separation on the same calibration
        "file": "wespeaker_en_voxceleb_CAM++_LM.onnx",       # (same-speaker min 0.19 < diff max 0.79) — avoid;
        "url": _SPK_URL_BASE + "wespeaker_en_voxceleb_CAM++_LM.onnx",   # kept only for explicit opt-in
        "hi": 0.66, "lo": 0.58,
    },
    "titanet": {    # NeMo TitaNet-large (97MB) — strongest, heavier per clip
        "file": "nemo_en_titanet_large.onnx",
        "url": _SPK_URL_BASE + "nemo_en_titanet_large.onnx",
        "hi": 0.60, "lo": 0.40,
    },
    "eres2net": {   # previous default (3D-Speaker, zh-cn) — kept for files already downloaded
        "file": "3dspeaker_eres2net_base_16k.onnx",
        "url": _SPK_URL_BASE + "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
        "hi": 0.55, "lo": 0.38,
    },
}
SPK_MODEL_ID = os.environ.get("LCC_SPK_MODEL_ID", "resnet293").strip().lower()
SPK_MODEL_PATH = os.environ.get("LCC_SPK_MODEL", "")     # explicit .onnx path overrides the registry
SPK_MAX_SPEAKERS = max(2, int(os.environ.get("LCC_SPK_MAX", "6")))
SPK_MIN_SEC = float(os.environ.get("LCC_SPK_MIN_SEC", "1.2"))   # shorter audio gives junk embeddings
SPK_PREV_BONUS = float(os.environ.get("LCC_SPK_PREV_BONUS", "0.07"))
SPK_TOPK = max(2, int(os.environ.get("LCC_SPK_TOPK", "10")))    # embeddings kept per speaker (centroid = mean)
SPK_MERGE_EVERY = max(2, int(os.environ.get("LCC_SPK_MERGE_EVERY", "8")))
SPK_MERGE_AT = float(os.environ.get("LCC_SPK_MERGE_AT", "0.70"))
SPK_THREADS = max(1, int(os.environ.get("LCC_SPK_THREADS", "2")))
SPK_DEBUG = os.environ.get("LCC_SPK_DEBUG", "0") == "1"          # per-clip similarity log -> calibrate thresholds
SR = 16000

_extractor = None
_extractor_lock = threading.Lock()


def _active_model():
    return SPK_MODELS.get(SPK_MODEL_ID, SPK_MODELS["resnet293"])


def _model_path():
    return SPK_MODEL_PATH or os.path.join(SPK_DIR, _active_model()["file"])


def _env_float(name, default):
    try:
        raw = os.environ.get(name)
        return float(raw) if raw not in (None, "") else float(default)
    except Exception:
        return float(default)


def _norm(vec):
    s = math.sqrt(sum(x * x for x in vec))
    if s <= 0:
        return None
    return [x / s for x in vec]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class OnlineSpeakerClusters:
    """Pure online speaker clustering over unit-norm embeddings (see module docstring for the four
    defenses). Labels are stable 1-based ints for the lifetime of the instance (one per connection);
    merged labels disappear from the books but already-emitted labels on screen are not rewritten."""

    def __init__(self, hi=None, lo=None, max_speakers=None, prev_bonus=None,
                 topk=None, merge_every=None, merge_at=None):
        m = _active_model()
        self.hi = _env_float("LCC_SPK_THRESHOLD", m["hi"]) if hi is None else float(hi)
        self.lo = _env_float("LCC_SPK_THRESHOLD_LOW", m["lo"]) if lo is None else float(lo)
        self.lo = min(self.lo, self.hi - 0.01)
        self.max_speakers = SPK_MAX_SPEAKERS if max_speakers is None else int(max_speakers)
        self.prev_bonus = SPK_PREV_BONUS if prev_bonus is None else float(prev_bonus)
        self.topk = SPK_TOPK if topk is None else int(topk)
        self.merge_every = SPK_MERGE_EVERY if merge_every is None else int(merge_every)
        self.merge_at = SPK_MERGE_AT if merge_at is None else float(merge_at)
        self.speakers = {}            # label -> {"embs": [unit vecs], "centroid": unit vec}
        self.next_label = 1
        self.last_label = None        # previous unit's speaker -> turn-continuity bonus
        self.pending = None           # one unconfirmed unknown-voice embedding (two-strike rule)
        self.adds = 0

    def _centroid(self, embs):
        dim = len(embs[0])
        mean = [sum(e[i] for e in embs) / len(embs) for i in range(dim)]
        return _norm(mean) or list(embs[0])

    def _join(self, label, v):
        rec = self.speakers[label]
        rec["embs"].append(v)
        if len(rec["embs"]) > self.topk:
            rec["embs"] = rec["embs"][-self.topk:]
        rec["centroid"] = self._centroid(rec["embs"])

    def _new_speaker(self, embs):
        label = self.next_label
        self.next_label += 1
        self.speakers[label] = {"embs": list(embs)[-self.topk:], "centroid": self._centroid(list(embs))}
        return label

    def merge_overlapping(self):
        """Collapse clusters whose centroids converged above merge_at into the EARLIER label — heals a
        voice that got split while its clusters were still forming. Future clips get the merged label."""
        changed = True
        while changed:
            changed = False
            labels = sorted(self.speakers)
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    a, b = labels[i], labels[j]
                    if _dot(self.speakers[a]["centroid"], self.speakers[b]["centroid"]) >= self.merge_at:
                        merged = (self.speakers[a]["embs"] + self.speakers[b]["embs"])[-self.topk:]
                        self.speakers[a] = {"embs": merged, "centroid": self._centroid(merged)}
                        del self.speakers[b]
                        if self.last_label == b:
                            self.last_label = a
                        changed = True
                        break
                if changed:
                    break

    def add(self, vec):
        """Assign a (raw) embedding to a speaker label. Returns a 1-based int, or None when the clip is
        a bad vector / the first strike of an unknown voice / an unknown voice at capacity."""
        v = _norm(list(vec or ()))
        if v is None:
            return None
        self.adds += 1
        if self.adds % self.merge_every == 0:
            self.merge_overlapping()
        if not self.speakers:
            self.last_label = self._new_speaker([v])
            return self.last_label
        sims = {label: _dot(rec["centroid"], v) for label, rec in self.speakers.items()}
        eff = {label: s + (self.prev_bonus if label == self.last_label else 0.0)
               for label, s in sims.items()}
        best = max(eff, key=eff.get)
        if SPK_DEBUG:
            shown = ", ".join(f"{l}:{sims[l]:.2f}" for l in sorted(sims))
            print(f"[spk] sims=({shown}) prev={self.last_label} -> ", end="", flush=True)
        if eff[best] >= self.hi:
            self._join(best, v)
            self.pending = None
            self.last_label = best
            if SPK_DEBUG:
                print(f"join {best}", flush=True)
            return best
        if eff[best] >= self.lo:
            # Known-ish voice: label it but keep the centroid clean. An in-between clip is not evidence
            # of a NEW speaker either, so the pending buffer resets.
            self.pending = None
            self.last_label = best
            if SPK_DEBUG:
                print(f"assign {best}", flush=True)
            return best
        confirm = (self.hi + self.lo) / 2.0
        if (self.pending is not None and _dot(self.pending, v) >= confirm
                and len(self.speakers) < self.max_speakers):
            label = self._new_speaker([self.pending, v])
            self.pending = None
            self.last_label = label
            if SPK_DEBUG:
                print(f"new {label}", flush=True)
            return label
        self.pending = v               # first strike (or at capacity): hold the voice, tag nothing
        if SPK_DEBUG:
            print("pending", flush=True)
        return None


def model_present():
    p = _model_path()
    return os.path.isfile(p) and os.path.getsize(p) > 1_000_000


def _download_model():
    m = _active_model()
    path = _model_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    print(f"[diarize] downloading speaker model ({m['file']}) -> {path}", flush=True)
    urllib.request.urlretrieve(m["url"], tmp)             # nosec - fixed release asset URL
    os.replace(tmp, path)


def ensure_extractor():
    """Load (and if needed download) the speaker embedding extractor. Idempotent; raises on failure.
    Call on a CPU pool — the download can take a while on a slow link."""
    global _extractor
    with _extractor_lock:
        if _extractor is not None:
            return _extractor
        if not model_present():
            if SPK_MODEL_PATH:
                raise RuntimeError(f"LCC_SPK_MODEL not found: {SPK_MODEL_PATH}")
            _download_model()
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=_model_path(), num_threads=SPK_THREADS, provider="cpu")
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        print(f"[diarize] speaker embedding extractor ready ({_model_path()})", flush=True)
        return _extractor


def embed(pcm: bytes):
    """One speaker embedding for a 16k mono PCM16 clip, or None when the clip is too short or the
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
