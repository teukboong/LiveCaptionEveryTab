"""Tests for the session term memory (auto-glossary): _mine_terms / _update_term_memory /
_merge_auto_glossary. Pure helpers, no model load:

    cd bridge && python test_term_memory.py
"""
import test_import_stubs
test_import_stubs.install()

import server as s

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def ok(name, cond):
    if not cond:
        fails.append(f"{name}: condition failed")


# --- _mine_terms: candidates ---
check("mine.name_mid", s._mine_terms("we talked to Blackwell about it", "블랙웰과 얘기했어요"),
      [("Blackwell", "")])
check("mine.verbatim_lock", s._mine_terms("the new GPU is from NVIDIA", "새 GPU는 NVIDIA 거예요"),
      [("GPU", "GPU"), ("NVIDIA", "NVIDIA")])
check("mine.multiword", s._mine_terms("I met Sam Altman yesterday", "어제 샘 올트먼을 만났어요"),
      [("Sam Altman", "")])
check("mine.leading_stopword_run", s._mine_terms("so The OpenAI team shipped it", "오픈AI 팀이 출시했죠"),
      [("OpenAI", "")])

# sentence-initial single capitalized word is NOT evidence of a name
check("mine.sent_initial", s._mine_terms("Today we ship the update.", "오늘 업데이트를 내요"), [])
check("mine.after_punct", s._mine_terms("It works. Really well.", "잘 돼요. 아주요."), [])
# ... but a sentence-initial ACRONYM still counts
check("mine.initial_acronym", s._mine_terms("NASA launched it.", "NASA가 발사했죠"), [("NASA", "NASA")])
# stopwords never mine, even mid-sentence
check("mine.stopword_mid", s._mine_terms("and Then it broke", "그리고 그게 망가졌어요"), [])
check("mine.empty", s._mine_terms("", ""), [])
# dedupe within one line
check("mine.dedupe", s._mine_terms("Gemma is fast, Gemma is small", "젬마는 빠르고 작아요"),
      [("Gemma", "")])

# --- _update_term_memory: counts, notability, rendering upgrade ---
stats = {}
ok("upd.first_not_notable", s._update_term_memory(stats, "we saw Blackwell today", "봤어요", 1.0) is False)
ok("upd.second_notable", s._update_term_memory(stats, "yes Blackwell again", "또요", 2.0) is True)
check("upd.count", stats["Blackwell"]["count"], 2)
ok("upd.rendering_upgrade_notable",
   s._update_term_memory(stats, "and Blackwell shipped", "Blackwell 출시", 3.0) is True)
check("upd.rendering", stats["Blackwell"]["rendering"], "Blackwell")

# --- _merge_auto_glossary: pin threshold, user-term exclusion, cap, ordering ---
stats2 = {
    "Gemma": {"count": 3, "rendering": "", "seen": 5.0},
    "NVIDIA": {"count": 2, "rendering": "NVIDIA", "seen": 4.0},
    "Once": {"count": 1, "rendering": "", "seen": 6.0},          # below min count -> out
}
check("merge.basic", s._merge_auto_glossary([], stats2),
      [("Gemma", ""), ("NVIDIA", "NVIDIA")])
check("merge.user_excluded", s._merge_auto_glossary([("gemma", "젬마")], stats2),
      [("NVIDIA", "NVIDIA")])
check("merge.cap", s._merge_auto_glossary([], stats2, cap=1), [("Gemma", "")])
check("merge.cap_zero", s._merge_auto_glossary([], stats2, cap=0), [])

# stats bounding survives a flood
stats3 = {}
for i in range(s.TERM_MEMORY_STATS_MAX + 60):
    stats3[f"Term{i:04d}x"] = {"count": 1, "rendering": "", "seen": float(i)}
s._update_term_memory(stats3, "we saw Blackwell here", "응", 999.0)
ok("upd.bounded", len(stats3) <= s.TERM_MEMORY_STATS_MAX)
ok("upd.kept_recent", "Blackwell" in stats3)

if fails:
    print("test_term_memory: FAIL")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_term_memory: OK (mining + stats + merge pass)")
