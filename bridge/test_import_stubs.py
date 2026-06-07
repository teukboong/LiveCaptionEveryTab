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


def _expect_model_load_blocked(label, fn):
    try:
        fn()
    except RuntimeError as e:
        if "outside model-free tests" in str(e):
            return
        raise AssertionError(f"{label}: wrong error: {e}") from e
    raise AssertionError(f"{label}: model loading was not blocked")


if __name__ == "__main__":
    install()
    import silero_vad

    _expect_model_load_blocked("load_silero_vad", silero_vad.load_silero_vad)
    _expect_model_load_blocked("VADIterator", silero_vad.VADIterator)
    print("test_import_stubs: OK (silero_vad model loading is blocked in model-free tests)")
