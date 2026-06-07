"""Characterization tests for the pure text/stream helpers in server.py.

These pin the CURRENT behavior of the helpers the unit-assembler and stream-gating rely on
(_append_text_dedupe, _next_sentence_cut, _weak_tail, _short_suffix_duplicate, _src_lang, _clean,
_stream_*). They are the regression guard for the upcoming Assembler/Scheduler extraction — written
test-first, before any of that code moves. No model is loaded (import only); run under the bridge venv:

    cd bridge && python test_text_helpers.py
"""
import test_import_stubs
test_import_stubs.install()

import server as s
from pathlib import Path
import json
import re
import types

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def ok(name, cond):
    if not cond:
        fails.append(f"{name}: condition failed")


# --- _append_text_dedupe: overlap-aware join ---
check("dedupe.empty_prev", s._append_text_dedupe("", "hello world"), "hello world")
check("dedupe.empty_new", s._append_text_dedupe("hello", ""), "hello")
check("dedupe.word_overlap", s._append_text_dedupe("the cat", "cat sat"), "the cat sat")
check("dedupe.new_in_prev", s._append_text_dedupe("hello world", "world"), "hello world")
check("dedupe.no_overlap", s._append_text_dedupe("a b c", "x y z"), "a b c x y z")
check("dedupe.tail_join", s._append_text_dedupe("good morning everyone", "everyone welcome back"),
      "good morning everyone welcome back")

# --- _next_sentence_cut: first COMPLETE sentence index, or -1 ---
ok("cut.too_short_frag", s._next_sentence_cut("Hi. Bye.") == -1)              # both fragments < MIN_SENT_CHARS
ok("cut.no_boundary", s._next_sentence_cut("this has no terminal punctuation yet") == -1)
_full = "This is a complete sentence. Next."
_cut = s._next_sentence_cut(_full)
_first = _full[:_cut].strip()
ok("cut.real_endswith", _first.endswith("."))
ok("cut.real_minlen", len(_first) >= s.MIN_SENT_CHARS)
ok("cut.real_content", "sentence" in _first and "Next" not in _first)
# decimals are NOT sentence boundaries
_full2 = "The value 3.14 is approximately pi here."
_first2 = _full2[: s._next_sentence_cut(_full2)].strip()
ok("cut.decimal_kept", "3.14" in _first2 and _first2.endswith("."))

# --- _weak_tail: a clause that shouldn't be force-committed yet ---
ok("weak.conj_and", s._weak_tail("I think and") is True)
ok("weak.aux_will", s._weak_tail("we will") is True)
ok("weak.trailing_comma", s._weak_tail("hello,") is True)
ok("weak.trailing_semicolon", s._weak_tail("wait;") is True)
ok("weak.strong_content", s._weak_tail("this is a full sentence") is False)
ok("weak.period_is_strong", s._weak_tail("done.") is False)

# --- _short_suffix_duplicate(new, prev): new is a short echo of prev's tail ---
ok("ssd.tail_echo", s._short_suffix_duplicate("world", "hello world") is True)
ok("ssd.prev_shorter", s._short_suffix_duplicate("the answer is", "x") is False)   # len(pw) !> len(nw)
ok("ssd.new_too_long", s._short_suffix_duplicate("a b c d e", "z a b c d e") is False)  # len(nw) > 4
ok("ssd.empty_new", s._short_suffix_duplicate("", "hello") is False)

# --- _src_lang: ratio-based, NOT any-hangul-means-Korean ---
check("src.english", s._src_lang("Hello world this is English"), "English")
check("src.korean", s._src_lang("안녕하세요 정말 반갑습니다 오늘은"), "Korean")
check("src.japanese", s._src_lang("これはテストですよろしく"), "Japanese")
check("src.en_with_kr_name", s._src_lang("I met 김수영 yesterday"), "English")   # documented edge
check("src.empty", s._src_lang(""), "English")

# --- target language wiring: popup options, protocol list, server allowlist, and prompts stay aligned ---
popup = Path(__file__).parents[1] / "extension" / "popup.html"
protocol = Path(__file__).parents[1] / "extension" / "protocol.js"
proto_targets = re.search(r"const LCC_TARGET_LANGS = Object\.freeze\(\[(.*?)\]\);", protocol.read_text(), re.S)
ok("target.protocol_list_found", proto_targets is not None)
proto_langs = re.findall(r'"([^"]+)"', proto_targets.group(1) if proto_targets else "")
check("target.protocol_has_hindi", "Hindi" in proto_langs, True)
check("target.server_has_hindi", "Hindi" in s._TARGET_LANGS, True)
check("target.lowercase_normalizes", s._normalize_target_lang("hindi"), "Hindi")
check("target.protocol_server_sync", sorted(proto_langs), sorted(s._TARGET_LANGS))
ok("target.popup_uses_protocol_source", '<select id="targetLang"></select>' in popup.read_text())
hindi_msgs = s._translate_messages("Hello everyone.", target="Hindi", register="casual")
ok("target.prompt_hindi", "Hindi" in hindi_msgs[0]["content"])
ok("target.prompt_not_korean_target", "Korean translation" not in hindi_msgs[0]["content"])
ok("target.context_signature_changes", s._translation_context_signature("Korean", "casual", "", [])
   != s._translation_context_signature("Hindi", "casual", "", []))
page_msgs = s._translate_messages("Share", target="Korean", register="casual", profile="page")
ok("page.prompt_dom_replacement", "direct DOM replacement" in page_msgs[0]["content"])
ok("page.prompt_not_live_speech", "live speech" not in page_msgs[0]["content"])
ok("page.fewshot_ui_label", {"role": "assistant", "content": "공유"} in page_msgs)
ok("page.fewshot_preserves_subreddit", {"role": "assistant", "content": "r/SipsTea"} in page_msgs)
caption_msgs = s._translate_messages("Share", target="Korean", register="casual", profile="caption")
ok("caption.prompt_live_speech", "live speech" in caption_msgs[0]["content"] or "live interpreter" in caption_msgs[0]["content"])
batch_msgs = s._translate_page_batch_messages([
    {"id": "a", "text": "Share"},
    {"id": "b", "text": "r/SipsTea"},
], target="Korean", hint="Reddit")
ok("page.batch_prompt_markers", "@@n@@" in batch_msgs[0]["content"])
ok("page.batch_prompt_dom", "DOM replacement" in batch_msgs[0]["content"])
ok("page.batch_input_marked", batch_msgs[-1]["content"].startswith("@@1@@"))
ok("page.batch_long_token_cap", s._page_batch_max_tokens([{"id": "long", "text": "x" * 1200}]) > 512)
check("page.batch_parse", s._parse_page_batch_result(
    "@@1@@\n공유\n\n@@2@@\nr/SipsTea",
    [{"id": "a", "text": "Share"}, {"id": "b", "text": "r/SipsTea"}],
), {"a": "공유", "b": "r/SipsTea"})
check("page.batch_parse_fenced", s._parse_page_batch_result(
    "```text\n@@1@@\n공유\n\n@@2@@\nr/SipsTea\n```",
    [{"id": "a", "text": "Share"}, {"id": "b", "text": "r/SipsTea"}],
), {"a": "공유", "b": "r/SipsTea"})
try:
    s._parse_page_batch_result("@@1@@\n공유", [{"id": "a", "text": "Share"}, {"id": "b", "text": "Log in"}])
    ok("page.batch_missing_segment_rejected", False)
except ValueError:
    ok("page.batch_missing_segment_rejected", True)

# --- page marker streaming: a segment emits once its NEXT marker appears; the last waits for the flush ---
seg_items = [{"id": "a", "text": "Share"}, {"id": "b", "text": "Log in"}, {"id": "c", "text": "Reply"}]
emitted_now, got = set(), []
s._emit_page_markers("@@1@@\n공유\n\n@@2@@\n로그", seg_items, emitted_now, lambda i, src, tgt: got.append((i, tgt)))
check("page.stream_first_complete", got, [("a", "공유")])      # segment 2 still growing -> held back
s._emit_page_markers("@@1@@\n공유\n\n@@2@@\n로그인\n\n@@3@@\n답글", seg_items, emitted_now, lambda i, src, tgt: got.append((i, tgt)))
check("page.stream_second_complete", got, [("a", "공유"), ("b", "로그인")])   # seg 1 not re-emitted; seg 3 still growing

# --- true partial streaming: the current segment streams before its next marker, half-written markers
# never leak into the speculative DOM text, and a final supersedes + clears the partial state ---
partial_emitted, partials, partial_state = set(), [], {}
s._emit_page_markers(
    "@@1@@\n공\n@@", seg_items, partial_emitted,
    lambda i, src, tgt: partials.append(("final", i, tgt)),
    lambda i, src, tgt: partials.append(("partial", i, tgt)),
    partial_state,
)
check("page.partial_current_segment", partials, [("partial", "a", "공")])   # half-marker "\n@@" stripped
s._emit_page_markers(
    "@@1@@\n공유\n\n@@2@@\n로", seg_items, partial_emitted,
    lambda i, src, tgt: partials.append(("final", i, tgt)),
    lambda i, src, tgt: partials.append(("partial", i, tgt)),
    partial_state,
)
ok("page.partial_final_supersedes", ("final", "a", "공유") in partials)
ok("page.partial_state_cleared_on_final", 1 not in partial_state)

# --- long paragraph: sentence-chunked, context-preserving (no model) ---
check("page.split_sentences", s._split_sentences("One. Two! Three?"), ["One.", "Two!", "Three?"])
_chunks = s._chunk_text(". ".join("X" * 80 for _ in range(8)) + ".", max_chars=200)
ok("page.chunk_multi", len(_chunks) >= 2)
ok("page.chunk_bounded", all(len(c) <= 400 for c in _chunks))         # never wildly over max
ok("page.chunk_lossless", sum(c.count("X") for c in _chunks) == 8 * 80)
_orig_tx = s.translate_once
_calls = []
def _fake_tx(text, recent_pairs=(), **kw):
    _calls.append((text, list(recent_pairs)))
    return "<" + text.strip()[:4] + ">"
try:
    s.translate_once = _fake_tx
    _para = ". ".join("Sentence number %d carrying enough words to be long" % i for i in range(20)) + "."
    _res = s.translate_page_long_once(_para, [("seedSrc", "씨앗")], target="Korean")
    ok("page.long_multichunk", len(_calls) >= 2)                       # genuinely chunked
    ok("page.long_join", isinstance(_res, str) and _res.count("<") == len(_calls))
    ok("page.long_seed_ctx", bool(_calls[0][1]) and _calls[0][1][0][0] == "seedSrc")  # seeded w/ recent_pairs
    ok("page.long_ctx_grows", len(_calls[-1][1]) > len(_calls[0][1]))  # later chunk sees prior chunks
    _short = s.translate_page_long_once("Just one short sentence.", [], target="Korean")
    ok("page.long_singlechunk", len(_calls) >= 3 and _short.startswith("<"))   # <=1 chunk -> one call
finally:
    s.translate_once = _orig_tx

# --- semantic block context: items carry an optional ctx (surrounding block) that rides into the prompt ---
_ctxitems = s._dom_translate_items({"items": [{"id": "x", "text": "fragment", "ctx": "A longer surrounding block of text here."}]})
ok("page.ctx_preserved", bool(_ctxitems) and _ctxitems[0].get("ctx") == "A longer surrounding block of text here.")
_ctxmsg = s._translate_page_batch_messages([{"id": "x", "text": "fragment", "ctx": "A longer surrounding block of text here."}], target="Korean")[-1]["content"]
ok("page.ctx_in_prompt", "reference only" in _ctxmsg and "surrounding page text" in _ctxmsg and "@@1@@" in _ctxmsg)
ok("page.ctx_skip_when_equal", "reference only" not in s._translate_page_batch_messages([{"id": "y", "text": "hello", "ctx": "hello"}], target="Korean")[-1]["content"])
ok("page.ctx_absent_no_preamble", s._translate_page_batch_messages([{"id": "z", "text": "hello"}], target="Korean")[-1]["content"].startswith("@@1@@"))

# --- DOM translation batch normalization: untrusted page items stay bounded before model use ---
dom_items = s._dom_translate_items({
    "items": [
        {"id": "a", "text": "  Hello page  "},
        {"id": "", "text": "skip"},
        {"id": "b" * 120, "text": "x" * 20},
        {"id": "c", "text": "y" * 20},
    ]
}, max_items=4, max_chars=8, max_total_chars=18)
check("dom.items_normalized", dom_items, [
    {"id": "a", "text": "Hello pa"},
    {"id": "b" * 80, "text": "xxxxxxxx"},
])
check("dom.non_list", s._dom_translate_items({"items": "nope"}), [])

# --- page DOM KV reuse: separate prefix cache, no caption-cache pollution ---
_orig = {
    "lm_tok": s.lm_tok,
    "lm_model": s.lm_model,
    "mx": s.mx,
    "lm_stream": s.lm_stream,
    "make_prompt_cache": s.make_prompt_cache,
    "trim_prompt_cache": s.trim_prompt_cache,
    "can_trim_prompt_cache": s.can_trim_prompt_cache,
    "_LM_IS_VLM": s._LM_IS_VLM,
    "_TX_KV_WINDOW": s._TX_KV_WINDOW,
    "_PAGE_TX_KVREUSE": s._PAGE_TX_KVREUSE,
    "_tx_cache": s._tx_cache,
    "_tx_cache_ids": s._tx_cache_ids,
    "_page_tx_cache": s._page_tx_cache,
    "_page_tx_cache_ids": s._page_tx_cache_ids,
}

class KVCache:
    def __init__(self):
        self.offset = 0

class _FakeMx:
    gpu = object()
    @staticmethod
    def set_default_device(_device):
        return None

class _FakeTok:
    def apply_chat_template(self, msgs, **_kwargs):
        return [ord(ch) for ch in json.dumps(msgs, ensure_ascii=False, sort_keys=True)]

feed_lens, prompt_lens = [], []

def _fake_make_prompt_cache(*_args, **_kwargs):
    return [KVCache()]

def _fake_trim_prompt_cache(cache, n):
    for layer in cache:
        layer.offset -= n
    return n

def _fake_lm_stream(_model, _tok, feed, max_tokens=None, sampler=None, prompt_cache=None):
    del max_tokens, sampler
    feed_lens.append(len(feed))
    if prompt_cache:
        for layer in prompt_cache:
            layer.offset += len(feed) + 1
    yield types.SimpleNamespace(text="@@1@@\n번역")

try:
    s._reset_page_tx_cache()
    s.lm_tok = _FakeTok()
    s.lm_model = object()
    s.mx = _FakeMx
    s.lm_stream = _fake_lm_stream
    s.make_prompt_cache = _fake_make_prompt_cache
    s.trim_prompt_cache = _fake_trim_prompt_cache
    s.can_trim_prompt_cache = lambda _cache: True
    s._LM_IS_VLM = False
    s._TX_KV_WINDOW = None
    s._PAGE_TX_KVREUSE = True
    s._tx_cache = object()
    tx_cache_sentinel = s._tx_cache

    for text in ("Share", "Log in"):
        msgs = s._translate_page_batch_messages([{"id": "a", "text": text}], [], "Korean", "Reddit", "casual", [])
        prompt_lens.append(len(s.lm_tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)))
        check("page.kv_result_" + text, s.translate_page_batch_once(
            [{"id": "a", "text": text}], [], "Korean", "Reddit", "casual", [], max_tokens=16,
        ), {"a": "번역"})

    check("page.kv_first_full_feed", feed_lens[0], prompt_lens[0])
    ok("page.kv_second_reuses_prefix", feed_lens[1] < prompt_lens[1])
    ok("page.kv_caption_cache_untouched", s._tx_cache is tx_cache_sentinel)
finally:
    s.lm_tok = _orig["lm_tok"]
    s.lm_model = _orig["lm_model"]
    s.mx = _orig["mx"]
    s.lm_stream = _orig["lm_stream"]
    s.make_prompt_cache = _orig["make_prompt_cache"]
    s.trim_prompt_cache = _orig["trim_prompt_cache"]
    s.can_trim_prompt_cache = _orig["can_trim_prompt_cache"]
    s._LM_IS_VLM = _orig["_LM_IS_VLM"]
    s._TX_KV_WINDOW = _orig["_TX_KV_WINDOW"]
    s._PAGE_TX_KVREUSE = _orig["_PAGE_TX_KVREUSE"]
    s._tx_cache = _orig["_tx_cache"]
    s._tx_cache_ids = _orig["_tx_cache_ids"]
    s._page_tx_cache = _orig["_page_tx_cache"]
    s._page_tx_cache_ids = _orig["_page_tx_cache_ids"]

# --- _clean: strip channel tags + whitespace ---
check("clean.trim", s._clean("  hi  "), "hi")
check("clean.plain", s._clean("plain text"), "plain text")
check("clean.channel", s._clean("a<|channel|>secret<|channel|>b"), "ab")

# --- _stream_visible_chars: non-whitespace count ---
check("vis.spaces", s._stream_visible_chars("a b c"), 3)
check("vis.padded", s._stream_visible_chars("  hello  world "), 10)
check("vis.empty", s._stream_visible_chars(""), 0)

# --- _stream_partial_substantial: substantial enough to show as a caption ---
ok("subst.empty", s._stream_partial_substantial("") is False)
ok("subst.early_fragment", s._stream_partial_substantial("오늘은") is False)        # short, no terminal punct
ok("subst.short_complete", s._stream_partial_substantial("네.") is True)            # short BUT complete
ok("subst.short_complete_kr", s._stream_partial_substantial("맞습니다.") is True)
ok("subst.two_words", s._stream_partial_substantial("this is enough") is True)
ok("subst.long_enough", s._stream_partial_substantial("12345678") is True)          # >= MIN_CHARS
ok("subst.trailing_comma", s._stream_partial_substantial("hi,") is False)

# --- _stream_partial_should_emit(text, last): worth pushing an updated partial ---
ok("emit.not_substantial", s._stream_partial_should_emit("오늘은", "") is False)
ok("emit.first", s._stream_partial_should_emit("네.", "") is True)
ok("emit.unchanged", s._stream_partial_should_emit("네.", "네.") is False)
ok("emit.more_words", s._stream_partial_should_emit("this is a much longer line now", "this is") is True)
ok("emit.delta_pass", s._stream_partial_should_emit("12345678", "1234") is True)    # +4 visible == DELTA
ok("emit.delta_gate", s._stream_partial_should_emit("12345678", "1234567") is False)  # +1 visible < DELTA

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_text_helpers: OK (all pure-helper characterization cases pass)")
