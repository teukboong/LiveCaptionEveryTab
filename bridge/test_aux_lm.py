"""Tests for the aux translator selection (_aux_lm_choice) — the dual-model concurrency gate.
Pure helper, no model load:

    cd bridge && python test_aux_lm.py
"""
import test_import_stubs
test_import_stubs.install()

import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(f"{name}: condition failed")


MAIN_26B = s._lm_select_value(s.lm_models()[0])
SMALL = s._lm_select_value(s.lm_models()[-1])
NEED = s.lm_models()[-1]["footprint_gb"] + s.AUX_LM_HEADROOM_GB

# off in every spelling
for off in ("", "0", "off", "no", "none", "false", "OFF"):
    ok(f"choice.off_{off or 'empty'}", s._aux_lm_choice(MAIN_26B, 64.0, off) is None)

# auto: pairs the smallest under the largest, only when memory fits
ok("choice.auto_fits", s._aux_lm_choice(MAIN_26B, NEED + 0.5, "auto") == SMALL)
ok("choice.auto_exact", s._aux_lm_choice(MAIN_26B, NEED, "auto") == SMALL)
ok("choice.auto_tight", s._aux_lm_choice(MAIN_26B, NEED - 0.5, "auto") is None)
ok("choice.auto_no_probe", s._aux_lm_choice(MAIN_26B, None, "auto") is None)
# auto never pairs under an already-small main pick
ok("choice.auto_small_main", s._aux_lm_choice(SMALL, 64.0, "auto") is None)
ok("choice.auto_mid_main", s._aux_lm_choice(s._lm_select_value(s.lm_models()[1]), 64.0, "auto") is None)

# explicit id resolves through the registry; user owns the RAM math (no memory gate)
ok("choice.explicit_id", s._aux_lm_choice(MAIN_26B, 0.1, "gemma-e2b") == SMALL)
ok("choice.explicit_repo", s._aux_lm_choice(MAIN_26B, 0.1, "my/custom-aux") == "my/custom-aux")
# explicit pick equal to the main translator is pointless -> disabled
ok("choice.explicit_same_as_main", s._aux_lm_choice(MAIN_26B, 64.0, "gemma-26b") is None)

if fails:
    print("test_aux_lm: FAIL")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_aux_lm: OK (aux translator selection gates pass)")
