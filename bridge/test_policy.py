"""Model-free tests for the interpretation-policy synthesis: _source_risk (numbers/negation) and
decide_commit (the unified commit-time decision wrapping _commit_decision + risk).

Run under the bridge venv:
    cd bridge && python test_policy.py
"""
import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(name)


# _source_risk: high when the line carries numbers or negation
ok("risk.number", s._source_risk("the port is 8765") == "high")
ok("risk.not", s._source_risk("this is not working") == "high")
ok("risk.contraction", s._source_risk("it doesn't work") == "high")
ok("risk.no", s._source_risk("there is no cache here") == "high")
ok("risk.plain", s._source_risk("welcome back to the stream") == "low")
ok("risk.single_digit", s._source_risk("I have 5 cats") == "low")          # single digit not significant
ok("risk.notes_nofalse", s._source_risk("review the latest patch notes") == "low")   # 'notes' must not match 'not'/'no'

# decide_commit: commit/wait choice + risk
ok("dc.eos", (lambda d: d.action == "commit" and d.reason == "eos")(s.decide_commit("hi", True, False, 0, 120, 1800)))
ok("dc.wait", s.decide_commit("short", False, False, 0, 120, 1800).action == "wait")
d4 = s.decide_commit("a normal clause here", False, True, 0, 120, 1800)
ok("dc.pause_low", d4.action == "commit" and d4.reason == "pause" and d4.risk == "low")
ok("dc.risk_high", s.decide_commit("the value is not 26 here", False, True, 0, 120, 1800).risk == "high")

# the commit/wait + reason MUST mirror _commit_decision exactly (behaviour-preserving wrapper)
for args in [("hi", True, False, 0, 120, 1800),
             ("x" * 200, False, False, 0, 120, 1800),
             ("we will", False, True, 9999, 120, 1800),
             ("a normal clause here", False, False, 5000, 120, 1800),
             ("short", False, False, 0, 120, 1800)]:
    f, r = s._commit_decision(*args)
    dd = s.decide_commit(*args)
    ok(f"dc.match[{args[0][:6]}]", (dd.action == "commit") == f and dd.reason == r)

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_policy: OK (source_risk + decide_commit cases pass)")
