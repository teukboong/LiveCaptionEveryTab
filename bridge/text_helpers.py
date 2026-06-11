import os
import re

PAGE_CHUNK_CHARS = max(200, int(os.environ.get("LCC_PAGE_CHUNK_CHARS", "500"))) # target size per chunk in that path

SENT_END = re.compile(r"[.!?。！？…][\"'»」』）)]?")   # candidate sentence boundary
MIN_SENT_CHARS = 18        # a split shorter than this is likely an abbreviation (Dr./Mr.), not a sentence
WEAK_TAIL_WORDS = {
    "and", "or", "but", "so", "because", "that", "which", "who", "to", "of", "in", "for",
    "with", "as", "at", "from", "by", "if", "when", "while", "than", "then",
    "i", "we", "you", "they", "he", "she", "it", "a", "an", "the",
    "am", "is", "are", "was", "were", "be", "being", "been",
    "will", "would", "can", "could", "should", "may", "might", "must",
    "do", "does", "did", "have", "has", "had",
}


def _has_hangul(s: str) -> bool:
    return any("가" <= c <= "힣" for c in s)


def _lcp_words(a, b):
    """Longest common word-prefix length (LocalAgreement n=2)."""
    n = 0
    for x, y in zip(a, b):
        if x == y:
            n += 1
        else:
            break
    return n


def _coalesce_batch(batch):
    """LA partials are UX-only. Drop them when finalizable work (clause/flush/eos) is queued,
    otherwise keep only the latest — so stale partials never delay the real ASR/translation."""
    if any(x[0] in ("clause", "flush", "eos") for x in batch):
        return [x for x in batch if x[0] != "partial"]
    latest, out = None, []
    for x in batch:
        if x[0] == "partial":
            latest = x
        else:
            out.append(x)
    if latest is not None:
        out.append(latest)
    return out


def _next_sentence_cut(text: str) -> int:
    """Index to split off the first COMPLETE sentence, or -1. Skips false boundaries: decimals (5.0)
    and short fragments ending in a dotted abbreviation (Dr./Mr.) via a minimum-length guard."""
    for m in SENT_END.finditer(text):
        i, j = m.start(), m.end()
        if text[i] == "." and i > 0 and text[i - 1].isdigit() and j < len(text) and text[j].isdigit():
            continue                                   # decimal point, not a sentence end
        if len(text[:j].strip()) >= MIN_SENT_CHARS:
            return j
    return -1


def _norm_word(w: str) -> str:
    return re.sub(r"^[^\w가-힣]+|[^\w가-힣]+$", "", w.lower())


def _norm_words(text: str):
    return [w for w in (_norm_word(x) for x in (text or "").split()) if w]


def _short_suffix_duplicate(new: str, prev: str) -> bool:
    nw, pw = _norm_words(new), _norm_words(prev)
    return bool(nw and pw and len(nw) <= 4 and len(pw) > len(nw) and pw[-len(nw):] == nw)


def _append_text_dedupe(prev: str, new: str) -> str:
    prev, new = prev.strip(), new.strip()
    if not prev:
        return new
    if not new:
        return prev
    if new.lower() in prev.lower():
        return prev
    pw, nw = prev.split(), new.split()
    max_k = min(14, len(pw), len(nw))
    for k in range(max_k, 0, -1):
        if [_norm_word(w) for w in pw[-k:]] == [_norm_word(w) for w in nw[:k]]:
            tail = " ".join(nw[k:]).strip()
            return prev if not tail else f"{prev} {tail}"
    return f"{prev} {new}"


def _dedupe_commit_overlap(text: str, tail_words, overlapped: bool) -> str:
    """When a sentence just committed and the next clause's audio OVERLAPPED it (soft/VAD overlap), drop the
    leading words of `text` that duplicate the committed sentence's tail (tail_words = its last normalized
    words). The overlap guard is load-bearing: across a REAL pause a repeat is legitimate ("…it. It is…"), so
    strip only when the audio actually overlapped. _append_text_dedupe handles this WITHIN a unit; this covers
    the cross-commit case the sentence split opens. Tested in test_assembler_decisions.py."""
    if not overlapped or not tail_words or not text:
        return text
    nw = text.split()
    tail = list(tail_words)
    for k in range(min(len(tail), len(nw), 3), 0, -1):
        if [_norm_word(w) for w in nw[:k]] == tail[-k:]:
            return " ".join(nw[k:]).strip()
    return text


def _weak_tail(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if s[-1] in ",;:、，-":
        return True
    words = s.split()
    return bool(words and _norm_word(words[-1]) in WEAK_TAIL_WORDS)


TX_REPEAT_MAX_CHARS = max(10, int(os.environ.get("LCC_TX_REPEAT_MAX_CHARS", "60")))


def _repeat_cache_eligible(source: str) -> bool:
    """True for short lines that read as complete on their own: anything tiny, or up to
    TX_REPEAT_MAX_CHARS when it ends on terminal punctuation. Tested in test_text_helpers.py."""
    s = (source or "").strip()
    if not s or len(s) > TX_REPEAT_MAX_CHARS:
        return False
    return len(s) <= 30 or bool(SENT_END.fullmatch(s[-2:]) or SENT_END.fullmatch(s[-1:]))


def _repeat_key(source: str) -> str:
    # a question must not reuse a declarative rendering ("Okay?" served "Okay."'s cache) — keep the mood
    # in the key. Other terminal punctuation stays normalized away (case/"!" insensitivity is intended).
    mood = "?" if (source or "").rstrip().endswith(("?", "？")) else ""
    return " ".join(_norm_words(source)) + mood


_CLEAN_RE = re.compile(r"<\|?channel\|?>.*?<\|?channel\|?>", re.S)
def _clean(s: str) -> str:
    return _CLEAN_RE.sub("", s).strip()


_KANA_RE = re.compile(r"[぀-ヿ]")        # hiragana + katakana -> Japanese source
_LATIN_RE = re.compile(r"[A-Za-z]")
_HANGUL_RE = re.compile(r"[가-힣]")
def _src_lang(text: str) -> str:
    # Ratio-based, not "any hangul -> Korean": an English line with a Korean name (e.g.
    # "I talked to 민준 about the demo") must NOT be treated as Korean (would skip translation).
    h = len(_HANGUL_RE.findall(text or ""))
    k = len(_KANA_RE.findall(text or ""))
    lat = len(_LATIN_RE.findall(text or ""))
    letters = h + k + lat
    if letters <= 0:
        return "English"
    if h >= 4 and h / letters >= 0.45:
        return "Korean"
    if k >= 2 and k / letters >= 0.30:
        return "Japanese"
    return "English"


def _gr_norm(s: str) -> str:
    """Casefold + strip everything non-alphanumeric, so 'black well' / 'Black-Well' both read 'blackwell'."""
    return re.sub(r"[^0-9a-z가-힣]+", "", (s or "").casefold())


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？…])\s+|\n[ \t]*\n")


def _split_sentences(text: str):
    return [p.strip() for p in _SENT_SPLIT_RE.split(str(text or "").strip()) if p and p.strip()]


def _chunk_text(text: str, max_chars: int = None):
    """Group sentences into <= max_chars chunks without splitting a sentence (a lone over-long sentence is
    hard-split as a last resort). Returns the chunks in order."""
    max_chars = max_chars or PAGE_CHUNK_CHARS
    chunks, cur = [], ""
    for s in _split_sentences(text):
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= max_chars:
            cur += " " + s
        else:
            chunks.append(cur); cur = s
        while len(cur) > max_chars * 2:                 # a single giant sentence -> hard split (rare)
            chunks.append(cur[:max_chars]); cur = cur[max_chars:].strip()
    if cur:
        chunks.append(cur)
    return chunks or [str(text or "").strip()]
