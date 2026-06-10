import os

from page_markers import _page_block_context_preamble, _page_marker_input
from text_helpers import _src_lang


# Gemma 4 is broadly multilingual (140+ langs); expose a generous set of widely-used targets. Any name here is
# inserted into the prompt as "into {target}" — few-shot/register anchors exist only for a few (graceful: others
# translate fine without anchors). Keep in sync with the popup targetLang <select>.
_TARGET_LANGS = {
    "Korean", "English", "Japanese", "Chinese", "Spanish", "French", "German", "Portuguese", "Italian",
    "Russian", "Dutch", "Polish", "Turkish", "Vietnamese", "Thai", "Indonesian", "Arabic", "Hindi",
    "Bengali", "Ukrainian", "Czech", "Greek", "Hebrew", "Romanian", "Hungarian", "Swedish", "Danish",
    "Norwegian", "Finnish", "Filipino", "Malay", "Tamil", "Telugu", "Urdu", "Persian", "Swahili",
    "Catalan", "Croatian", "Slovak", "Bulgarian", "Serbian", "Lithuanian", "Slovenian", "Estonian", "Latvian",
}

def _normalize_target_lang(value, default="Korean"):
    raw = str(value or default or "Korean").strip()
    for lang in _TARGET_LANGS:
        if raw.lower() == lang.lower():
            return lang
    return default if default in _TARGET_LANGS else "Korean"

def _translation_context_signature(target, register, hint, glossary_pairs, custom=""):
    # custom is part of the signature (INV-9): change the custom prompt -> cache/epoch invalidates, so a
    # prompt edit never leaves stale translations rendering. Trailing default keeps it backward-compatible.
    return (
        _normalize_target_lang(target),
        str(register or "casual"),
        str(hint or ""),
        tuple(glossary_pairs or ()),
        str(custom or ""),
    )

TX_PROFILE = os.environ.get("LCC_TX_PROFILE", "quality").strip().lower()
TX_FEWSHOT_MAX = max(0, int(os.environ.get("LCC_TX_FEWSHOT_MAX", "0" if TX_PROFILE in ("fast", "compact", "latency") else "3")))
PAGE_TX_FEWSHOT_MAX = max(0, int(os.environ.get("LCC_PAGE_TX_FEWSHOT_MAX", "8")))
TX_COMPACT_PROMPT = TX_PROFILE in ("fast", "compact", "latency")

# Register-aware, source-language-aware style anchors (few-shot). Content type changes the right
# tone (a gaming stream vs a conference talk vs a newscast), so each register carries its own anchors;
# and EN->KO vs JA->KO want different example sources, so anchors are keyed by detected source language
# too. Kept to 2-3 lines each so per-call prefill stays cheap. Falls back: register->casual, src->English.
_TX_FEWSHOT = {
    "Korean": {
        "casual": {   # gaming / streaming — 캐주얼 방송 진행자 톤, 해요체
            "English": [
                ("Hey everyone, welcome back to the stream.", "여러분 안녕하세요, 다시 방송으로 돌아왔습니다."),
                ("So basically the whole thing crashed right in the middle of the demo.", "그러니까 결국, 시연 도중에 전체가 그냥 다 뻗어버린 거예요."),
                ("Okay the patch just dropped and they completely reworked ranked.", "자, 방금 패치 떴는데 랭크를 완전히 갈아엎었어요."),
            ],
            "Japanese": [
                ("じゃあ次のステージ行ってみましょうか。", "자, 그럼 다음 스테이지 가볼까요."),
                ("いや今のはマジで運が良かったですね。", "와, 방금 건 진짜 운이 좋았네요."),
            ],
        },
        "lecture": {   # talks / conferences — 정중한 합니다체, 기술용어 정확
            "English": [
                ("Today we're announcing our next-generation GPU architecture.", "오늘 저희는 차세대 GPU 아키텍처를 발표합니다."),
                ("Let me walk you through how the training pipeline actually works.", "학습 파이프라인이 실제로 어떻게 동작하는지 차근차근 설명드리겠습니다."),
                ("This delivers roughly five times the throughput of the previous generation.", "이는 이전 세대 대비 약 5배의 처리량을 제공합니다."),
            ],
            "Japanese": [
                ("本日は新しいアーキテクチャについてご紹介します。", "오늘은 새로운 아키텍처에 대해 소개해 드리겠습니다."),
                ("ここで実際のベンチマーク結果をご覧ください。", "여기서 실제 벤치마크 결과를 보시겠습니다."),
            ],
        },
        "news": {      # news / interview — 중립 보도체
            "English": [
                ("Officials say the new policy will take effect next month.", "당국은 새 정책이 다음 달부터 시행된다고 밝혔습니다."),
                ("The company reported record quarterly earnings on Thursday.", "이 회사는 목요일 분기 사상 최대 실적을 발표했습니다."),
                ("Critics argue the measure does not go far enough.", "비판론자들은 이 조치가 충분하지 않다고 지적합니다."),
            ],
            "Japanese": [
                ("政府は来週、追加の対策を発表する見通しです。", "정부는 다음 주 추가 대책을 발표할 전망입니다."),
                ("専門家はこの傾向が続くと指摘しています。", "전문가들은 이런 추세가 이어질 것이라고 지적합니다."),
            ],
        },
        "chat": {      # casual chat / podcast — 친근한 대화체
            "English": [
                ("Honestly I didn't even think it would work at first.", "솔직히 처음엔 이게 될 거라고 생각도 안 했어요."),
                ("Wait, are you serious right now? That's insane.", "잠깐, 지금 진심이에요? 완전 말도 안 되는데."),
                ("Yeah so we ended up just talking about it for like two hours.", "네, 그래서 결국 그거 가지고 두 시간을 떠들었어요."),
            ],
            "Japanese": [
                ("いやー、それめっちゃ分かるわ。", "아 그거 완전 이해돼요."),
                ("でさ、結局どうなったの?", "그래서, 결국 어떻게 됐어요?"),
            ],
        },
    },
    "Japanese": {
        "casual": {
            "English": [
                ("Hey everyone, welcome back to the stream.", "皆さんこんにちは、配信に戻ってきました。"),
                ("So basically the whole thing crashed right in the middle of the demo.", "それで結局、デモの途中で全部落ちちゃったんですよ。"),
                ("That's a great question — let me actually break it down for you.", "すごくいい質問ですね。ちょっと順を追って説明しますね。"),
            ],
        },
        "lecture": {
            "English": [
                ("Today we're announcing our next-generation GPU architecture.", "本日、次世代のGPUアーキテクチャを発表いたします。"),
                ("This delivers roughly five times the throughput of the previous generation.", "これは前世代の約5倍のスループットを実現します。"),
            ],
        },
        "news": {
            "English": [
                ("Officials say the new policy will take effect next month.", "当局は、新たな政策が来月から施行されると発表しました。"),
                ("The company reported record quarterly earnings on Thursday.", "同社は木曜日、四半期として過去最高の業績を発表しました。"),
            ],
        },
    },
}

_PAGE_TX_FEWSHOT = {
    "Korean": {
        "English": [
            ("Share", "공유"),
            ("Log in", "로그인"),
            ("View more comments", "댓글 더 보기"),
            ("11 hours ago", "11시간 전"),
            ("r/SipsTea", "r/SipsTea"),
            ("Infamous_Question430", "Infamous_Question430"),
            ("People taking zero accountability is an epidemic these days.", "요즘은 책임을 전혀 지지 않는 사람이 너무 많다."),
            ("Woman saves her dogs from another dog in the street", "길거리에서 다른 개로부터 자기 강아지들을 구해낸 여자"),
        ],
        "Japanese": [
            ("コメントをもっと見る", "댓글 더 보기"),
            ("シェア", "공유"),
        ],
    },
    "Japanese": {
        "English": [
            ("Share", "共有"),
            ("Log in", "ログイン"),
            ("View more comments", "コメントをさらに表示"),
            ("11 hours ago", "11時間前"),
            ("r/SipsTea", "r/SipsTea"),
            ("Infamous_Question430", "Infamous_Question430"),
        ],
    },
    "English": {
        "Korean": [
            ("공유", "Share"),
            ("로그인", "Log in"),
            ("댓글 더 보기", "View more comments"),
            ("11시간 전", "11 hours ago"),
        ],
        "Japanese": [
            ("共有", "Share"),
            ("ログイン", "Log in"),
            ("コメントをさらに表示", "View more comments"),
        ],
    },
}

# Per-register tone instruction, appended to the system prompt (target-specific).
_REGISTER_TONE = {
    "Korean": {
        "casual":  "캐주얼한 방송 진행자 말투로, 화자 톤에 맞춰 존댓말(해요체)을 기본으로 자연스럽게 옮겨라. 번역투·영어 어순·직역을 피하고 자연스러운 한국어 종결어미를 써라. ",
        "lecture": "발표·강연 상황의 정중한 존댓말(합니다체)로 옮겨라. 기술용어·고유명사는 정확히, 매끄럽고 명료한 문장으로. 번역투·영어 어순을 피해라. ",
        "news":    "중립적이고 정제된 보도체 존댓말로 옮겨라. 군더더기 없이 사실 위주로, 자연스러운 한국어 보도 문장으로. ",
        "chat":    "친구끼리 편하게 대화하듯 자연스러운 구어체로 옮겨라. 화자 톤에 맞춰 해요체/반말이 섞여도 좋다. 번역투를 피해라. ",
    },
    "Japanese": {
        "casual":  "配信者の自然な口調で、敬体を基本に訳すこと。翻訳調や英語の語順を避け、自然な終助詞で。 ",
        "lecture": "講演・発表の丁寧な敬体（です・ます）で訳すこと。専門用語・固有名詞は正確に、明瞭で滑らかな文に。 ",
        "news":    "中立的で整った報道体で訳すこと。余計な要素を省き、事実中心に自然な日本語で。 ",
        "chat":    "親しい会話のような自然な口語で訳すこと。話者のトーンに合わせて。 ",
    },
}
_REGISTERS = ("casual", "lecture", "news", "chat")

def _fewshot(target: str, register: str, src_lang: str, profile: str = "caption"):
    if profile == "write":
        return []                # write-back: the caption anchors point the wrong direction (they're X->target_caption)
    if profile == "page":
        by_src = _PAGE_TX_FEWSHOT.get(target, {})
        return by_src.get(src_lang) or by_src.get("English") or []
    by_reg = _TX_FEWSHOT.get(target, {})
    by_src = by_reg.get(register) or by_reg.get("casual") or {}
    return by_src.get(src_lang) or by_src.get("English") or []


def _parse_glossary(raw: str):
    """Parse a user glossary into (source_term, target_rendering) pairs. Accepts 'Blackwell=블랙웰',
    'Blackwell→블랙웰', or a bare 'Blackwell' (term-only: ASR biasing + 'keep consistent')."""
    pairs = []
    for line in (raw or "").splitlines():
        line = line.strip()[:160]
        if not line:
            continue
        sep = "=" if "=" in line else ("→" if "→" in line else None)
        if sep:
            a, b = line.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a:
                pairs.append((a, b))
        else:
            pairs.append((line, ""))
        if len(pairs) >= 40:
            break
    return pairs


def _glossary_clause(pairs) -> str:
    rules = [f"'{s}'→'{t}'" for s, t in pairs if t]
    terms = [s for s, t in pairs if not t]
    out = ""
    if rules:
        out += "Always translate these terms exactly as given: " + "; ".join(rules) + ". "
    if terms:
        out += "Keep these names/terms consistent: " + ", ".join(terms) + ". "
    return out


_FAST_REGISTER_TONE = {
    "casual": "Casual broadcast tone. ",
    "lecture": "Clear lecture/presentation tone. ",
    "news": "Concise news/interview tone. ",
    "chat": "Natural conversation tone. ",
}


def _page_tx_system(target: str, hint: str = "", glossary_pairs=(), custom: str = "") -> str:
    # DOM-preservation structure is ALWAYS kept (INV-10): a custom prompt layers translation STYLE on top
    # of the mandatory structural rules (same-node replacement, handles/URLs/code unchanged, output-only) —
    # it never removes them. So for page, custom augments; the structural guard prose stays verbatim.
    custom = (custom or "").strip()
    if TX_COMPACT_PROMPT:
        s = (f"Translate visible web page text into concise {target}. Replace the same DOM node only. "
             "Keep handles, subreddit names, URLs, code, numbers, timestamps, emoji, and already-target-language text unchanged. "
             "Use label-like wording for UI text. ")
        if custom:
            s += f"Follow these translation instructions: {custom}. "
        s += _glossary_clause(glossary_pairs)
        if hint:
            s += f"Page context / terms: {hint}. "
        return s + f"Output only the replacement text in {target}, or the unchanged source."
    s = (f"You translate visible web page text into {target} for direct DOM replacement. Each user message is the "
         "complete text of one page node or short UI fragment. Output exactly the replacement text for that same "
         "node, with no explanations, prefixes, quotes, markdown, or extra alternatives. Preserve formatting intent, "
         "line breaks when useful, numbers, timestamps, currencies, emoji, handles, subreddit/community names, URLs, "
         "code, IDs, product names, and proper nouns. If the text is already in {target}, a username/handle, a "
         "subreddit/community name, code, a URL, or not meaningful to translate, return it unchanged. For buttons, "
         "menus, labels, counts, and navigation text, use short native UI wording instead of conversational sentences. "
         "Do not add politeness, commentary, inferred context, or sentence endings that are not present in the source. ")
    if custom:
        s += f"Follow these translation instructions: {custom}. "
    s += _glossary_clause(glossary_pairs)
    if hint:
        s += f"Use this page context only to disambiguate names/terms: {hint}. "
    return s + f"Output ONLY the {target} replacement text, nothing else."


def _write_tx_system(target: str, glossary_pairs=()) -> str:
    # Write-back (입력창 역번역): the user composed a draft in THEIR language and wants it posted in the
    # page's language. This is authoring, not captioning — render the message as the user would have
    # written it natively, never as a visibly-translated text.
    s = (f"Rewrite the user's draft message in natural, native {target}, exactly as they would have "
         f"written it in {target} themselves. Keep the meaning, tone, politeness level, line breaks, "
         "emoji, @mentions, #tags, URLs, and code unchanged. Do not add greetings, sign-offs, "
         "explanations, or anything not in the draft. ")
    s += _glossary_clause(glossary_pairs)
    return s + f"Output ONLY the {target} text, nothing else."


def _tx_system(target: str, register: str = "casual", hint: str = "", glossary_pairs=(),
               profile: str = "caption", custom: str = "") -> str:
    # custom (when set) REPLACES the descriptive instruction + register tone (per "서술부만 교체"), but the
    # structural guards stay: glossary clause, hint clause, and the final "Output ONLY the translation" guard
    # (INV-10). Empty custom => byte-identical to the previous prompt (INV-11 / backward-compat).
    if profile == "page":
        return _page_tx_system(target, hint, glossary_pairs, custom)
    if profile == "write":
        return _write_tx_system(target, glossary_pairs)
    custom = (custom or "").strip()
    if TX_COMPACT_PROMPT:
        if custom:
            s = custom + " "
        else:
            s = (f"Translate live speech into natural {target}. Preserve meaning, tone, and names. "
                 f"If the line is incomplete, translate only what is present. ")
            s += _FAST_REGISTER_TONE.get(register, "")
        s += _glossary_clause(glossary_pairs)
        if hint:
            s += f"Consistent names/terms: {hint}. "
        return s + f"Output only {target}."
    if custom:
        s = custom + " "
    else:
        s = (f"You are an expert live interpreter turning a continuous talk/stream into natural, fluent {target}. "
             f"Translate the user's line by MEANING into idiomatic {target} that a native speaker would actually "
             f"say — never word-for-word, never transliterate, no translationese or foreign word order. Match the "
             f"speaker's tone and register, and keep names/terms consistent with the running conversation above. "
             f"The line may be cut off mid-sentence; translate what is there naturally without inventing the rest. ")
        s += _REGISTER_TONE.get(target, {}).get(register, "")
    s += _glossary_clause(glossary_pairs)
    if hint:
        s += f"Render these names/terms consistently: {hint}. "
    return s + f"Output ONLY the {target} translation, nothing else."


def _translate_messages(text, recent_pairs=(), target="Korean", hint="", register="casual", glossary_pairs=(),
                        profile: str = "caption", custom: str = ""):
    """The chat-message list for one clause translation: register-aware system instruction + source-language-
    matched few-shot anchors + the model's recent (source->target) renderings (consistency) + the line itself.
    Shared by the MLX and CUDA backends so both produce byte-identical prompts (same translation regardless of
    runtime — custom is threaded HERE, in the shared builder, not per-backend; INV-11). Each backend applies
    its own chat template (MLX: apply_chat_template; CUDA: server-side)."""
    msgs = [{"role": "system", "content": _tx_system(target, register, hint, glossary_pairs, profile, custom)}]
    fewshot_max = PAGE_TX_FEWSHOT_MAX if profile == "page" else TX_FEWSHOT_MAX
    for ex_src, ex_tgt in _fewshot(target, register, _src_lang(text), profile)[:fewshot_max]:   # source-lang-matched style anchors
        msgs += [{"role": "user", "content": ex_src}, {"role": "assistant", "content": ex_tgt}]
    for s, t in recent_pairs:                                   # the model's own recent renderings -> consistency
        msgs += [{"role": "user", "content": s}, {"role": "assistant", "content": t}]
    msgs.append({"role": "user", "content": text})
    return msgs

def _page_marker_system(target: str, hint: str = "", glossary_pairs=(), recent_pairs=(), custom: str = "") -> str:
    s = (
        f"Translate visible web-page text into {target} for direct DOM replacement. The input has numbered "
        "segments — each a marker like @@1@@ on its own line followed by that segment's text. Output the SAME "
        "@@n@@ markers in the SAME order, each on its own line, immediately followed by ONLY that segment's "
        f"{target} replacement text. Keep every @@n@@ marker exactly; translate every segment; never merge, "
        "drop, reorder, or add segments. Preserve handles, subreddit/community names, URLs, code, IDs, numbers, "
        "timestamps, emoji, product names, and already-target-language text unchanged. Use short native wording "
        "for UI labels. Output only the @@n@@ markers and their translations — no JSON, markdown, comments, "
        "quotes, or explanations. Some segments contain inline placeholders like ⟦1⟧ that mark where a "
        "link or fixed element sits — echo every ⟦n⟧ placeholder verbatim, in ascending order, exactly "
        "once each, and translate the text around them naturally; never translate, drop, reorder, or duplicate a "
        "placeholder. "
    )
    if (custom or "").strip():   # custom layers STYLE on top; the @@n@@/placeholder structure above stays (INV-10)
        s += f"Follow these translation instructions: {custom.strip()}. "
    s += _glossary_clause(glossary_pairs)
    if hint:
        s += f"Page context/terms: {hint}. "
    if recent_pairs:
        recent = "; ".join(f"'{src}'->'{tgt}'" for src, tgt in list(recent_pairs)[-4:])
        s += "Recent page renderings for consistency only: " + recent + ". "
    return s

def _translate_page_batch_messages(items, recent_pairs=(), target="Korean", hint="", register="casual",
                                   glossary_pairs=(), custom: str = ""):
    """Prompt for page DOM microbatch translation. Output is @@n@@-marked segments so the content script can
    map every replacement back to its text node and the bridge can stream segments as they complete; a missing
    marker falls back to per-item translation. A marker-free block-context preamble (when items carry `ctx`)
    gives the model the surrounding prose for fragments split by inline elements."""
    msgs = [{"role": "system", "content": _page_marker_system(target, hint, glossary_pairs, recent_pairs, custom)}]
    src_lang = _src_lang(" ".join(str(it.get("text", "")) for it in items))
    few = _fewshot(target, register, src_lang, "page")[:min(PAGE_TX_FEWSHOT_MAX, 4)]
    if few:
        ex_in = _page_marker_input([{"text": src} for src, _ in few])
        ex_out = "\n\n".join(f"@@{i + 1}@@\n{tgt}" for i, (_, tgt) in enumerate(few))
        msgs += [{"role": "user", "content": ex_in}, {"role": "assistant", "content": ex_out}]
    msgs.append({"role": "user", "content": _page_block_context_preamble(items) + _page_marker_input(items)})
    return msgs

def _ask_messages(mode: str, transcript_text: str, question: str = "", target: str = "Korean"):
    """(messages, max_tokens) for an on-demand summary / Q&A over the running transcript. Shared by both
    backends so the summary/answer is identical across runtimes."""
    if mode == "qa" and question.strip():
        sysmsg = ("You answer a viewer's question about a live talk/stream using ONLY the transcript below. "
                  f"Answer in {target}, concise and concrete. If the transcript doesn't cover it, say so in {target}.")
        user = f"Transcript:\n{transcript_text}\n\nQuestion: {question}"
        max_toks = 320
    else:
        sysmsg = (f"You summarize a live talk/stream from its running transcript. Give a concise {target} summary "
                  f"of the key points so far as short bullet points. Output only the summary, in {target}.")
        user = f"Transcript so far:\n{transcript_text}"
        max_toks = 420
    return [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}], max_toks
