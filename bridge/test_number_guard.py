"""Model-free tests for the high-risk number trust guard (_sig_numbers / _missing_numbers / _guard_numbers).

Run under the bridge venv:
    cd bridge && python test_number_guard.py
"""
import policy as p
import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(name)


# _sig_numbers: >= 2-digit runs (separators stripped); single digits intentionally ignored
ok("sig.basic", s._sig_numbers("port 8765 v4.5") == ["8765", "45"])
ok("sig.single_ignored", s._sig_numbers("I have 5 cats and 1 dog") == [])
ok("sig.year", s._sig_numbers("back in 2026 we shipped") == ["2026"])
ok("sig.commas", s._sig_numbers("1,000,000 won") == ["1000000"])
ok("sig.none", s._sig_numbers("no numbers here") == [])

# _missing_numbers: significant source numbers whose digits aren't anywhere in the translation
ok("miss.preserved", s._missing_numbers("port 8765", "포트는 8765입니다") == [])
ok("miss.dropped", s._missing_numbers("port 8765", "포트 번호입니다") == ["8765"])
ok("miss.single_nofalse", s._missing_numbers("5 cats", "고양이 다섯 마리") == [])   # single digit not guarded
ok("miss.partial", s._missing_numbers("26GB at 4.5x", "4.5배만 빨라요") == ["26"])

# _ko_number_forms: Sino + native spellings for 0..99; () outside the range
ok("kof.26", set(s._ko_number_forms("26")) == {"이십육", "스물여섯"})
ok("kof.20", set(s._ko_number_forms("20")) == {"이십", "스물"})
ok("kof.10", set(s._ko_number_forms("10")) == {"십", "열"})
ok("kof.over99", s._ko_number_forms("100") == ())

# spelled-out Korean numbers (Sino/native) must NOT be flagged as missing
ok("miss.sino", s._missing_numbers("at 20 percent", "이십 퍼센트로") == [])
ok("miss.native", s._missing_numbers("26 cats", "고양이 스물여섯 마리") == [])
ok("miss.sino26", s._missing_numbers("26 items", "이십육 개 남음") == [])
ok("miss.spellout_still_dropped", s._missing_numbers("26 items", "여러 개입니다") == ["26"])
ok("miss.over99_digits_only", s._missing_numbers("port 8765", "포트 번호") == ["8765"])

# _guard_numbers (requires LCC_NUMGUARD on): append the missing literal, flag uncertain
p.NUMGUARD_ON = True
disp, unc = s._guard_numbers("the port is 8765", "포트 번호입니다")
ok("guard.append", disp == "포트 번호입니다 (8765)" and unc is True)
disp2, unc2 = s._guard_numbers("the port is 8765", "포트는 8765입니다")
ok("guard.preserved_noop", disp2 == "포트는 8765입니다" and unc2 is False)
disp3, unc3 = s._guard_numbers("no numbers here", "숫자 없음")
ok("guard.nonumbers", disp3 == "숫자 없음" and unc3 is False)
dispm, uncm = s._guard_numbers("26GB and 4.5x", "4.5배")
ok("guard.multi", dispm == "4.5배 (26)" and uncm is True)
dispk, unck = s._guard_numbers("about 26 people", "스물여섯 명 정도")
ok("guard.spellout_noop", dispk == "스물여섯 명 정도" and unck is False)

# disabled -> never touches the translation (byte-identical)
p.NUMGUARD_ON = False
d, u = s._guard_numbers("the port is 8765", "포트 번호입니다")
ok("guard.off", d == "포트 번호입니다" and u is False)

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_number_guard: OK (extraction + preservation + guard cases pass)")
