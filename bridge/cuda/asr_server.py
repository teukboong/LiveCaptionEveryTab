"""OpenAI-compatible /v1/audio/transcriptions serving the SAME ASR models as the MLX path — granite-speech-4.1
(영어) and Qwen3-ASR (다국어) — on CUDA via transformers. The bridge's CUDA backend posts model=granite|qwen3
and this routes to the matching model (lazy-loaded on first use, both can co-reside on a 24GB card: ~2GB each).
Mirrors server.transcribe_pcm: granite gets an explicit ASR instruction; qwen3 transcribes with no prompt.

  pip install -U qwen-asr "transformers>=4.57" torch torchaudio accelerate soundfile fastapi "uvicorn[standard]" python-multipart
  python asr_server.py            # http://0.0.0.0:8000/v1/audio/transcriptions

Env:
  LCC_CUDA_ASR_GRANITE_REPO   default ibm-granite/granite-speech-4.1-2b
  LCC_CUDA_ASR_QWEN3_REPO     default Qwen/Qwen3-ASR-1.7B
  LCC_ASR_DEVICE / LCC_ASR_DTYPE / LCC_ASR_HOST / LCC_ASR_PORT / LCC_ASR_MAX_TOKENS
  LCC_ASR_SINGLE_MODEL=1      unload the inactive ASR model before loading another engine; useful on 8GB GPUs
  LCC_GRANITE_PROMPT          granite ASR instruction (default same as the bridge)

Qwen3-ASR uses Qwen's official `qwen-asr` package. Granite still uses the transformers audio-chat path.
"""
import io
import os

import soundfile as sf
from fastapi import FastAPI, File, Form, UploadFile

SR = 16000
DEVICE = os.environ.get("LCC_ASR_DEVICE", "cuda")
DTYPE = os.environ.get("LCC_ASR_DTYPE", "auto")
MAX_TOKENS = int(os.environ.get("LCC_ASR_MAX_TOKENS", "200"))
MAX_BATCH = int(os.environ.get("LCC_QWEN3_ASR_MAX_BATCH", "1"))
SINGLE_MODEL = os.environ.get("LCC_ASR_SINGLE_MODEL", "0").strip().lower() in {"1", "true", "yes", "on"}
REPOS = {
    "granite": os.environ.get("LCC_CUDA_ASR_GRANITE_REPO", "ibm-granite/granite-speech-4.1-2b"),
    "qwen3": os.environ.get("LCC_CUDA_ASR_QWEN3_REPO", "Qwen/Qwen3-ASR-1.7B"),
}
GRANITE_PROMPT = os.environ.get(
    "LCC_GRANITE_PROMPT", "transcribe the speech with proper punctuation and capitalization.")

_loaded = {}   # engine -> (processor, model); lazy


def _torch():
    import torch
    return torch


def _torch_dtype(torch):
    if DTYPE != "auto":
        return getattr(torch, DTYPE)
    return torch.float16 if DEVICE.startswith("cuda") else torch.float32


def _device_map():
    return "cuda:0" if DEVICE == "cuda" else DEVICE


def _evict_inactive(engine):
    if not SINGLE_MODEL:
        return
    stale = [k for k in _loaded if k != engine]
    if not stale:
        return
    for key in stale:
        print(f"[asr] unloading inactive engine {key}", flush=True)
        del _loaded[key]
    import gc

    gc.collect()
    torch = _torch()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _load(engine):
    if engine in _loaded:
        return _loaded[engine]
    _evict_inactive(engine)
    import torch
    repo = REPOS[engine]
    print(f"[asr] loading {engine} = {repo} on {DEVICE} …", flush=True)
    if engine == "qwen3":
        from qwen_asr import Qwen3ASRModel

        model = Qwen3ASRModel.from_pretrained(
            repo,
            dtype=_torch_dtype(torch),
            device_map=_device_map(),
            max_inference_batch_size=MAX_BATCH,
            max_new_tokens=MAX_TOKENS,
        )
        _loaded[engine] = (None, model)
    else:
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        proc = AutoProcessor.from_pretrained(repo, trust_remote_code=True)
        dtype = ("auto" if DTYPE == "auto" else getattr(torch, DTYPE))
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            repo, torch_dtype=dtype, trust_remote_code=True, device_map=_device_map())
        model.eval()
        _loaded[engine] = (proc, model)
    print(f"[asr] {engine} ready", flush=True)
    return _loaded[engine]


def _transcribe(engine, audio):
    """audio: float32 mono @16k. Returns text."""
    torch = _torch()
    proc, model = _load(engine)
    if engine == "qwen3":
        results = model.transcribe(audio=(audio, SR), language=None)
        return (getattr(results[0], "text", "") if results else "").strip()

    target_device = next(model.parameters()).device
    wav = torch.as_tensor(audio, dtype=torch.float32).unsqueeze(0)
    prompt = proc.tokenizer.apply_chat_template(
        [{"role": "user", "content": f"<|audio|>{GRANITE_PROMPT}"}],
        tokenize=False,
        add_generation_prompt=True,
    )
    inputs = proc(prompt, wav, device=str(target_device), return_tensors="pt").to(target_device)
    ilen = inputs["input_ids"].shape[-1]
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=MAX_TOKENS, do_sample=False)
    return proc.tokenizer.batch_decode(out[:, ilen:], add_special_tokens=False, skip_special_tokens=True)[0].strip()


app = FastAPI()


@app.get("/")
def root():
    return {"ok": True, "engines": list(REPOS.keys()), "loaded": list(_loaded.keys())}


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(default="qwen3"),            # the bridge sends granite | qwen3
    response_format: str = Form(default="json"),   # noqa: ARG001
    temperature: float = Form(default=0.0),        # noqa: ARG001
):
    engine = model.strip().lower()
    if engine not in REPOS:
        engine = "qwen3"
    audio, sr = sf.read(io.BytesIO(await file.read()), dtype="float32")
    if getattr(audio, "ndim", 1) > 1:              # stereo → mono
        audio = audio.mean(axis=1)
    if sr != SR and getattr(audio, "size", 0) > 1:   # the bridge sends 16k, but be safe — linear resample (numpy is core; no heavy librosa dep)
        import numpy as np
        n = max(1, int(round(audio.shape[0] * SR / sr)))
        audio = np.interp(np.linspace(0, audio.shape[0] - 1, n),
                          np.arange(audio.shape[0]), audio).astype("float32")
    try:
        text = _transcribe(engine, audio)
    except Exception as e:
        print(f"[asr] {engine} error: {e}", flush=True)
        text = ""
    return {"text": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("LCC_ASR_HOST", "0.0.0.0"),
        port=int(os.environ.get("LCC_ASR_PORT", "8000")),
        log_level="warning",
    )
