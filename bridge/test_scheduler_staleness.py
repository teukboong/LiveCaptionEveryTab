"""Characterization tests for the pure preview-staleness predicate extracted from the translation
scheduler (_preview_is_stale).

Model-free; run under the bridge venv:
    cd bridge && python test_scheduler_staleness.py
"""
import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(name)


def job(final=False, unit_id=5, rev=2):
    return {"final": final, "unit_id": unit_id, "rev": rev}


FU = set()          # finalized_units
LPR = {5: 2}        # latest_preview_rev: unit 5 is currently at rev 2
CUR_UID, CUR_REV = 5, 2

# fresh preview for the active unit + rev -> NOT stale
ok("fresh", s._preview_is_stale(job(), FU, CUR_UID, CUR_REV, LPR) is False)
# final jobs are never stale (regardless of unit)
ok("final_never", s._preview_is_stale(job(final=True), FU, CUR_UID, CUR_REV, LPR) is False)
ok("final_never_old_unit", s._preview_is_stale(job(final=True, unit_id=999), FU, CUR_UID, CUR_REV, LPR) is False)
# the unit already finalized -> stale
ok("finalized", s._preview_is_stale(job(), {5}, CUR_UID, CUR_REV, LPR) is True)
# the active unit moved on -> stale
ok("unit_moved_on", s._preview_is_stale(job(unit_id=4), FU, 5, CUR_REV, {4: 2}) is True)
# current_rev advanced past the job's rev -> stale
ok("rev_superseded_current", s._preview_is_stale(job(rev=2), FU, 5, 3, LPR) is True)
# a newer preview rev was registered for the unit -> stale
ok("rev_superseded_latest", s._preview_is_stale(job(rev=2), FU, 5, 2, {5: 3}) is True)
# latest_preview_rev has no entry for the unit (get -> None != rev) -> stale
ok("rev_latest_missing", s._preview_is_stale(job(unit_id=7, rev=1), FU, 7, 1, {}) is True)

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_scheduler_staleness: OK (preview-staleness cases pass)")
