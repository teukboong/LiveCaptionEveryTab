"""Characterization tests for the pure text/stream helpers in server.py.

These pin the CURRENT behavior of the helpers the unit-assembler and stream-gating rely on
(_append_text_dedupe, _next_sentence_cut, _weak_tail, _short_suffix_duplicate, _src_lang, _clean,
_stream_*). They are the regression guard for the upcoming Assembler/Scheduler extraction — written
test-first, before any of that code moves. No model is loaded (import only); run under the bridge venv:

    cd bridge && python test_text_helpers.py
"""
import server as s

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
