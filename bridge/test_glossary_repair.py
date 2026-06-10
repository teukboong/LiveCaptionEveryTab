"""Tests for the post-ASR glossary spelling repair (_repair_glossary_terms / _gr_norm).

The repair rewrites fuzzy ASR spellings of user glossary source terms to their canonical form before
translation (granite cannot take ASR-side hints without losing punctuation — see transcribe_pcm). Pure
helpers, no model load:

    cd bridge && python test_glossary_repair.py
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


G = [("Blackwell", "블랙웰"), ("Sam Altman", "샘 올트먼"), ("Hesperides", "")]

# --- _gr_norm ---
check("norm.case_space", s._gr_norm("Black Well"), "blackwell")
check("norm.hyphen", s._gr_norm("Black-Well!"), "blackwell")
check("norm.hangul", s._gr_norm("블랙 웰"), "블랙웰")

# --- exact normalized match: split / merged / case-only transcriptions canonicalize ---
check("repair.split", s._repair_glossary_terms("the black well chip is fast", G),
      "the Blackwell chip is fast")
check("repair.caseonly", s._repair_glossary_terms("we saw blackwell yesterday", G),
      "we saw Blackwell yesterday")
check("repair.merged", s._repair_glossary_terms("I met SamAltman today", G),
      "I met Sam Altman today")
check("repair.multiword", s._repair_glossary_terms("sam altman said so", G),
      "Sam Altman said so")

# --- fuzzy match (>= ratio) ---
check("repair.fuzzy_typo", s._repair_glossary_terms("the Blackwel launch", G), "the Blackwell launch")
check("repair.fuzzy_extra", s._repair_glossary_terms("Hesperedes is a project", G),
      "Hesperides is a project")

# --- punctuation around the span survives ---
check("repair.punct", s._repair_glossary_terms("Have you seen black well? Yes.", G),
      "Have you seen Blackwell? Yes.")
check("repair.quote", s._repair_glossary_terms('He said "blackwell" twice.', G),
      'He said "Blackwell" twice.')

# --- must NOT touch ---
check("repair.already_canonical", s._repair_glossary_terms("Blackwell is here", G), "Blackwell is here")
check("repair.unrelated", s._repair_glossary_terms("the weather is nice today", G),
      "the weather is nice today")
check("repair.far_word", s._repair_glossary_terms("the blackboard is clean", G),
      "the blackboard is clean")
check("repair.empty_glossary", s._repair_glossary_terms("black well", []), "black well")
check("repair.empty_text", s._repair_glossary_terms("", G), "")

# short terms are never fuzzy-matched (collision-prone)
check("repair.short_term", s._repair_glossary_terms("go went gone", [("Go", "고 언어")]),
      "go went gone")

# an exact normalized match of a DIFFERENT glossary term is never rewritten to this one
G2 = [("OpenAI", ""), ("OpenAPI", "")]
check("repair.other_term_guard", s._repair_glossary_terms("the openapi spec", G2), "the OpenAPI spec")

# two occurrences both repaired; replacements don't overlap
check("repair.two_hits", s._repair_glossary_terms("black well and black well again", G),
      "Blackwell and Blackwell again")

# kill switch restores identity
s.GLOSSARY_REPAIR_ON = False
check("repair.killswitch", s._repair_glossary_terms("black well", G), "black well")
s.GLOSSARY_REPAIR_ON = True

if fails:
    print("test_glossary_repair: FAIL")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_glossary_repair: OK (norm + exact/fuzzy repair + guards pass)")
