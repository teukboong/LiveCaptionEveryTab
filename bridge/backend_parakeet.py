"""Optional sherpa-onnx Parakeet ASR backend for low-latency English transcription."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

SR = 16000


class ParakeetAsr:
    def __init__(self, model_dir: str, *, num_threads: int = 4, provider: str = "cpu"):
        try:
            import sherpa_onnx
        except Exception as e:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "sherpa_onnx is required for LCC_ASR_ENGINE=parakeet. "
                "Install pinned package: sherpa-onnx==1.13.2"
            ) from e

        root = Path(os.path.expanduser(model_dir)).resolve()
        required = {
            "encoder": root / "encoder.int8.onnx",
            "decoder": root / "decoder.int8.onnx",
            "joiner": root / "joiner.int8.onnx",
            "tokens": root / "tokens.txt",
        }
        missing = [str(path) for path in required.values() if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Parakeet model directory is missing required files: " + ", ".join(missing)
            )

        self.model_dir = str(root)
        self.provider = provider
        self.num_threads = num_threads
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(required["encoder"]),
            decoder=str(required["decoder"]),
            joiner=str(required["joiner"]),
            tokens=str(required["tokens"]),
            num_threads=num_threads,
            provider=provider,
            model_type="nemo_transducer",
            decoding_method="greedy_search",
        )

    def transcribe_pcm(self, pcm: bytes, hint: str = "") -> str | None:
        del hint  # Parakeet has no prompt-time vocabulary hint channel in this backend.
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if audio.size == 0:
            return None
        audio *= 1.0 / 32768.0
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SR, audio)
        self._recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
        return text or None
