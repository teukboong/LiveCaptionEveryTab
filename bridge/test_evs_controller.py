"""Model-free tests for the EVS (Ear-Voice Span) load-adaptive controller: the pure hysteresis step
(_evs_step) and the pressure-modulated knobs (_lat_pending_cap / _lat_pending_max_age_ms).

Run under the bridge venv:
    cd bridge && python test_evs_controller.py
"""
import policy as p
import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(name)


# Force the controller on with known thresholds (the module reads these globals at call time).
p.EVS_ON = True
p.EVS_ENTER_MS = 1800
p.EVS_EXIT_MS = 900

# from nominal (0): hold below ENTER, flip to pressured at/above ENTER
ok("enter.below", s._evs_step(0, 1799) == 0)
ok("enter.at", s._evs_step(0, 1800) == 1)
ok("enter.high", s._evs_step(0, 9000) == 1)
# from pressured (1): hold above EXIT, relax at/below EXIT
ok("exit.above", s._evs_step(1, 901) == 1)
ok("exit.at", s._evs_step(1, 900) == 0)
ok("exit.zero", s._evs_step(1, 0) == 0)
# hysteresis dead-band [EXIT, ENTER): the level is held, not flipped
ok("hysteresis.hold_nominal", s._evs_step(0, 1200) == 0)
ok("hysteresis.hold_pressured", s._evs_step(1, 1200) == 1)

# disabled -> always nominal (byte-identical to the static profile)
p.EVS_ON = False
ok("off.from0", s._evs_step(0, 9_999_999) == 0)
ok("off.from1", s._evs_step(1, 9_999_999) == 0)
p.EVS_ON = True

# knob modulation: pressure shaves the thresholds; nominal is unchanged
p.EVS_CAP_DROP = 40
p.EVS_AGE_DROP = 600
ok("cap.nominal", s._lat_pending_cap("aggressive", 0) == s.AGG_PENDING_CAP)
ok("cap.pressured", s._lat_pending_cap("aggressive", 1) == max(40, s.AGG_PENDING_CAP - 40))
ok("age.nominal", s._lat_pending_max_age_ms("balanced", 0) == s.BAL_PENDING_MAX_AGE_MS)
ok("age.pressured", s._lat_pending_max_age_ms("balanced", 1) == max(600, s.BAL_PENDING_MAX_AGE_MS - 600))
# default pressure arg (0) keeps the old call signature working -> nominal
ok("cap.default_arg", s._lat_pending_cap("aggressive") == s.AGG_PENDING_CAP)

# floors hold even under an absurd drop (never commit on a 0-char / 0-ms threshold)
p.EVS_CAP_DROP = 1_000_000
p.EVS_AGE_DROP = 1_000_000
ok("cap.floor", s._lat_pending_cap("aggressive", 1) == 40)
ok("age.floor", s._lat_pending_max_age_ms("aggressive", 1) == 600)

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_evs_controller: OK (hysteresis + knob modulation cases pass)")
