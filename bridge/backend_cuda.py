"""CUDA / NVIDIA backend for the live-caption bridge — an OpenAI-compatible HTTP client.

The bridge core (VAD, sentence assembly, scheduler, latency policy, number guard, prompt builders) is
platform-independent; this module supplies only the GPU leaves — transcribe / translate / ask — by calling
remote inference servers, so the SAME bridge runs on a Windows+NVIDIA box (servers in WSL2) instead of MLX.

Selected with ``LCC_BACKEND=cuda``. server.py rebinds its module globals to the functions here (its
"Backend seam"). Shared prompt builders (``_translate_messages`` / ``_ask_messages`` / ``_clean``) are
imported from server.py LAZILY (no import cycle) so both backends emit identical prompts.

Two endpoints, both OpenAI-compatible:
  - translate / ask : POST {CHAT_URL}  — llama.cpp llama-server or vLLM (chat.completions, streamed)
  - transcribe      : POST {ASR_URL}   — /v1/audio/transcriptions; the popup's engine (granite=영어 /
                      qwen3=다국어 / whisper=다국어) is sent as the ``model`` field. granite/qwen3 use the
                      transformers ASR server (cuda/asr_server.py); whisper uses whisper.cpp's whisper-server
                      (q6 gguf, cuda/serve_whisper.sh) on its own port — same OpenAI surface.

Stdlib only (urllib) so a fresh WSL2 env needs nothing beyond the bridge's own deps.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import wave

SR = 16000

# --- Endpoints / models (env-overridable) -------------------------------------------------------------
CHAT_URL = os.environ.get("LCC_CUDA_CHAT_URL", "http://127.0.0.1:8080/v1/chat/completions")
CHAT_MODEL = os.environ.get("LCC_CUDA_CHAT_MODEL", "local")   # llama.cpp ignores it; vLLM needs the served name

ASR_URL = os.environ.get("LCC_CUDA_ASR_URL", "http://127.0.0.1:8000/v1/audio/transcriptions")
ASR_MODEL = os.environ.get("LCC_CUDA_ASR_MODEL", "")          # global model override; "" = use the engine name
ASR_SWITCH_CMD = os.environ.get("LCC_CUDA_ASR_SWITCH_CMD", "").strip()

API_KEY = os.environ.get("LCC_CUDA_API_KEY", "").strip()       # optional Bearer token (vLLM --api-key)
ENABLE_THINKING = os.environ.get("LCC_CUDA_ENABLE_THINKING", "0") == "1"
TEMPERATURE = float(os.environ.get("LCC_CUDA_TEMPERATURE", "0.0"))
TIMEOUT = float(os.environ.get("LCC_CUDA_TIMEOUT", "60"))

TX_GEN_MAX = max(1, int(os.environ.get("LCC_TX_GEN_MAX_TOKENS", "64")))   # mirror server.py default


# --- Per-engine ASR routing (granite=영어 / qwen3=다국어 / whisper=다국어, same models as MLX) ----------
# The popup's 전사 엔진 choice is the ASR MODEL. On CUDA each engine maps to a (url, model). granite/qwen3
# hit the transformers ASR server (cuda/asr_server.py, port 8000) and select the model by name. WHISPER is
# served separately by whisper.cpp's whisper-server (q6 gguf, port 8002 by default) — a distinct binary, but
# the SAME OpenAI /v1/audio/transcriptions surface, so transcribe_pcm is unchanged. Whisper sends no prompt
# (own decode/langID). Override any engine's URL/MODEL to point it at a different server.
_ASR_ENGINES = ("granite", "qwen3", "whisper")
# whisper.cpp whisper-server default endpoint (distinct from the granite/qwen3 server on 8000).
WHISPER_URL = os.environ.get("LCC_CUDA_ASR_WHISPER_URL", "http://127.0.0.1:8002/v1/audio/transcriptions")


def _engine_cfg(engine):
    """Resolve an ASR engine name to its (url, model). Reads env at CALL time so a live popup engine switch
    takes effect on the next utterance. parakeet (MLX-only CPU engine) and unknowns → qwen3 (multilingual)."""
    engine = (engine or "").strip().lower()
    if engine not in _ASR_ENGINES:
        engine = "qwen3"
    up = engine.upper()
    default_url = WHISPER_URL if engine == "whisper" else ASR_URL
    return {
        "engine": engine,
        "url": os.environ.get(f"LCC_CUDA_ASR_{up}_URL") or default_url,
        "model": os.environ.get(f"LCC_CUDA_ASR_{up}_MODEL") or ASR_MODEL or ("local" if ASR_SWITCH_CMD else engine),
    }


# --- HTTP plumbing ------------------------------------------------------------------------------------
def _headers(content_type: str = "application/json") -> dict:
    h = {"Content-Type": content_type}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _iter_sse_deltas(lines):
    """Yield assistant content deltas from an OpenAI-style SSE stream (chat.completions, stream=True).
    ``lines`` is any iterable of bytes/str (one SSE line each). Pure — unit-tested without a live server."""
    for raw in lines:
        line = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        choices = obj.get("choices") or [{}]
        ch = choices[0] if choices else {}
        piece = (ch.get("delta") or {}).get("content")
        if piece:
            yield piece
        # message.content outside a delta is the AUTHORITATIVE full text: non-streaming servers echo it
        # as the only payload, and draft-streaming servers (diffusion-gemma-http) attach it to the finish
        # chunk because a denoise step may flip text after a draft delta already went out. Either way it
        # REPLACES the accumulation rather than appending to it.
        final = (ch.get("message") or {}).get("content")
        if final is not None:
            yield ("__final__", final)
        fin = ch.get("finish_reason")
        if fin:
            yield ("__finish__", fin)           # "length" = hit max_tokens -> output is cap-truncated


def _collect_stream(deltas, on_update=None, stream_every: int = 4, clean=None, meta=None) -> str:
    """Assemble streamed deltas, invoking on_update(partial) every ``stream_every`` pieces. Pure (clean is a
    fn) so the streaming/coalescing behaviour is testable offline."""
    clean = clean or (lambda s: (s or "").strip())
    out, since = [], 0
    for piece in deltas:
        if isinstance(piece, tuple) and piece:
            if piece[0] == "__final__":
                out = [piece[1] or ""]          # authoritative full text: replace, never append
            elif piece[0] == "__finish__" and meta is not None:
                meta["truncated"] = piece[1] == "length"
            continue
        out.append(piece)
        since += 1
        if on_update is not None and since >= max(1, stream_every):
            since = 0
            p = clean("".join(out))
            if p:
                on_update(p)
    return clean("".join(out))


def _chat(messages, max_tokens, stream_every: int = 4, on_update=None, extra_body=None, meta=None) -> str:
    """One streamed chat.completions call against CHAT_URL. Used for translate AND ask."""
    import server as _srv   # lazy: shared _clean; server is fully loaded by the time any request runs
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": TEMPERATURE,
        "stream": True,
    }
    if not ENABLE_THINKING:
        # uncensored/"harmony" finetunes default to a hidden <think> channel; suppress it so the caption isn't
        # the model's reasoning. llama.cpp and most vLLM chat templates honour chat_template_kwargs.
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if extra_body:
        payload.update(extra_body)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CHAT_URL, data=data, headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return _collect_stream(_iter_sse_deltas(resp), on_update, stream_every, _srv._clean, meta)


# --- Audio helpers ------------------------------------------------------------------------------------
def _pcm_to_wav_bytes(pcm, sr: int = SR) -> bytes:
    """Wrap raw PCM16 mono bytes in a WAV container (the transcription endpoint wants a file)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)        # int16
        w.setframerate(sr)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


def _multipart_audio(wav_bytes: bytes, fields: dict):
    """Build a multipart/form-data body (stdlib has no helper). Returns (body, content_type)."""
    boundary = "----lcc-cuda-boundary-Xk7Qe2Ab"
    parts = []
    for name, val in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode("utf-8"))
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
         f"Content-Type: audio/wav\r\n\r\n").encode("utf-8"))
    body = b"".join(parts) + bytes(wav_bytes) + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def _postprocess_asr(text):
    """Strip, collapse repeated lines, gate empties / "[no speech]" → None. Mirrors server.transcribe_pcm's
    cleanup (granite/qwen3 emit "[no speech]" on silence — same anti-hallucination gate as the MLX path)."""
    text = (text or "").strip()
    if not text:
        return None
    text = re.sub(r"^language\s+[^<\n]+<asr_text>\s*", "", text, flags=re.I).strip()
    text = re.sub(r"^<asr_text>\s*", "", text, flags=re.I).strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    dedup = []
    for l in lines:
        if not dedup or dedup[-1] != l:
            dedup.append(l)
    text = " ".join(dedup).strip()
    if not text or "[no speech]" in text.lower():
        return None
    return text


# --- Backend interface (the names server.py rebinds to) -----------------------------------------------
def translate_once(text, recent_pairs=(), target="Korean", hint="", register="casual",
                   glossary_pairs=(), on_update=None, kv_reuse=None, max_tokens=None, stream_every=None,
                   profile="caption", custom="", runtime=None, meta=None):
    """Stateless per-clause translation. Same prompt as the MLX path (shared _translate_messages), streamed
    so the live loop's on_update preview works identically. kv_reuse is ignored — the remote server manages
    its own prefix/KV caching (llama.cpp prompt cache, vLLM automatic prefix caching). custom mirrors the MLX
    signature so the live loop can pass the user's custom translation prompt on the CUDA path too (INV-11).
    runtime (the MLX aux-translator redirect) is accepted for signature parity and ignored — CUDA serves one
    GGUF, and the live loop never routes aux work here (aux_lm_ready() is False off MLX)."""
    del runtime
    import server as _srv
    msgs = _srv._translate_messages(text, recent_pairs, target, hint, register, glossary_pairs, profile, custom)
    gen_max = max(1, int(max_tokens or TX_GEN_MAX))
    return _chat(msgs, gen_max, int(stream_every or 4), on_update, meta=meta)


def translate_page_batch_once(items, recent_pairs=(), target="Korean", hint="", register="casual",
                              glossary_pairs=(), max_tokens=None, kv_reuse=None, on_segment=None, on_partial=None,
                              custom="", runtime=None):
    """DOM page microbatch translation. Same @@n@@-marker prompt/parser as the MLX path. kv_reuse is ignored
    (remote CUDA text servers handle prefix caching); runtime (MLX aux redirect) likewise — see translate_once.
    When on_segment is given, segments stream back via the chat stream's incremental marker parse just like
    the MLX path; on_partial streams the still-growing current segment as speculative UI."""
    del kv_reuse, runtime
    import server as _srv
    clean_items = [
        {"id": str(it.get("id", ""))[:80], "text": str(it.get("text", "")).strip(),
         "ctx": str(it.get("ctx", "")).strip()}
        for it in (items or [])
        if isinstance(it, dict) and str(it.get("id", "")).strip() and str(it.get("text", "")).strip()
    ]
    if not clean_items:
        return {}
    msgs = _srv._translate_page_batch_messages(clean_items, recent_pairs, target, hint, register, glossary_pairs, custom)
    emitted = set()
    partial_state = {}
    on_update = None
    if on_segment is not None:
        def on_update(partial):
            _srv._emit_page_markers(partial, clean_items, emitted, on_segment, on_partial, partial_state)
    raw = _chat(msgs, int(max_tokens or _srv._page_batch_max_tokens(clean_items)), 6, on_update)
    result = _srv._parse_page_batch_result(raw, clean_items)
    if on_segment is not None:
        for i, it in enumerate(clean_items):
            if (i + 1) not in emitted and str(it["id"]) in result:
                emitted.add(i + 1)
                on_segment(str(it["id"]), str(it["text"]), result[str(it["id"])])
    return result


def run_ask(mode, transcript_text, question="", target="Korean", on_partial=None):
    """On-demand summary / Q&A over the transcript — same messages as the MLX path, streamed."""
    import server as _srv
    msgs, max_toks = _srv._ask_messages(mode, transcript_text, question, target)
    return _chat(msgs, max_toks, 4, on_partial)


def bind_tx_only(warm_native):
    """Hybrid tx_http seam (popup tx_http model pick or LCC_TX_BACKEND=cuda): translation/ask go to
    CHAT_URL while ASR stays on the native MLX path. The external server is spawned (or adopted) HERE
    and dies with the bridge — model_runtime.ensure_diffusion_server owns the process exactly like the
    in-process models' lifetimes. The lm warm becomes an HTTP ping so the MLX 26B translator never
    loads. Returns (translate_once, translate_page_batch_once, run_ask, warm) for server.py to rebind."""
    import model_runtime
    model_runtime.ensure_diffusion_server()
    global CHAT_URL
    if not os.environ.get("LCC_CUDA_CHAT_URL"):
        # popup flow sets only LCC_LM_MODEL: aim the chat client at the diffusion server we just
        # started (its port), not the full-cuda default (8080)
        CHAT_URL = model_runtime._dg_base_url() + "/v1/chat/completions"
    def warm(asr=False, lm=False, asr_engine=None):
        if asr:
            warm_native(asr=True, lm=False, asr_engine=asr_engine)
        if lm:
            warm_selected(asr=False, lm=True)

    def tx(text, recent_pairs=(), target="Korean", hint="", register="casual", glossary_pairs=(),
           on_update=None, kv_reuse=None, max_tokens=None, stream_every=None, profile="caption",
           custom="", runtime=None, meta=None):
        # the diffusion server streams stable WORD-PREFIX deltas (a handful per line), not tokens —
        # the MLX-tuned cadence (every 4 deltas) starves the overlay to 0-1 paints per caption and the
        # screen looks dead while a 2-5s final denoises. Paint every delta instead.
        return translate_once(text, recent_pairs, target, hint, register, glossary_pairs,
                              on_update, kv_reuse, max_tokens, 1, profile, custom, runtime, meta)

    return tx, translate_page_batch_once, run_ask, warm


def transcribe_pcm(pcm, hint="", asr_engine=None):
    """Transcribe one PCM16 mono 16k segment with the selected ASR model (granite=영어 / qwen3=다국어), the
    SAME models as the MLX path, served on CUDA. The popup engine choice routes via _engine_cfg."""
    del hint   # no per-call biasing channel over the OpenAI API; glossary still biases the 26B translation
    if not pcm or len(pcm) < 2:
        return None
    cfg = _engine_cfg(asr_engine)
    wav = _pcm_to_wav_bytes(pcm)
    fields = {"model": cfg["model"], "response_format": "json", "temperature": "0"}
    prompt = os.environ.get(f"LCC_CUDA_ASR_{cfg['engine'].upper()}_PROMPT", "")
    if not prompt and cfg["engine"] == "granite":
        prompt = os.environ.get(
            "LCC_CUDA_ASR_PROMPT",
            "transcribe the speech with proper punctuation and capitalization.",
        )
    if prompt:
        fields["prompt"] = prompt
    body, ctype = _multipart_audio(wav, fields)
    req = urllib.request.Request(cfg["url"], data=body, headers=_headers(ctype), method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        obj = json.loads(resp.read().decode("utf-8", "ignore"))
    return _postprocess_asr(obj.get("text", "") if isinstance(obj, dict) else "")


def ensure_asr_loaded(engine=None):
    """Engine switch from the popup. If LCC_CUDA_ASR_SWITCH_CMD is set, run it with the selected engine so a
    small-VRAM box can keep only one ASR server/model resident at a time."""
    cfg = _engine_cfg(engine)
    if ASR_SWITCH_CMD:
        env = os.environ.copy()
        env["LCC_CUDA_ASR_ENGINE"] = cfg["engine"]
        print(f"[bridge] cuda asr switch: {ASR_SWITCH_CMD} {cfg['engine']}", flush=True)
        subprocess.run([ASR_SWITCH_CMD, cfg["engine"]], env=env, check=True, timeout=TIMEOUT)
    print(f"[bridge] cuda asr engine={cfg['engine']} -> {cfg['url']} model={cfg['model']}", flush=True)
    return engine


def warm_selected(asr=False, lm=False, asr_engine=None):
    """Best-effort endpoint warm-up (compiles graphs / loads weights server-side on first hit). Never raises."""
    if lm:
        try:
            _chat([{"role": "user", "content": "hello"}], 8, 9999, None)
        except Exception as e:
            print(f"[warm] cuda chat: {e}", flush=True)
    if asr:
        try:
            transcribe_pcm(b"\x00\x00" * SR, asr_engine=asr_engine)   # 1s int16 silence on the selected engine
        except Exception as e:
            print(f"[warm] cuda asr: {e}", flush=True)


def _base_url(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url


def _ping(url: str, label: str):
    base = _base_url(url)
    try:
        with urllib.request.urlopen(base, timeout=3) as resp:
            print(f"[bridge] cuda {label} endpoint reachable: {base} (HTTP {getattr(resp, 'status', '?')})", flush=True)
    except urllib.error.HTTPError as e:
        # An HTTP error still means the server answered (e.g. 404 on the bare root) -> it's up.
        print(f"[bridge] cuda {label} endpoint reachable: {base} (HTTP {e.code})", flush=True)
    except Exception as e:
        print(f"[bridge] cuda {label} endpoint NOT reachable: {base} ({e}) — start the server first", flush=True)


def load(asr=True, lm=True):
    """Best-effort reachability check at startup. Does NOT hard-fail (servers may still be warming); the first
    real request surfaces any error and the live loop degrades gracefully."""
    if lm:
        _ping(CHAT_URL, "chat/translate")
    if asr:
        for url in sorted({_engine_cfg(e)["url"] for e in _ASR_ENGINES}):   # distinct ASR endpoints only
            _ping(url, "asr")
