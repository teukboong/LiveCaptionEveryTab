import os
import re

from text_helpers import _gr_norm


# --- Session term memory (auto-glossary) ---------------------------------------------------------------
# recent_pairs only carries the last few finals, so on a long stream the 26B forgets how it rendered a name
# twenty minutes ago and the rendering drifts. Mine recurring proper-noun-ish terms from committed finals
# and pin them into the glossary clause automatically: a term the model kept VERBATIM in the translation
# (e.g. "GPT", "Blackwell" left in Latin) pins as an exact pair; everything else pins term-only ("keep
# consistent" + ASR biasing). User glossary entries always win. Updates are BATCHED (every N finals) because
# the glossary lives in the system prompt — every change invalidates the translator's KV prefix, so the
# ~850ms re-prefill is amortized. Off: LCC_TERM_MEMORY=0. Tested in test_term_memory.py.
TERM_MEMORY_ON = os.environ.get("LCC_TERM_MEMORY", "1") == "1"
TERM_MEMORY_MAX = max(0, int(os.environ.get("LCC_TERM_MEMORY_MAX", "12")))          # auto terms in the clause
TERM_MEMORY_MIN_COUNT = max(1, int(os.environ.get("LCC_TERM_MEMORY_MIN_COUNT", "2")))  # recur before pinning
TERM_MEMORY_UPDATE_EVERY = max(1, int(os.environ.get("LCC_TERM_MEMORY_UPDATE_EVERY", "8")))
TERM_MEMORY_STATS_MAX = 200
# Single capitalized words that are ordinary sentence material, not names. Filters SINGLE-word candidates
# only — multi-word runs ("Sam Altman") and acronyms are kept.
_TERM_STOPWORDS = frozenset(w.casefold() for w in (
    "The This That These Those There Here What When Where Which Who Whose Why How If And But Or So Not "
    "No Yes It Its He She They We You I My Our Your His Her Their Then Now Today Tonight Yesterday "
    "Tomorrow Okay Oh Hey Hello Hi Thanks Thank Well Right Let Look Listen Just Also Even Still Maybe "
    "Please Sorry Is Are Was Were Do Does Did Done Have Has Had Can Could Will Would Should May Might "
    "Must Get Got Go Going Gone Come Coming Welcome Back New One Two Three Four Five First Second Next "
    "Last Good Great Big Small Many Most More Some All Every Each Other Another Because Before After "
    "Over Under Again Anyway Actually Basically Literally Honestly Alright Guys Everyone Everybody"
).split())
_TERM_CAND_RE = re.compile(r"\b(?:[A-Z]{2,}[0-9]*|[A-Z][a-zA-Z0-9]{2,})\b")
_TERM_SENT_LEAD_RE = re.compile(r"[.!?。！？…\"'»」』)\]]\s*$")


def _mine_terms(source: str, ko: str):
    """Proper-noun-ish term candidates from one committed (source, translation) pair.
    Returns [(term, rendering)] where rendering == term when the translation kept the term verbatim
    (locks the Latin form), else "" (term-only: consistency clause + ASR bias). Adjacent capitalized
    words merge into one multi-word term; sentence-initial single words are skipped (too often just
    sentence case), as are stopwords. Latin-script mining only (the dominant EN->KO direction)."""
    source, ko = source or "", ko or ""
    matches = list(_TERM_CAND_RE.finditer(source))
    if not matches:
        return []
    runs, cur = [], []
    for m in matches:
        if cur and source[cur[-1].end():m.start()] == " ":
            cur.append(m)
        else:
            if cur:
                runs.append(cur)
            cur = [m]
    runs.append(cur)
    out, seen = [], set()
    for run in runs:
        while run and run[0].group(0).casefold() in _TERM_STOPWORDS:
            run = run[1:]                                   # "The OpenAI" -> "OpenAI"
        if not run:
            continue
        term = source[run[0].start():run[-1].end()]
        if len(run) == 1:
            w = run[0].group(0)
            if w.casefold() in _TERM_STOPWORDS:
                continue
            acronym = w.isupper()
            lead = source[:run[0].start()].strip()
            initial = not lead or bool(_TERM_SENT_LEAD_RE.search(source[:run[0].start()]))
            if initial and not acronym:                     # sentence case, not evidence of a name
                continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append((term, term if term in ko else ""))
    return out


def _update_term_memory(stats: dict, source: str, ko: str, now: float):
    """Fold one committed final into the running term stats (mutates; bounded). Returns True when a
    term reached pin eligibility or gained a verbatim rendering — i.e. the merged clause may change."""
    notable = False
    for term, rendering in _mine_terms(source, ko):
        rec = stats.get(term)
        if rec is None:
            rec = stats[term] = {"count": 0, "rendering": "", "seen": 0.0}
        rec["count"] += 1
        rec["seen"] = now
        if rec["count"] == TERM_MEMORY_MIN_COUNT:
            notable = True
        if rendering and not rec["rendering"]:
            rec["rendering"] = rendering
            if rec["count"] >= TERM_MEMORY_MIN_COUNT:
                notable = True
    if len(stats) > TERM_MEMORY_STATS_MAX:                  # bound pathological sessions
        for k in sorted(stats, key=lambda t: (stats[t]["count"], stats[t]["seen"]))[:len(stats) - TERM_MEMORY_STATS_MAX]:
            del stats[k]
    return notable


def _merge_auto_glossary(user_pairs, stats: dict, cap: int = None):
    """The auto-pinned (term, rendering) list: recurring terms not already covered by the user glossary,
    most-frequent first, capped. Pure — the caller appends this to the user pairs at prompt-build time."""
    cap = TERM_MEMORY_MAX if cap is None else cap
    if cap <= 0:
        return []
    user_norms = {_gr_norm(s) for s, _ in (user_pairs or ())}
    cands = [(t, rec) for t, rec in stats.items()
             if rec["count"] >= TERM_MEMORY_MIN_COUNT and _gr_norm(t) not in user_norms]
    cands.sort(key=lambda kv: (-kv[1]["count"], -kv[1]["seen"], kv[0]))
    return [(t, rec["rendering"]) for t, rec in cands[:cap]]
