"""Characterization tests for the pure decision logic extracted from inference_loop (the assembler):
_commit_decision (force-commit + reason) and _two_pass_eligible (accuracy-mode 2-pass gate).

Model-free; run under the bridge venv:
    cd bridge && python test_assembler_decisions.py
"""
import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(name)


def eq(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r} want {want!r}")


PC, PMA = 120, 1800   # pending_cap, pending_max_age_ms (aggressive-ish values)

# _commit_decision(text, eos_now, finalize_now, age_ms, pending_cap, pending_max_age_ms) -> (force, reason)
eq("cd.eos", s._commit_decision("hi", True, False, 0, PC, PMA), (True, "eos"))
eq("cd.cap", s._commit_decision("x" * 200, False, False, 0, PC, PMA), (True, "cap"))
eq("cd.age", s._commit_decision("a normal clause here", False, False, 5000, PC, PMA), (True, "age"))
eq("cd.pause", s._commit_decision("a normal clause here", False, True, 0, PC, PMA), (True, "pause"))
eq("cd.none", s._commit_decision("short", False, False, 0, PC, PMA), (False, ""))
# a weak tail (conjunction/aux/trailing comma) defers pause/age/cap...
eq("cd.weak_defers", s._commit_decision("we will", False, True, 9999, PC, PMA), (False, ""))
eq("cd.weak_comma", s._commit_decision("hello,", False, True, 9999, PC, PMA), (False, ""))
# ...but never an eos
eq("cd.weak_eos", s._commit_decision("we will", True, False, 0, PC, PMA), (True, "eos"))
# precedence: eos reason wins even when too_long would also fire
eq("cd.eos_over_cap", s._commit_decision("x" * 200, True, False, 0, PC, PMA), (True, "eos"))
# precedence: cap (too_long) wins over age in the reason string
eq("cd.cap_over_age", s._commit_decision("x" * 200, False, False, 9999, PC, PMA), (True, "cap"))

# _two_pass_eligible(accuracy_mode, unit_pure, unit_clauses, pcm_len)
lo = int(s.TWO_PASS_MIN_SEC * s.SR) * 2
hi = int(s.TWO_PASS_MAX_SEC * s.SR) * 2
mid = (lo + hi) // 2
ok("tp.eligible", s._two_pass_eligible(True, True, 2, mid) is True)
ok("tp.acc_off", s._two_pass_eligible(False, True, 2, mid) is False)
ok("tp.not_pure", s._two_pass_eligible(True, False, 2, mid) is False)
ok("tp.few_clauses", s._two_pass_eligible(True, True, 1, mid) is False)
ok("tp.too_short", s._two_pass_eligible(True, True, 2, lo - 2) is False)
ok("tp.too_long", s._two_pass_eligible(True, True, 2, hi + 2) is False)
ok("tp.lo_bound", s._two_pass_eligible(True, True, 2, lo) is True)
ok("tp.hi_bound", s._two_pass_eligible(True, True, 2, hi) is True)

# _dedupe_commit_overlap(text, tail_words, overlapped): drop a re-transcribed boundary word, ON OVERLAP ONLY
eq("dov.overlap", s._dedupe_commit_overlap("protein structure prediction", ["challenge", "of", "protein"], True), "structure prediction")
eq("dov.two_word", s._dedupe_commit_overlap("David Baker was", ["jumper", "and", "david"], True), "Baker was")
eq("dov.no_overlap", s._dedupe_commit_overlap("It is great", ["i", "love", "it"], False), "It is great")   # real pause -> keep legit repeat
eq("dov.no_match", s._dedupe_commit_overlap("structure prediction", ["challenge", "of", "protein"], True), "structure prediction")
eq("dov.empty_tail", s._dedupe_commit_overlap("protein structure", [], True), "protein structure")
eq("dov.empty_text", s._dedupe_commit_overlap("", ["a", "b"], True), "")

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_assembler_decisions: OK (commit + 2-pass decision cases pass)")
