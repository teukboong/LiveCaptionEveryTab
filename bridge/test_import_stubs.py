"""Tiny import stubs for model-free server helper tests.

The pure helper tests import server.py but do not instantiate VAD or ASR models. A fresh
developer shell may not have the bridge venv installed, so provide just enough of silero_vad's
module shape to import server without weakening any helper assertions.
"""
import sys
import types


def install():
    if "silero_vad" in sys.modules:
        return
    mod = types.ModuleType("silero_vad")

    def load_silero_vad(*_args, **_kwargs):
        raise RuntimeError("silero_vad stub: model loading is outside model-free tests")

    class VADIterator:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("silero_vad stub: VADIterator is outside model-free tests")

    mod.load_silero_vad = load_silero_vad
    mod.VADIterator = VADIterator
    sys.modules["silero_vad"] = mod
