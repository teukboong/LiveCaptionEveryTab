"""Model-free unit tests for the CUDA HTTP backend (no server, no GPU, no network).

Covers the pure pieces: WAV framing, multipart body, SSE delta parsing, stream coalescing, ASR
post-processing, per-engine ASR routing (granite=영어 / qwen3=다국어 — the SAME models as MLX), and PROMPT
PARITY (translate/ask post exactly what server.py builds). Run: python test_backend_cuda.py
"""
import io
import wave

import test_import_stubs
test_import_stubs.install()

import backend_cuda as bc

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


# --- _pcm_to_wav_bytes: a valid 16k mono int16 WAV ----------------------------------------------------
pcm = b"\x01\x02" * 16000           # 1s
wav = bc._pcm_to_wav_bytes(pcm)
check("wav_riff_header", wav[:4] == b"RIFF" and wav[8:12] == b"WAVE")
with wave.open(io.BytesIO(wav), "rb") as w:
    check("wav_mono", w.getnchannels() == 1)
    check("wav_16bit", w.getsampwidth() == 2)
    check("wav_16k", w.getframerate() == 16000)
    check("wav_frames", w.getnframes() == 16000)
    check("wav_data_roundtrips", w.readframes(16000) == pcm)


# --- _multipart_audio: well-formed body ---------------------------------------------------------------
body, ctype = bc._multipart_audio(wav, {"model": "granite", "response_format": "json"})
check("mp_content_type", ctype.startswith("multipart/form-data; boundary="))
boundary = ctype.split("boundary=")[1]
check("mp_has_model_field", b'name="model"' in body and b"granite" in body)
check("mp_has_file_field", b'name="file"; filename="audio.wav"' in body)
check("mp_has_wav_payload", wav in body)
check("mp_closing_boundary", body.rstrip().endswith(f"--{boundary}--".encode()))


# --- _iter_sse_deltas: OpenAI streaming shape, [DONE], message fallback, malformed skip ---------------
sse = [
    b'data: {"choices":[{"delta":{"role":"assistant"}}]}',          # no content -> skip
    b'data: {"choices":[{"delta":{"content":"\xec\x95\x88"}}]}',    # "안"
    b'',                                                            # blank -> skip
    b'data: {"choices":[{"delta":{"content":"\xeb\x85\x95"}}]}',    # "녕"
    b'garbage not sse',                                             # skip
    b'data: {bad json}',                                            # skip
    b'data: [DONE]',
    b'data: {"choices":[{"delta":{"content":"X"}}]}',               # after DONE -> ignored
]
check("sse_deltas_content", list(bc._iter_sse_deltas(sse)) == ["안", "녕"])
msg_only = [b'data: {"choices":[{"message":{"content":"full"}}]}', b'data: [DONE]']
check("sse_message_fallback", list(bc._iter_sse_deltas(msg_only)) == [("__final__", "full")])
# draft-streaming server (diffusion-gemma-http): deltas are a best-effort draft; the finish chunk's
# message.content is the authoritative text and REPLACES the accumulation instead of appending.
drafty = [
    b'data: {"choices":[{"delta":{"content":"draft "}}]}',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop","message":{"content":"final text"}}]}',
    b'data: [DONE]',
]
check("sse_final_replaces_draft",
      bc._collect_stream(bc._iter_sse_deltas(drafty), clean=lambda s: s) == "final text")


# --- _collect_stream: assembly + on_update cadence ----------------------------------------------------
seen = []
final = bc._collect_stream(iter(["a", "b", "c", "d", "e"]), on_update=lambda p: seen.append(p),
                           stream_every=2, clean=lambda s: s.strip())
check("collect_final", final == "abcde")
check("collect_update_cadence", seen == ["ab", "abcd"])   # fired at 2 and 4, not the trailing 5th
check("collect_no_update_when_none",
      bc._collect_stream(iter(["x", "y"]), on_update=None, stream_every=1, clean=lambda s: s) == "xy")


# --- _postprocess_asr: dedup + no-speech gate (granite/qwen3 emit "[no speech]" like the MLX path) ----
check("post_normal", bc._postprocess_asr("Hello world.") == "Hello world.")
check("post_empty_none", bc._postprocess_asr("   ") is None)
check("post_nospeech_none", bc._postprocess_asr("[no speech]") is None)
check("post_dedup_lines", bc._postprocess_asr("same\nsame\nother") == "same other")


# --- per-engine ASR routing: granite=영어 / qwen3=다국어, model field = engine name -------------------
import os as _os
g = bc._engine_cfg("granite")
q = bc._engine_cfg("qwen3")
check("engine_granite", g["engine"] == "granite" and g["model"] == "granite")
check("engine_qwen3", q["engine"] == "qwen3" and q["model"] == "qwen3")
check("engine_default_url_shared", g["url"] == bc.ASR_URL and q["url"] == bc.ASR_URL)
check("engine_unknown_falls_to_multilingual", bc._engine_cfg("nope")["engine"] == "qwen3")
check("engine_none_falls_to_multilingual", bc._engine_cfg(None)["engine"] == "qwen3")
check("engine_parakeet_falls_to_multilingual", bc._engine_cfg("parakeet")["engine"] == "qwen3")
# per-engine env override, read live (no reload) — repoint qwen3 at a separate server / model name
_os.environ["LCC_CUDA_ASR_QWEN3_URL"] = "http://127.0.0.1:8001/v1/audio/transcriptions"
_os.environ["LCC_CUDA_ASR_QWEN3_MODEL"] = "qwen3-asr-custom"
q2 = bc._engine_cfg("qwen3")
check("engine_override_url", q2["url"] == "http://127.0.0.1:8001/v1/audio/transcriptions")
check("engine_override_model", q2["model"] == "qwen3-asr-custom")
check("engine_granite_unaffected", bc._engine_cfg("granite")["url"] == bc.ASR_URL)
for _k in ("LCC_CUDA_ASR_QWEN3_URL", "LCC_CUDA_ASR_QWEN3_MODEL"):
    _os.environ.pop(_k, None)


# --- config defaults ----------------------------------------------------------------------------------
check("cfg_asr_is_transcriptions", bc.ASR_URL.endswith("/v1/audio/transcriptions"))
check("cfg_chat_is_chat", bc.CHAT_URL.endswith("/v1/chat/completions"))


# --- PROMPT PARITY: translate_once / run_ask post exactly what server.py builds -----------------------
import server as srv

captured = {}


def _fake_chat(messages, max_tokens, stream_every=4, on_update=None, extra_body=None, meta=None):
    captured["messages"] = messages
    captured["max_tokens"] = max_tokens
    if meta is not None:
        meta["truncated"] = False
    if "@@n@@" in messages[0]["content"]:
        return "@@1@@\n공유\n\n@@2@@\nr/SipsTea"
    if on_update is not None:
        on_update("부분")
    return "최종"


bc._chat = _fake_chat   # type: ignore

upd = []
out = bc.translate_once("Hello there, everyone.", [("prev en", "이전 한국어")], "Korean", "GPU", "lecture",
                        [("Blackwell", "블랙웰")], on_update=lambda p: upd.append(p), max_tokens=48, stream_every=2)
check("tx_returns_clean_final", out == "최종")
check("tx_on_update_called", upd == ["부분"])
check("tx_max_tokens_passthrough", captured["max_tokens"] == 48)
check("tx_prompt_parity",
      captured["messages"] == srv._translate_messages("Hello there, everyone.", [("prev en", "이전 한국어")],
                                                       "Korean", "GPU", "lecture", [("Blackwell", "블랙웰")]))
bc.translate_once("Share", [], "Korean", "Reddit page", "casual", [], max_tokens=24, profile="page")
check("tx_page_profile_prompt_parity",
      captured["messages"] == srv._translate_messages("Share", [], "Korean", "Reddit page", "casual", [], "page"))
page_batch = bc.translate_page_batch_once([
    {"id": "a", "text": "Share"},
    {"id": "b", "text": "r/SipsTea"},
], [], "Korean", "Reddit page", "casual", [], max_tokens=96)
check("tx_page_batch_returns_json_map", page_batch == {"a": "공유", "b": "r/SipsTea"})
check("tx_page_batch_max_tokens_passthrough", captured["max_tokens"] == 96)
check("tx_page_batch_prompt_parity",
      captured["messages"] == srv._translate_page_batch_messages([
          {"id": "a", "text": "Share"},
          {"id": "b", "text": "r/SipsTea"},
      ], [], "Korean", "Reddit page", "casual", []))

# custom translation prompt must thread through to the SAME shared builder as the MLX path (regression guard:
# the live loop passes custom as a trailing positional/keyword; a missing param TypeError'd every CUDA translation)
bc.translate_once("Hello", [], "Korean", "", "casual", [], max_tokens=24, profile="caption", custom="be terse")
check("tx_custom_prompt_parity",
      captured["messages"] == srv._translate_messages("Hello", [], "Korean", "", "casual", [], "caption", "be terse"))
bc.translate_page_batch_once([{"id": "a", "text": "Share"}, {"id": "b", "text": "r/SipsTea"}],
                             [], "Korean", "Reddit page", "casual", [], max_tokens=96, custom="keep slang")
check("tx_page_batch_custom_prompt_parity",
      captured["messages"] == srv._translate_page_batch_messages([
          {"id": "a", "text": "Share"},
          {"id": "b", "text": "r/SipsTea"},
      ], [], "Korean", "Reddit page", "casual", [], "keep slang"))

want_msgs, want_max = srv._ask_messages("qa", "the transcript", "what was said?", "Korean")
bc.run_ask("qa", "the transcript", "what was said?", "Korean")
check("ask_prompt_parity", captured["messages"] == want_msgs)
check("ask_max_tokens_parity", captured["max_tokens"] == want_max)


# --- report -------------------------------------------------------------------------------------------
if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_backend_cuda: OK (wav + multipart + sse + stream + asr-postproc + engine-routing + prompt-parity)")
